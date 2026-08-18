"""Обнаружение установленных версий платформы 1С.

В отличие от исходной реализации server.py (которая брала только последнюю
найденную версию и падала, если у неё нет справки), здесь перечисляются ВСЕ
установленные версии — на машине аудита 4 из 19 версий оказались тонким клиентом
без `shcntx_ru.hbk` вовсе (docs/IMPROVEMENT_PLAN.md, раздел 5.2/6.5).

Реестра версий в 1С нет: `HKLM\\SOFTWARE\\1C` на Windows не существует даже при
множестве установленных версий (проверено на машине аудита) — источник истины
только файловая система.

Для Docker-развёртывания (docs/IMPROVEMENT_PLAN.md, Docker-раздел) добавлена
configure(): каталог загруженных через веб-панель .hbk и дополнительные пути из
SYNTAX_MCP_PLATFORM_PATHS сканируются наравне со штатными путями установки.
Ключевое решение: раскладка внутри каталога загрузок идентична настоящей
установке (`<версия>/bin/<имя>.hbk`), поэтому это не отдельный кодовый путь, а
ещё одна база в том же списке — find_all_platforms/find_platform/ensure-логика
не меняются вовсе. configure() без вызова не меняет поведение (default — только
штатные пути, как раньше)."""

import os
from pathlib import Path

if os.name == "nt":
    _BASE_PATHS = [
        Path(r"C:\Program Files\1cv8"),
        Path(r"C:\Program Files (x86)\1cv8"),
        Path(r"C:\Program Files\1C\1CE\1cv8"),
    ]
else:
    _BASE_PATHS = [
        Path("/opt/1cv8/x86_64"),
        Path("/opt/1C/v8.3/x86_64"),
        Path("/opt/1C/v8.5/x86_64"),
    ]

_HBK_NAMES = ("shcntx_ru", "shquery_ru", "shlang_ru", "shclang_ru", "shcntx_root")

# [(путь, source), ...] в порядке приоритета при дедупе версий. По умолчанию —
# только штатные пути установки, как в исходной реализации; configure()
# добавляет каталог загрузок ПЕРВЫМ (явная загрузка пользователя перекрывает
# одноимённую установку) и дополнительные пути ПОСЛЕДНИМИ.
_configured_bases = [(p, "install") for p in _BASE_PATHS]


def configure(extra_paths=(), upload_dir=None):
    """Вызывается один раз при старте сервера (server.py, из config.Settings).
    upload_dir — куда веб-панель кладёт загруженные .hbk; extra_paths —
    SYNTAX_MCP_PLATFORM_PATHS."""
    global _configured_bases
    bases = []
    if upload_dir is not None:
        bases.append((Path(upload_dir), "upload"))
    bases.extend((p, "install") for p in _BASE_PATHS)
    bases.extend((Path(p), "install") for p in extra_paths)
    _configured_bases = bases


class Platform:
    """Одна установленная версия платформы, найденная на диске."""

    def __init__(self, version, install_path, source="install"):
        self.version = version
        self.install_path = install_path
        self.source = source  # "install" (штатный путь) | "upload" (веб-панель)
        self.hbk_paths = {}
        bin_dir = install_path / "bin"
        for stem in _HBK_NAMES:
            p = bin_dir / f"{stem}.hbk"
            if p.exists():
                self.hbk_paths[stem] = p

    @property
    def has_help(self):
        return "shcntx_ru" in self.hbk_paths

    @property
    def sort_key(self):
        try:
            return tuple(int(p) for p in self.version.split("."))
        except ValueError:
            return (0,)

    def __repr__(self):
        return f"Platform({self.version!r}, has_help={self.has_help}, source={self.source!r})"


def find_all_platforms():
    """Все установленные версии платформы (включая загруженные через веб-панель
    и настроенные дополнительные пути — см. configure()), отсортированные от
    новой к старой. При совпадении версии в нескольких базах побеждает первая
    по приоритету списка _configured_bases (обычно каталог загрузок)."""
    seen_versions = set()
    platforms = []
    for base, source in _configured_bases:
        if not base.exists():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir() or not item.name[:1].isdigit():
                continue
            if item.name in seen_versions:
                continue
            seen_versions.add(item.name)
            platforms.append(Platform(item.name, item, source=source))
    platforms.sort(key=lambda p: p.sort_key, reverse=True)
    return platforms


def find_newest_with_help():
    """Первая версия (от новой к старой), у которой есть shcntx_ru.hbk, или None."""
    for platform in find_all_platforms():
        if platform.has_help:
            return platform
    return None


def find_platform(version):
    """Ищет конкретную версию по строке; None, если не установлена."""
    for platform in find_all_platforms():
        if platform.version == version:
            return platform
    return None
