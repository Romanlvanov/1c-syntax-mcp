"""Обнаружение установленных версий платформы 1С.

В отличие от исходной реализации server.py (которая брала только последнюю
найденную версию и падала, если у неё нет справки), здесь перечисляются ВСЕ
установленные версии — на машине аудита 4 из 19 версий оказались тонким клиентом
без `shcntx_ru.hbk` вовсе (docs/IMPROVEMENT_PLAN.md, раздел 5.2/6.5).

Реестра версий в 1С нет: `HKLM\\SOFTWARE\\1C` на Windows не существует даже при
множестве установленных версий (проверено на машине аудита) — источник истины
только файловая система.
"""

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


class Platform:
    """Одна установленная версия платформы, найденная на диске."""

    def __init__(self, version, install_path):
        self.version = version
        self.install_path = install_path
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
        return f"Platform({self.version!r}, has_help={self.has_help})"


def find_all_platforms():
    """Все установленные версии платформы, отсортированные от новой к старой."""
    platforms = []
    for base in _BASE_PATHS:
        if not base.exists():
            continue
        for item in base.iterdir():
            if not item.is_dir() or not item.name[:1].isdigit():
                continue
            platforms.append(Platform(item.name, item))
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
