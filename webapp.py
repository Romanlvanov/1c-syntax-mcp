"""HTTP-транспорт MCP (streamable) + веб-панель управления индексами.

Импортируется ЛЕНИВО из server.py (только когда --transport http) — чтобы
stdio-режим и CLI (`--build-index`) не тянули Starlette/uvicorn и чтобы
tests/_server_loader.py не нуждался в заглушках для этого стека.

Диспетчер ASGI решает конкретную проблему, измеренную на установленной версии
Starlette (1.6.0): `Mount("/mcp", app=manager.handle_request)` компилируется в
маршрут `^/mcp/(?P<path>.*)$` — голый `/mcp` не матчится и роутер отдаёт 307,
на котором часть MCP-клиентов теряет тело POST. Здесь `/mcp` и `/mcp/`
обрабатываются одинаково, без редиректа: диспетчер сравнивает путь напрямую и
передаёт управление StreamableHTTPSessionManager, минуя Starlette-роутинг
целиком; всё остальное (панель, /api/*, лайфспан) идёт в обычное Starlette-
приложение."""

import contextlib
import logging
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import anyio
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import platforms as platforms_mod
import server as server_mod
from hbk_reader import open_hbk
from index_manager import IndexRegistry, hbk_fingerprint, is_valid_version

_logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
_HBK_KINDS = ("shcntx_ru", "shquery_ru", "shlang_ru")


def _security_settings(settings):
    """None -> DNS-rebinding защита выключена (совместимо с локальной работой
    из панели/curl без лишней настройки). Явно включается только когда заданы
    ALLOWED_HOSTS -- иначе TransportSecuritySettings() с пустым allowed_hosts
    отклоняет ЛЮБОЙ запрос 421-м (проверено на исходниках transport_security.py:
    allowed_hosts=[] никогда не проходит _validate_host)."""
    if not settings.allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def _check_fts5() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(a, tokenize=\"unicode61 remove_diacritics 2\")")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


async def _startup_ensure_default(settings, registry):
    """Выполняется в фоне ПОСЛЕ того, как uvicorn уже слушает порт -- панель
    и /api/health должны отвечать сразу, а не через ~10 секунд сборки индекса.
    Старое поведение stdio-режима (print+return при отсутствии платформы,
    server.py) здесь недопустимо: сервер обязан подняться даже без единой
    установленной версии 1С, чтобы можно было открыть панель и загрузить .hbk."""
    version = await anyio.to_thread.run_sync(registry.resolve_default, settings.default_version)
    if version is None:
        _logger.info("Платформ 1С не найдено и готового индекса нет — ждём загрузки через веб-панель")
        return
    if registry.has_built_index(version):
        _logger.info("Индекс по умолчанию уже собран: %s", version)
        return
    if not settings.auto_build:
        _logger.info("Индекс для версии %s не собран, автосборка выключена (SYNTAX_MCP_AUTO_BUILD=0)", version)
        return

    job_id = registry.jobs.create("build", version)

    def _build():
        return registry.build_version(
            version, progress=lambda phase, done, total: registry.jobs.progress(job_id, phase, done, total)
        )

    try:
        result = await anyio.to_thread.run_sync(_build)
        registry.jobs.finish(job_id, result)
        _logger.info("Индекс %s собран автоматически: %s элементов", version, result.get("count"))
    except Exception as e:
        registry.jobs.fail(job_id, e)
        _logger.warning("Автосборка индекса %s не удалась: %s", version, e)


async def health(request):
    registry = request.app.state.registry
    version = registry.get_default_version()
    return JSONResponse({
        "ok": True,
        "ready": bool(version) and registry.has_built_index(version),
        "default_version": version,
        "fts5": _check_fts5(),
    })


async def state(request):
    registry = request.app.state.registry
    settings = request.app.state.settings
    rows = await anyio.to_thread.run_sync(registry.list_state)
    return JSONResponse({
        "versions": rows,
        "jobs": registry.jobs.list_all(),
        "mcp_url": f"http://{settings.host}:{settings.port}/mcp",
    })


async def root(request):
    index_html = WEB_DIR / "panel.html"
    if index_html.exists():
        return HTMLResponse(index_html.read_text(encoding="utf-8"))
    return PlainTextResponse(
        "1C Syntax MCP\n\n"
        "Веб-панель ещё не собрана в этом образе. Доступны:\n"
        "  GET  /api/health\n  GET  /api/state\n  ANY  /mcp\n"
    )


