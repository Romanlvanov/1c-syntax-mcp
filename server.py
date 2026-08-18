#!/usr/bin/env python3
"""MCP сервер для проверки синтаксиса 1С.

Поддерживает два транспорта: stdio (локальный запуск, см. README) и streamable
HTTP (контейнер, веб-панель — docs/IMPROVEMENT_PLAN.md, Docker-раздел). Состояние
собранных индексов и версия по умолчанию инкапсулированы в index_manager.IndexRegistry
вместо модульных глобалов — это позволяет безопасно обслуживать HTTP-запросы,
панель и фоновую сборку индекса из одного процесса одновременно."""

import asyncio
import sys

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import platforms as platforms_mod
from config import load_settings
from index_manager import IndexRegistry
from syntax_db import diff_versions as syntax_diff_versions
from validate import check_call

app = Server("1c-syntax-mcp")

_registry: IndexRegistry = None  # инициализируется в run_stdio()/webapp.run_http()
_db_limiter: anyio.CapacityLimiter = None  # ограничивает число потоков, обслуживающих БД


def _fmt_params(params):
    if not params:
        return ""
    lines = []
    for p in params:
        req = "обязательный" if p.get("required") else "необязательный"
        lines.append(f"  <{p.get('name', '')}> ({req}) — Тип: {p.get('type', '?')}. {p.get('description', '')}".rstrip())
    return "\n".join(lines)


def _fmt_entry_brief(item):
    owner = f" [{item['owner_ru']}]" if item.get("owner_ru") else ""
    lang = f" (язык: {item['language']})" if item.get("language") and item["language"] != "bsl" else ""
    return f"{item['name_ru']} / {item['name_en']}{owner}{lang}"


