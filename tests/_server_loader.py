"""Вспомогательные функции для тестов: загрузка server.py и поиск собранного индекса.

server.py на верхнем уровне модуля делает `from mcp.server import Server` — если
пакет `mcp` не установлен, обычный `import server` падает ещё до того, как мы
доберёмся до кода, который для большинства тестов пакет mcp вообще не использует.
Этот модуль подставляет в sys.modules минимальные заглушки ТОЛЬКО если настоящего
пакета mcp нет, и только на время импорта server.py.
"""

import importlib
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_mcp_stub_if_missing():
    try:
        import mcp  # noqa: F401

        return False  # настоящий пакет есть — ничего не подменяем
    except ImportError:
        pass

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, *a, **kw):
            return self

        def list_tools(self):
            return lambda fn: fn

        def call_tool(self):
            return lambda fn: fn

    mcp_mod = types.ModuleType("mcp")
    mcp_server_mod = types.ModuleType("mcp.server")
    mcp_server_stdio_mod = types.ModuleType("mcp.server.stdio")
    mcp_types_mod = types.ModuleType("mcp.types")

    mcp_server_mod.Server = _Stub
    mcp_server_stdio_mod.stdio_server = _Stub
    mcp_types_mod.Tool = _Stub
    mcp_types_mod.TextContent = _Stub

    mcp_mod.server = mcp_server_mod
    sys.modules.setdefault("mcp", mcp_mod)
    sys.modules.setdefault("mcp.server", mcp_server_mod)
    sys.modules.setdefault("mcp.server.stdio", mcp_server_stdio_mod)
    sys.modules.setdefault("mcp.types", mcp_types_mod)
    return True


def load_server_module():
    """Импортирует server.py из корня репозитория и возвращает модуль."""
    _install_mcp_stub_if_missing()
    sys.path.insert(0, str(REPO_ROOT))
    try:
        if "server" in sys.modules:
            importlib.reload(sys.modules["server"])
        return importlib.import_module("server")
    finally:
        sys.path.remove(str(REPO_ROOT))


def find_syntax_index_db():
    """Возвращает Path к собранному индексу новейшей версии платформы, иначе None.

    С Этапа 3 индекс лежит в index/<версия>/syntax_index.sqlite (один файл на
    версию платформы, docs/IMPROVEMENT_PLAN.md §8). Намеренно НЕ строит индекс
    сам: сборка требует установленной 1С, занимает секунды и создаёт файл на
    диске — тесты не должны делать это неявно. Собрать явно:
    `python server.py --build-index`.
    """
    env_path = os.environ.get("SYNTAX_MCP_DB")
    if env_path:
        p = Path(env_path)
        return p if p.exists() else None

    index_dir = REPO_ROOT / "index"
    if not index_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in index_dir.glob("*/syntax_index.sqlite")),
        key=lambda p: tuple(int(x) for x in p.parent.name.split(".") if x.isdigit()),
        reverse=True,
    )
    return candidates[0] if candidates else None