def _check_panel_token(request):
    """Гейт на изменяющие /api/* маршруты (не на /api/health, /api/state, /mcp —
    у /mcp своя защита через TransportSecuritySettings). Пусто по умолчанию:
    панель рассчитана на публикацию строго на 127.0.0.1 (docs/IMPROVEMENT_PLAN.md,
    Docker-раздел), токен — дополнительный, а не единственный рубеж."""
    settings = request.app.state.settings
    if not settings.panel_token:
        return None
    if request.headers.get("x-panel-token", "") != settings.panel_token:
        return JSONResponse({"error": "неверный или отсутствующий заголовок X-Panel-Token"}, status_code=401)
    return None


def _invalid_version(version):
    return JSONResponse(
        {"error": f"Некорректный формат версии: {version!r} (ожидается вида 8.5.1.1150)"},
        status_code=400,
    )


async def upload_hbk(request):
    """PUT потоковой загрузки -- request.stream(), не multipart: постоянная
    память независимо от размера файла (shcntx_ru.hbk — 35-40 МБ на диске) и не
    тянет python-multipart как зависимость. Прогресс на стороне браузера считается
    через XHR upload.onprogress -- fetch() не даёт событий прогресса ОТДАЧИ тела."""
    denied = _check_panel_token(request)
    if denied is not None:
        return denied

    version = request.path_params["version"]
    kind = request.path_params["kind"]
    settings = request.app.state.settings

    if not is_valid_version(version):
        return _invalid_version(version)
    if kind not in _HBK_KINDS:
        return JSONResponse({"error": f"Недопустимый kind: {kind!r}, ожидается одно из {_HBK_KINDS}"}, status_code=400)

    max_bytes = settings.max_upload_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return JSONResponse({"error": f"Файл больше лимита {settings.max_upload_mb} МБ"}, status_code=413)
        except ValueError:
            pass

    upload_dir = Path(settings.upload_dir)
    incoming_dir = upload_dir / ".incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = incoming_dir / f"{uuid.uuid4().hex}.part"

    try:
        size = 0
        with open(tmp_path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    return JSONResponse({"error": f"Файл больше лимита {settings.max_upload_mb} МБ"}, status_code=413)
                f.write(chunk)

        # проверка, что это ДЕЙСТВИТЕЛЬНО контейнер .hbk -- в потоке, разбирает
        # файл целиком (hbk_reader.open_hbk), честно падает HbkFormatError на
        # обрезанной или повреждённой загрузке — до того, как что-либо заменено
        def _describe():
            zf = open_hbk(tmp_path)
            return {"member_count": len(zf.namelist()), "sha256": hbk_fingerprint(tmp_path)["sha256"]}

        try:
            info = await anyio.to_thread.run_sync(_describe)
        except Exception as e:
            return JSONResponse({"error": f"Не похоже на файл .hbk: {e}"}, status_code=400)

        dest_dir = upload_dir / version / "bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{kind}.hbk"
        os.replace(tmp_path, dest_path)
        Path(str(dest_path) + ".fingerprint.json").unlink(missing_ok=True)  # старый кеш не про этот файл

        return JSONResponse({
            "version": version, "kind": kind, "size": size,
            "sha256": info["sha256"], "member_count": info["member_count"],
        }, status_code=201)
    finally:
        tmp_path.unlink(missing_ok=True)
        Path(str(tmp_path) + ".fingerprint.json").unlink(missing_ok=True)


async def build_version_route(request):
    denied = _check_panel_token(request)
    if denied is not None:
        return denied

    version = request.path_params["version"]
    registry = request.app.state.registry
    if not is_valid_version(version):
        return _invalid_version(version)

    platform = platforms_mod.find_platform(version)
    if platform is None or not platform.has_help:
        return JSONResponse(
            {"error": f"Версия {version!r} не найдена или не имеет shcntx_ru.hbk — сначала загрузите файл"},
            status_code=404,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(body.get("force")) if isinstance(body, dict) else False

    job_id = registry.jobs.create("build", version)

    async def _run():
        def _build():
            return registry.build_version(
                version, force=force,
                progress=lambda phase, done, total: registry.jobs.progress(job_id, phase, done, total),
            )
        try:
            result = await anyio.to_thread.run_sync(_build)
            registry.jobs.finish(job_id, result)
        except Exception as e:
            registry.jobs.fail(job_id, e)

    request.app.state.task_group.start_soon(_run)
    return JSONResponse({"job_id": job_id, "version": version}, status_code=202)


async def job_status(request):
    registry = request.app.state.registry
    job = registry.jobs.get(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"error": "задача не найдена"}, status_code=404)
    return JSONResponse(job)


async def set_default_route(request):
    denied = _check_panel_token(request)
    if denied is not None:
        return denied

    registry = request.app.state.registry
    try:
        body = await request.json()
    except Exception:
        body = {}
    version = body.get("version") if isinstance(body, dict) else None
    if not version:
        return JSONResponse({"error": "требуется поле version"}, status_code=400)
    try:
        registry.set_default(version)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"default_version": version})