def _fmt_entry_full(item):
    lines = [f"=== {_fmt_entry_brief(item)} ===\n", f"Тип: {item['kind']}"]
    if item.get("owner_ru"):
        lines.append(f"Владелец: {item['owner_ru']} ({item.get('owner_en', '')})")
    if item.get("since"):
        lines.append(f"Доступен, начиная с версии: {item['since']}")
    if item.get("signature"):
        lines.append(f"\nСинтаксис:\n{item['signature']}")
    if item.get("params"):
        lines.append(f"\nПараметры:\n{_fmt_params(item['params'])}")
    if item.get("return_type"):
        lines.append(f"\nВозвращаемое значение:\n{item['return_type']}")
    if item.get("description"):
        lines.append(f"\nОписание:\n{item['description']}")
    if item.get("availability"):
        lines.append(f"\nДоступность:\n{item['availability']}")
    return "\n".join(lines)


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Список доступных инструментов."""
    return [
        Tool(
            name="search_syntax",
            description="Поиск функций, методов, свойств или объектов 1С по имени, "
            "владельцу или описанию (полнотекстовый поиск, русский или английский, "
            "встроенный язык BSL и язык запросов SDBL)",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Запрос: имя, фраза на естественном языке"},
                    "limit": {"type": "number", "description": "Максимум результатов (по умолчанию 10)"},
                    "kind": {"type": "string", "description": "Сузить по типу: object|method|property|event|ctor|sdbl|bsl_syntax"},
                    "owner": {"type": "string", "description": "Сузить до конкретного объекта-владельца"},
                    "language": {"type": "string", "description": "bsl|sdbl; по умолчанию ищет в обоих"},
                    "platform": {"type": "string", "description": "Версия платформы; по умолчанию новейшая с собранным индексом"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_function_info",
            description="Получить детальную информацию о функции, методе или свойстве 1С. "
            "Если имя неоднозначно (несколько объектов-владельцев), вернёт список кандидатов "
            "— уточните owner или используйте get_by_fqn",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Точное имя (русский или английский)"},
                    "owner": {"type": "string", "description": "Объект-владелец, если имя неоднозначно"},
                    "language": {"type": "string", "description": "bsl (по умолчанию) | sdbl"},
                    "platform": {"type": "string", "description": "Версия платформы; по умолчанию новейшая с собранным индексом"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="get_by_fqn",
            description="Получить детальную информацию по полному имени вида Объект.Метод "
            "— однозначная адресация без коллизий имён",
            inputSchema={
                "type": "object",
                "properties": {
                    "fqn": {"type": "string", "description": "Например: ЗаписьJSON.ЗаписатьНачалоОбъекта"},
                    "language": {"type": "string", "description": "bsl (по умолчанию) | sdbl"},
                    "platform": {"type": "string", "description": "Версия платформы; по умолчанию новейшая с собранным индексом"},
                },
                "required": ["fqn"],
            },
        ),
        Tool(
            name="suggest_completion",
            description="Предложить автодополнение по частичному имени функции",
            inputSchema={
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Начало имени (например: Стр, Str)"},
                    "limit": {"type": "number", "description": "Максимум предложений (по умолчанию 10)"},
                    "platform": {"type": "string", "description": "Версия платформы; по умолчанию новейшая с собранным индексом"},
                },
                "required": ["prefix"],
            },
        ),
        Tool(
            name="validate_syntax",
            description="Проверить вызов функции 1С: существование имени и, где это выводимо "
            "из переданного кода, арность вызова. Поддерживает разрешение вызовов через точку "
            "(Получатель.Метод), если тип получателя присвоен в этом же коде через "
            "«Х = Новый Тип(...)». ЧЕСТНО НЕ проверяет: реальный вывод типов, поток управления, "
            "объекты метаданных конфигурации — в этих случаях возвращает ok=null, а не гадает",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Код для проверки, например: СтрДлина(\"текст\") или Х = Новый Массив; Х.Добавить(1)"},
                    "language": {"type": "string", "description": "bsl (по умолчанию) | sdbl"},
                    "platform": {"type": "string", "description": "Версия платформы; по умолчанию новейшая с собранным индексом"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="list_versions",
            description="Список установленных версий платформы 1С: у каких есть справка, "
            "какие уже проиндексированы и сколько элементов в индексе",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="diff_versions",
            description="Сравнить две версии платформы: что добавлено/удалено/изменено в "
            "справке BSL между ними. Обе версии должны быть предварительно проиндексированы "
            "(python server.py --build-index --platform=<версия>)",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_version": {"type": "string", "description": "Например: 8.3.19.1264"},
                    "to_version": {"type": "string", "description": "Например: 8.5.1.1150"},
                },
                "required": ["from_version", "to_version"],
            },
        ),
    ]


def _no_index_message(requested):
    """Единый текст для обоих транспортов: локальный CLI-путь работает всегда,
    Docker-путь актуален только в контейнере, но упомянуть его безвредно и там,
    и там — лишняя строка не мешает локальному пользователю."""
    lines = (
        [f"Индекс для версии {requested!r} не собран."] if requested
        else ["Индекс не собран, версия платформы не определена."]
    )
    lines.append(
        "Локально: python server.py --build-index" + (f" --platform={requested}" if requested else "")
    )
    lines.append(
        "В Docker: откройте веб-панель сервера (корневой путь /) и загрузите shcntx_ru.hbk, "
        "либо смонтируйте каталог установки 1С в контейнер."
    )
    return "\n".join(lines)


def _call_tool_sync(name: str, arguments: dict) -> list[TextContent]:
    """Синхронная реализация — вызывается из потока (см. call_tool ниже), чтобы
    долгие операции (полный обход записей в diff_versions, построение fuzzy-индекса
    при первом запросе) не блокировали event loop при HTTP-транспорте, где тот же
    loop параллельно обслуживает веб-панель и загрузку .hbk. При stdio отдельный
    поток не обязателен (один клиент, вызовы и так последовательны), но одна
    реализация для обоих транспортов проще и безопаснее двух."""
    registry = _registry

    if name == "list_versions":
        lines = ["Установленные версии платформы 1С:\n"]
        for row in registry.list_state():
            if not row["has_help"] and not row["indexed"]:
                status = "нет справки (тонкий клиент)"
            elif row["indexed"] is True:
                status = f"проиндексировано, {row['entry_count']} элементов, собран {row['built_at']}"
            elif row["indexed"] == "error":
                status = "индекс повреждён или собран другой схемой — требуется пересборка"
            else:
                status = "справка есть, индекс не собран"
            source_mark = " (загружено)" if row["source"] == "upload" else ""
            default_mark = " [по умолчанию]" if row["is_default"] else ""
            lines.append(f"  · {row['version']}{default_mark}{source_mark} — {status}")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "diff_versions":
        from_version = arguments.get("from_version", "")
        to_version = arguments.get("to_version", "")
        db_a, db_b = registry.db_path_for(from_version), registry.db_path_for(to_version)
        missing = [v for v, p in ((from_version, db_a), (to_version, db_b)) if not p.exists()]
        if missing:
            return [TextContent(
                type="text",
                text=f"Индекс не собран для версий: {', '.join(missing)}. "
                f"Соберите: python server.py --build-index --platform=<версия>",
            )]
        d = syntax_diff_versions(db_a, db_b)
        lines = [
            f"Diff {from_version} -> {to_version}",
            f"Всего элементов: {d['total_a']} -> {d['total_b']}",
            f"Добавлено: {d['added_count']}  Удалено: {d['removed_count']}  Изменено: {d['changed_count']}\n",
        ]
        for label, items in (("Добавлено", d["added"]), ("Удалено", d["removed"]), ("Изменено", d["changed"])):
            lines.append(f"{label} (первые 20 из {len(items)}):")
            lines.extend(f"  · {x}" for x in items[:20])
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]

    syntax_db = registry.get_db(arguments.get("platform"))
    if syntax_db is None:
        requested = arguments.get("platform") or registry.get_default_version()
        return [TextContent(type="text", text=_no_index_message(requested))]

    if name == "search_syntax":
        query = arguments.get("query", "")
        limit = int(arguments.get("limit", 10))
        kind = arguments.get("kind") or None
        owner = arguments.get("owner") or None
        language = arguments.get("language") or None

        results = syntax_db.search(query, limit=limit, kind=kind, owner=owner, language=language)
        if not results:
            return [TextContent(type="text", text=f"Ничего не найдено по запросу: {query}")]

        lines = [f"Найдено результатов: {len(results)}\n"]
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. {_fmt_entry_brief(item)}")
            lines.append(f"   Тип: {item['kind']}  (совпадение: {item['tier']})")
            if item.get("signature"):
                lines.append(f"   Синтаксис: {item['signature']}")
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "get_function_info":
        func_name = arguments.get("name", "")
        owner = arguments.get("owner") or None
        language = arguments.get("language") or "bsl"
        item, candidates = syntax_db.get_by_name(func_name, owner=owner, language=language)

        if item is None and candidates:
            lines = [f"Найдено {len(candidates)} элементов с именем «{func_name}» — уточните owner:\n"]
            for c in candidates[:20]:
                lines.append(f"  · {c['owner_ru']}.{c['name_ru']}  [{c['kind']}]")
            if len(candidates) > 20:
                lines.append(f"  … и ещё {len(candidates) - 20}")
            lines.append("\nПовторите запрос с параметром owner или вызовите get_by_fqn.")
            return [TextContent(type="text", text="\n".join(lines))]

        if item is None:
            return [TextContent(type="text", text=f"Функция не найдена: {func_name}")]

        return [TextContent(type="text", text=_fmt_entry_full(item))]

    if name == "get_by_fqn":
        fqn = arguments.get("fqn", "")
        language = arguments.get("language") or "bsl"
        item = syntax_db.get_by_fqn(fqn, language=language)
        if item is None:
            return [TextContent(type="text", text=f"Не найдено по FQN: {fqn}")]
        return [TextContent(type="text", text=_fmt_entry_full(item))]

    if name == "suggest_completion":
        prefix = arguments.get("prefix", "")
        limit = int(arguments.get("limit", 10))
        suggestions = syntax_db.suggest_completions(prefix, limit=limit)
        if not suggestions:
            return [TextContent(type="text", text=f"Нет предложений для: {prefix}")]
        lines = [f"Предложения для '{prefix}':\n"]
        for i, s in enumerate(suggestions, 1):
            owner = f", владелец: {s['owner_ru']}" if s.get("owner_ru") else ""
            lines.append(f"{i}. {s['name_ru']} ({s['kind']}{owner})")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "validate_syntax":
        code = arguments.get("code", "")
        language = arguments.get("language") or "bsl"
        result = check_call(syntax_db, code, language=language)
        mark = {"True": "✓", "False": "❌", "None": "⚠"}[str(result["ok"])]
        lines = [f"{mark} {result['note']}"]
        if result.get("signature"):
            lines.append(f"\nСинтаксис:\n{result['signature']}")
        return [TextContent(type="text", text="\n".join(lines))]

    return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Обработка вызовов инструментов — в потоке из общего пула (см. _call_tool_sync)."""
    return await anyio.to_thread.run_sync(_call_tool_sync, name, arguments, limiter=_db_limiter)


