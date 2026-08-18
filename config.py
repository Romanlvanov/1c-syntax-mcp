"""Единая точка чтения конфигурации: переменные окружения + CLI.

server.py раньше не читал ни одной переменной окружения — INDEX_DIR был захардкожен
как Path(__file__).parent / "index" (см. docs/IMPROVEMENT_PLAN.md, Docker-раздел).
Это делало контейнеризацию невозможной: путь к индексу, каталог загруженных .hbk и
базовые пути поиска платформы 1С было нечем переопределить. Приоритет: CLI > env > дефолт.

Префикс переменных окружения SYNTAX_MCP_ согласован с уже существовавшим SYNTAX_MCP_DB
(tests/_server_loader.py) — его семантику (путь к готовому файлу индекса для тестов) не трогаем.
"""

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _env_list(name):
    raw = os.environ.get(name, "")
    return [p for p in (s.strip() for s in raw.split(",")) if p]


@dataclass(frozen=True)
class Settings:
    transport: str = "stdio"  # stdio | http
    host: str = "127.0.0.1"
    port: int = 8765

    index_dir: Path = field(default_factory=lambda: _REPO_ROOT / "index")
    upload_dir: Path = field(default_factory=lambda: _REPO_ROOT / "uploads")
    platform_paths: tuple = ()

    default_version: str = ""
    auto_build: bool = True

    allowed_hosts: tuple = ()
    allowed_origins: tuple = ()
    panel_token: str = ""

    http_stateless: bool = True
    http_json_response: bool = False
    max_upload_mb: int = 64
    db_threads: int = 8
    panel_enabled: bool = True

    log_level: str = "info"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings(argv=None) -> tuple:
    """Возвращает (Settings, argparse.Namespace). CLI-флаги переопределяют
    переменные окружения, которые переопределяют дефолты. Namespace возвращается
    отдельно от Settings, т.к. несёт CLI-only поля сборки индекса
    (build_index, platform, all, force), не относящиеся к рантайм-настройкам."""
    env_defaults = Settings(
        transport=os.environ.get("SYNTAX_MCP_TRANSPORT", "stdio"),
        host=os.environ.get("SYNTAX_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("SYNTAX_MCP_PORT", "8765")),
        index_dir=Path(os.environ.get("SYNTAX_MCP_INDEX_DIR", str(_REPO_ROOT / "index"))),
        upload_dir=Path(os.environ.get("SYNTAX_MCP_UPLOAD_DIR", str(_REPO_ROOT / "uploads"))),
        platform_paths=tuple(_env_list("SYNTAX_MCP_PLATFORM_PATHS")),
        default_version=os.environ.get("SYNTAX_MCP_DEFAULT_VERSION", ""),
        auto_build=_env_bool("SYNTAX_MCP_AUTO_BUILD", True),
        allowed_hosts=tuple(_env_list("SYNTAX_MCP_ALLOWED_HOSTS")),
        allowed_origins=tuple(_env_list("SYNTAX_MCP_ALLOWED_ORIGINS")),
        panel_token=os.environ.get("SYNTAX_MCP_PANEL_TOKEN", ""),
        http_stateless=_env_bool("SYNTAX_MCP_HTTP_STATELESS", True),
        http_json_response=_env_bool("SYNTAX_MCP_HTTP_JSON_RESPONSE", False),
        max_upload_mb=int(os.environ.get("SYNTAX_MCP_MAX_UPLOAD_MB", "64")),
        db_threads=int(os.environ.get("SYNTAX_MCP_DB_THREADS", "8")),
        panel_enabled=_env_bool("SYNTAX_MCP_PANEL_ENABLED", True),
        log_level=os.environ.get("SYNTAX_MCP_LOG_LEVEL", "info"),
    )

    parser = argparse.ArgumentParser(description="1C Syntax MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--index-dir", default=None)
    parser.add_argument("--upload-dir", default=None)
    parser.add_argument("--no-auto-build", action="store_true")

    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--platform", default=None, help="Собрать индекс только для этой версии")
    parser.add_argument("--all", action="store_true", help="Собрать индекс для всех версий со справкой")
    parser.add_argument("--force", action="store_true", help="Пересобрать, даже если индекс актуален")

    args = parser.parse_args(argv)

    settings = Settings(
        transport=args.transport or env_defaults.transport,
        host=args.host or env_defaults.host,
        port=args.port if args.port is not None else env_defaults.port,
        index_dir=Path(args.index_dir) if args.index_dir else env_defaults.index_dir,
        upload_dir=Path(args.upload_dir) if args.upload_dir else env_defaults.upload_dir,
        platform_paths=env_defaults.platform_paths,
        default_version=env_defaults.default_version,
        auto_build=False if args.no_auto_build else env_defaults.auto_build,
        allowed_hosts=env_defaults.allowed_hosts,
        allowed_origins=env_defaults.allowed_origins,
        panel_token=env_defaults.panel_token,
        http_stateless=env_defaults.http_stateless,
        http_json_response=env_defaults.http_json_response,
        max_upload_mb=env_defaults.max_upload_mb,
        db_threads=env_defaults.db_threads,
        panel_enabled=env_defaults.panel_enabled,
        log_level=env_defaults.log_level,
    )
    return settings, args