async def delete_version_route(request):
    denied = _check_panel_token(request)
    if denied is not None:
        return denied

    version = request.path_params["version"]
    if not is_valid_version(version):
        return _invalid_version(version)
    with_files = request.query_params.get("with_files") in ("1", "true", "yes")
    registry = request.app.state.registry
    await anyio.to_thread.run_sync(registry.delete_version, version, with_files)
    return JSONResponse({"deleted": version, "with_files": with_files})


async def search_probe(request):
    """Проба поиска для панели — самопроверка без отдельного MCP-клиента."""
    registry = request.app.state.registry
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body or {}).get("query", "") if isinstance(body, dict) else ""
    if not query:
        return JSONResponse({"error": "требуется поле query"}, status_code=400)
    limit = int((body or {}).get("limit", 10))
    platform = (body or {}).get("platform") or None

    def _search():
        db = registry.get_db(platform)
        if db is None:
            return None
        return db.search(query, limit=limit)

    results = await anyio.to_thread.run_sync(_search)
    if results is None:
        return JSONResponse({"error": "индекс не собран"}, status_code=409)
    return JSONResponse({"results": results})


async def _json_error_handler(request, exc):
    _logger.exception("Необработанная ошибка на %s", request.url.path, exc_info=exc)
    return JSONResponse({"error": str(exc)}, status_code=500)


def create_app(settings, registry, manager):
    routes = [Route("/api/health", health, methods=["GET"])]
    if settings.panel_enabled:
        routes.append(Route("/", root, methods=["GET"]))
        routes.append(Route("/api/state", state, methods=["GET"]))
        routes.append(Route(
            "/api/uploads/{version}/{kind}", upload_hbk, methods=["PUT"],
            max_body_size=settings.max_upload_bytes,
        ))
        routes.append(Route("/api/versions/{version}/build", build_version_route, methods=["POST"]))
        routes.append(Route("/api/jobs/{job_id}", job_status, methods=["GET"]))
        routes.append(Route("/api/default", set_default_route, methods=["POST"]))
        routes.append(Route("/api/versions/{version}", delete_version_route, methods=["DELETE"]))
        routes.append(Route("/api/search", search_probe, methods=["POST"]))
        if WEB_DIR.is_dir():
            routes.append(Mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static"))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            async with anyio.create_task_group() as tg:
                app.state.task_group = tg
                tg.start_soon(_startup_ensure_default, settings, registry)
                yield

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={Exception: _json_error_handler})
    app.state.registry = registry
    app.state.settings = settings
    app.state.manager = manager
    return app


def create_asgi(settings):
    """Возвращает верхнеуровневый ASGI-колбэк: uvicorn запускает ЕГО, а не
    Starlette-приложение напрямую -- см. модульный докстринг про /mcp без 307."""
    platforms_mod.configure(extra_paths=settings.platform_paths, upload_dir=settings.upload_dir)

    registry = IndexRegistry(settings.index_dir, auto_build=settings.auto_build)
    server_mod._registry = registry
    server_mod._db_limiter = anyio.CapacityLimiter(settings.db_threads)

    manager = StreamableHTTPSessionManager(
        app=server_mod.app,
        stateless=settings.http_stateless,
        json_response=settings.http_json_response,
        security_settings=_security_settings(settings),
        max_request_body_size=4 * 1024 * 1024,
    )

    panel_app = create_app(settings, registry, manager)

    async def dispatch(scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
            await manager.handle_request(scope, receive, send)
            return
        await panel_app(scope, receive, send)

    return dispatch


def run_http(settings):
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asgi = create_asgi(settings)
    uvicorn.run(asgi, host=settings.host, port=settings.port, log_level=settings.log_level)