def _configure_platforms(settings):
    platforms_mod.configure(extra_paths=settings.platform_paths, upload_dir=settings.upload_dir)


def _ensure_default_built(registry, settings, transport_label):
    """Гарантирует индекс для версии по умолчанию, если авто-сборка включена;
    иначе только сообщает о недостающем индексе. Возвращает выбранную версию
    (может быть None, если платформ не найдено и готового индекса тоже нет —
    сервер в HTTP-режиме всё равно поднимается, старое поведение stdio-режима
    (print+return при отсутствии платформы) здесь недопустимо: панель для
    загрузки .hbk нужна именно в этом случае)."""
    version = registry.resolve_default(forced_version=settings.default_version)
    if version is None:
        print("Установленных версий 1С со справкой не найдено, готового индекса тоже нет. "
              "Соберите индекс явно или, в Docker, загрузите shcntx_ru.hbk через веб-панель.",
              file=sys.stderr, flush=True)
        return None

    if not registry.has_built_index(version):
        if settings.auto_build:
            try:
                registry.build_version(version)
            except Exception as e:
                print(f"Не удалось собрать индекс {version}: {e}", file=sys.stderr, flush=True)
                return version
        else:
            print(f"Индекс для {version} не собран, автосборка выключена ({transport_label}).",
                  file=sys.stderr, flush=True)
            return version

    if registry.has_built_index(version):
        db = registry.get_db(version)
        count = db.conn.execute("SELECT count(*) FROM entry").fetchone()[0]
        print(f"Индекс {version} загружен: {count} элементов", file=sys.stderr, flush=True)
    return version


