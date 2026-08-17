#!/usr/bin/env python3
"""MCP сервер для проверки синтаксиса 1С."""

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from platforms import find_all_platforms, find_newest_with_help, find_platform
from syntax_db import SyntaxDB, build_index, diff_versions
from validate import check_call

app = Server("1c-syntax-mcp")

INDEX_DIR = Path(__file__).parent / "index"
_syntax_dbs: dict = {}  # версия -> SyntaxDB, ленивая загрузка при первом обращении
_default_version: Optional[str] = None


def _db_path_for(version: str) -> Path:
    return INDEX_DIR / version / "syntax_index.sqlite"


def _hbk_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_index(version: str, force: bool = False) -> bool:
    """Гарантирует наличие актуального индекса для указанной версии платформы.

    Union-индекс по нескольким версиям (docs/IMPROVEMENT_PLAN.md §5.2) в этой
    реализации не строится — каждая версия хранится отдельным файлом
    index/<версия>/syntax_index.sqlite; это проще и уже покрывает все критерии
    Этапа 3 (сборка по ≥2 версиям, diff, `since`). Экономия union (~66 МБ вместо
    ~966 МБ на 15 версий) становится значимой только если реально держать все
    версии одновременно собранными — не типичный сценарий использования.
    """
    platform = find_platform(version)
    if platform is None or not platform.has_help:
        print(f"Версия {version} не установлена или не имеет справки (shcntx_ru.hbk)", file=sys.stderr)
        return _db_path_for(version).exists()

    db_path = _db_path_for(version)
    hbk_path = platform.hbk_paths["shcntx_ru"]

    if not force and db_path.exists():
        try:
            probe = SyntaxDB(db_path)
            row = probe.conn.execute("SELECT hbk_sha256 FROM schema_meta LIMIT 1").fetchone()
            probe.close()
            if row and row[0] == _hbk_sha256(hbk_path):
                return True
        except Exception:
            pass  # индекс повреждён или собран старой схемой -- пересоберём

    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Сборка индекса {version} из {hbk_path}...", file=sys.stderr, flush=True)
    count = build_index(
        hbk_path, db_path,
        platform_version=version, hbk_sha256=_hbk_sha256(hbk_path),
        shquery_hbk_path=platform.hbk_paths.get("shquery_ru"),
        shlang_hbk_path=platform.hbk_paths.get("shlang_ru"),
    )
    print(f"Индекс {version} собран: {count} элементов -> {db_path}", file=sys.stderr, flush=True)
    return True


def get_db(version: Optional[str] = None) -> Optional[SyntaxDB]:
    """SyntaxDB для версии (по умолчанию — новейшая с уже собранным индексом).

    Индекс для НЕзапрошенной по умолчанию версии не строится неявно здесь —
    сборка остаётся отдельным явным шагом (`--build-index --platform=...`),
    а не побочным эффектом вызова инструмента."""
    version = version or _default_version
    if version is None:
        return None
    if version not in _syntax_dbs:
        db_path = _db_path_for(version)
        if not db_path.exists():
            return None
        _syntax_dbs[version] = SyntaxDB(db_path)
    return _syntax_dbs[version]


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


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Обработка вызовов инструментов."""
    if name == "list_versions":
        lines = ["Установленные версии платформы 1С:\n"]
        for platform in find_all_platforms():
            db_path = _db_path_for(platform.version)
            if not platform.has_help:
                status = "нет справки (тонкий клиент)"
            elif db_path.exists():
                try:
                    probe = SyntaxDB(db_path)
                    count = probe.conn.execute("SELECT count(*) FROM entry").fetchone()[0]
                    built_at = probe.conn.execute("SELECT built_at FROM schema_meta LIMIT 1").fetchone()[0]
                    probe.close()
                    status = f"проиндексировано, {count} элементов, собран {built_at}"
                except Exception:
                    status = "индекс повреждён или устарел — требуется пересборка"
            else:
                status = "справка есть, индекс не собран"
            default_mark = " [по умолчанию]" if platform.version == _default_version else ""
            lines.append(f"  · {platform.version}{default_mark} — {status}")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "diff_versions":
        from_version = arguments.get("from_version", "")
        to_version = arguments.get("to_version", "")
        db_a, db_b = _db_path_for(from_version), _db_path_for(to_version)
        missing = [v for v, p in ((from_version, db_a), (to_version, db_b)) if not p.exists()]
        if missing:
            return [TextContent(
                type="text",
                text=f"Индекс не собран для версий: {', '.join(missing)}. "
                f"Соберите: python server.py --build-index --platform=<версия>",
            )]
        d = diff_versions(db_a, db_b)
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

    syntax_db = get_db(arguments.get("platform"))
    if syntax_db is None:
        requested = arguments.get("platform") or _default_version
        return [TextContent(
            type="text",
            text=f"Индекс для версии {requested!r} не собран. "
            f"Соберите: python server.py --build-index" + (f" --platform={requested}" if requested else ""),
        )]

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


async def main():
    """Запуск MCP сервера."""
    global _default_version

    platform = find_newest_with_help()
    if platform is None:
        print("Не найдена установленная версия 1С со справкой (shcntx_ru.hbk)", file=sys.stderr)
        return
    _default_version = platform.version

    if not ensure_index(_default_version):
        print(f"Ошибка: индекс для {_default_version} не собран", file=sys.stderr)
        return

    db = get_db(_default_version)
    count = db.conn.execute("SELECT count(*) FROM entry").fetchone()[0]
    print(f"Индекс {_default_version} загружен: {count} элементов", file=sys.stderr, flush=True)

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def _cli_build_index():
    parser = argparse.ArgumentParser(description="Сборка индекса синтаксиса 1С")
    parser.add_argument("--build-index", action="store_true", required=True)
    parser.add_argument("--platform", help="Собрать индекс только для этой версии")
    parser.add_argument("--all", action="store_true", help="Собрать индекс для всех версий со справкой")
    parser.add_argument("--force", action="store_true", help="Пересобрать, даже если индекс актуален")
    args = parser.parse_args()

    if args.all:
        versions = [p.version for p in find_all_platforms() if p.has_help]
    elif args.platform:
        versions = [args.platform]
    else:
        newest = find_newest_with_help()
        if newest is None:
            print("Не найдена установленная версия 1С со справкой", file=sys.stderr)
            return
        versions = [newest.version]

    for version in versions:
        ensure_index(version, force=args.force)


if __name__ == "__main__":
    if "--build-index" in sys.argv:
        _cli_build_index()
    else:
        asyncio.run(main())