async def run_stdio(settings):
    global _registry, _db_limiter
    _configure_platforms(settings)
    _registry = IndexRegistry(settings.index_dir, auto_build=settings.auto_build)
    _db_limiter = anyio.CapacityLimiter(settings.db_threads)

    _ensure_default_built(_registry, settings, "stdio")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def _cli_build_index(settings, args):
    """Отдельная явная команда сборки индекса — не побочный эффект запуска сервера."""
    _configure_platforms(settings)
    registry = IndexRegistry(settings.index_dir, auto_build=False)

    if args.all:
        versions = [p.version for p in platforms_mod.find_all_platforms() if p.has_help]
    elif args.platform:
        versions = [args.platform]
    else:
        newest = platforms_mod.find_newest_with_help()
        if newest is None:
            print("Не найдена установленная версия 1С со справкой", file=sys.stderr)
            return 1
        versions = [newest.version]

    exit_code = 0
    for version in versions:
        try:
            result = registry.build_version(version, force=args.force)
            if result["rebuilt"]:
                print(f"Индекс {version} собран: {result['count']} элементов -> {result['path']}",
                      file=sys.stderr, flush=True)
            else:
                print(f"Индекс {version} актуален, пересборка не требуется", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"Ошибка сборки индекса {version}: {e}", file=sys.stderr, flush=True)
            exit_code = 1
    return exit_code


def main():
    settings, args = load_settings()

    if args.build_index:
        sys.exit(_cli_build_index(settings, args) or 0)

    if settings.transport == "http":
        from webapp import run_http  # ленивый импорт: HTTP-стек не нужен для stdio/CLI
        run_http(settings)
    else:
        asyncio.run(run_stdio(settings))


if __name__ == "__main__":
    main()
