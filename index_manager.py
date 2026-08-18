"""Управление жизненным циклом собранных индексов: кеш SyntaxDB, версия по
умолчанию, безопасная (атомарная) пересборка, статусы фоновых задач для
веб-панели.

Раньше это состояние жило в трёх модульных глобалах server.py (INDEX_DIR,
_syntax_dbs, _default_version, docs/IMPROVEMENT_PLAN.md Docker-раздел) —
нерасширяемо и небезопасно при параллельных HTTP-запросах (панель, MCP,
фоновая сборка — один процесс). IndexRegistry инкапсулирует это состояние
под одним RLock и не завязано на конкретный транспорт: используется
одинаково из stdio- и HTTP-режимов server.py.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from platforms import find_all_platforms, find_platform
from syntax_db import SyntaxDB, build_index

_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def is_valid_version(version: str) -> bool:
    """Версия платформы имеет вид '8.5.1.1150'. Внутри .hbk версии нет (см.
    docs/IMPROVEMENT_PLAN.md, Docker-раздел — элемент Book содержит только тип
    книги), поэтому при загрузке через веб-панель пользователь вводит её
    вручную — а версия становится именем каталога на диске, значит нужна
    защита от path traversal, а не только "красивого формата"."""
    return bool(version) and ".." not in version and bool(_VERSION_RE.match(version))


def _hash_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hbk_fingerprint(path):
    """{size, mtime_ns, sha256} с кешем в сайдкар-файле рядом с .hbk.

    Полный SHA-256 (39 МБ для shcntx_ru.hbk) пересчитывается только если
    size/mtime_ns разошлись с кешем — раньше он считался заново при КАЖДОМ
    старте сервера (server.py, старый _hbk_sha256 в ensure_index), что на
    bind-mount каталога установки через WSL2 заметно тормозит старт контейнера.
    """
    path = Path(path)
    st = path.stat()
    sidecar = path.with_name(path.name + ".fingerprint.json")
    cached = None
    if sidecar.exists():
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns:
        return cached
    fingerprint = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _hash_file(path)}
    try:
        sidecar.write_text(json.dumps(fingerprint), encoding="utf-8")
    except OSError:
        pass  # каталог только для чтения (например, bind-mount :ro) -- не критично, посчитаем заново в следующий раз
    return fingerprint


class JobRegistry:
    """Статусы фоновых задач (сборка индекса, запущенная из веб-панели).
    Обычный словарь под локом — реальная частота таких операций единицы в час,
    отдельная инфраструктура очередей не нужна."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def create(self, kind, version):
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "kind": kind,
                "version": version,
                "status": "running",
                "phase": None,
                "done": 0,
                "total": 0,
                "error": None,
                "result": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        return job_id

    def progress(self, job_id, phase, done, total):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(phase=phase, done=done, total=total, updated_at=time.time())

    def finish(self, job_id, result=None):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(status="done", result=result, updated_at=time.time())

    def fail(self, job_id, error):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(status="error", error=str(error), updated_at=time.time())

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def list_all(self):
        with self._lock:
            return [dict(j) for j in self._jobs.values()]


class IndexRegistry:
    """Единая точка доступа к собранным индексам. Один экземпляр на процесс сервера."""

    def __init__(self, index_dir, auto_build=True):
        self.index_dir = Path(index_dir)
        self.auto_build = auto_build
        self._lock = threading.RLock()
        self._dbs = {}       # версия -> SyntaxDB
        self._db_stat = {}   # версия -> (mtime_ns, size, ino) на момент открытия
        self._default_version = None
        self.jobs = JobRegistry()

    def db_path_for(self, version):
        return self.index_dir / version / "syntax_index.sqlite"

    def has_built_index(self, version):
        return self.db_path_for(version).exists()

    def list_built_versions(self):
        """Версии с уже собранным индексом на диске — в т.ч. те, для которых
        исходная установка 1С сейчас недоступна (например индекс собран
        раньше, а bind-mount временно отключён)."""
        if not self.index_dir.is_dir():
            return []
        versions = []
        for item in self.index_dir.iterdir():
            if item.is_dir() and (item / "syntax_index.sqlite").exists():
                versions.append(item.name)
        return versions

    @staticmethod
    def _sort_key(version):
        try:
            return tuple(int(p) for p in version.split("."))
        except ValueError:
            return (0,)

    def get_db(self, version=None):
        """Кешированный SyntaxDB для версии (по умолчанию — текущая версия по
        умолчанию). Индекс для НЕзапрошенной версии не строится неявно здесь —
        сборка остаётся отдельным явным шагом.

        Самовосстановление: если файл индекса заменили снаружи (docker exec
        --build-index при работающем сервере, сборка через панель другим
        воркером) — переоткрываем вместо того, чтобы годами отдавать
        устаревшие данные из уже открытых read-only соединений."""
        version = version or self.get_default_version()
        if version is None:
            return None
        with self._lock:
            db = self._dbs.get(version)
            if db is not None:
                try:
                    st = os.stat(db.db_path)
                    fresh = (st.st_mtime_ns, st.st_size, st.st_ino)
                except OSError:
                    fresh = None
                if fresh is None or fresh != self._db_stat.get(version):
                    db.close()
                    self._dbs.pop(version, None)
                    self._db_stat.pop(version, None)
                    db = None
            if db is None:
                path = self.db_path_for(version)
                if not path.exists():
                    return None
                db = SyntaxDB(path)
                st = os.stat(path)
                self._dbs[version] = db
                self._db_stat[version] = (st.st_mtime_ns, st.st_size, st.st_ino)
            return db

    def evict(self, version):
        with self._lock:
            db = self._dbs.pop(version, None)
            self._db_stat.pop(version, None)
        if db is not None:
            db.close()

    def get_default_version(self):
        with self._lock:
            current = self._default_version
        if current and self.has_built_index(current):
            return current
        return self.resolve_default()

    def set_default(self, version):
        if not self.has_built_index(version):
            raise ValueError(f"индекс для версии {version!r} не собран")
        with self._lock:
            self._default_version = version

    def resolve_default(self, forced_version=""):
        """Порядок выбора версии по умолчанию (docs/IMPROVEMENT_PLAN.md,
        Docker-раздел):
        1. принудительно заданная версия (SYNTAX_MCP_DEFAULT_VERSION), если
           для неё есть индекс или хотя бы исходный .hbk;
        2. новейшая платформа со справкой (включая загруженные через панель);
        3. новейший уже собранный индекс в INDEX_DIR — делает рабочим
           контейнер с томом данных, но без установленной 1С;
        4. None — сервер всё равно поднимается, инструменты вернут инструкцию,
           вместо того чтобы падать (старое поведение server.py: print+return
           при отсутствии платформы означало, что HTTP-сервер с веб-панелью
           для загрузки .hbk вообще не мог подняться)."""
        with self._lock:
            if forced_version:
                if self.has_built_index(forced_version):
                    self._default_version = forced_version
                    return forced_version
                platform = find_platform(forced_version)
                if platform is not None and platform.has_help:
                    self._default_version = forced_version
                    return forced_version

            for p in find_all_platforms():
                if p.has_help:
                    self._default_version = p.version
                    return p.version

            built = sorted(self.list_built_versions(), key=self._sort_key, reverse=True)
            if built:
                self._default_version = built[0]
                return built[0]

            self._default_version = None
            return None

    def list_state(self):
        """Объединённый список версий для list_versions/панели: платформы
        (установленные + загруженные) и уже собранные индексы, даже если
        платформа для индекса сейчас не видна на диске."""
        rows = {}
        for p in find_all_platforms():
            rows[p.version] = {
                "version": p.version,
                "source": p.source,
                "has_help": p.has_help,
                "hbk_kinds": sorted(p.hbk_paths.keys()),
                "indexed": False,
                "entry_count": None,
                "built_at": None,
            }
        for version in self.list_built_versions():
            row = rows.setdefault(version, {
                "version": version, "source": "unknown", "has_help": False,
                "hbk_kinds": [], "indexed": False, "entry_count": None, "built_at": None,
            })
            try:
                db = SyntaxDB(self.db_path_for(version))
                try:
                    row["indexed"] = True
                    row["entry_count"] = db.conn.execute("SELECT count(*) FROM entry").fetchone()[0]
                    row["built_at"] = db.conn.execute(
                        "SELECT built_at FROM schema_meta LIMIT 1"
                    ).fetchone()[0]
                finally:
                    db.close()
            except Exception as e:
                row["indexed"] = "error"
                row["error"] = str(e)

        default_version = self.get_default_version()
        result = sorted(rows.values(), key=lambda r: self._sort_key(r["version"]), reverse=True)
        for row in result:
            row["is_default"] = row["version"] == default_version
        return result

    @staticmethod
    def _validate_built(tmp_path):
        """Провал здесь не даёт заменить рабочий индекс: несовпадение схемы
        (проверяется уже в SyntaxDB.__init__), пустой результат сборки или
        неработающий FTS5 (MATCH упадёт OperationalError, если sqlite3 в этом
        окружении собран без FTS5 — тогда сборка образа должна была упасть
        раньше на отдельной проверке, но проверка здесь — вторая линия)."""
        db = SyntaxDB(tmp_path)
        try:
            count = db.conn.execute("SELECT count(*) FROM entry").fetchone()[0]
            if count == 0:
                raise RuntimeError("собранный индекс пуст (0 записей)")
            # функциональная проверка FTS5, а не только успешный CREATE: та же
            # OperationalError, что молча проглатывается в syntax_db.py при
            # обычном поиске (except sqlite3.OperationalError) -- здесь она
            # обязана всплыть и провалить сборку, а не деградировать тихо.
            # Содержимое запроса неважно (нужен любой MATCH, не конкретное
            # совпадение) -- "а"/"a" гарантированно валидны как префиксы.
            db.conn.execute("SELECT rowid FROM fts_doc WHERE fts_doc MATCH ? LIMIT 1", ('"а"* OR "a"*',))
        finally:
            db.close()

    def build_version(self, version, force=False, progress=None):
        """Безопасная пересборка: build_index() пишет во временный файл рядом
        с рабочим индексом; старый индекс не тронут до успешной валидации,
        замена атомарна (os.replace). Раньше build_index удалял старый файл
        ПЕРВОЙ строкой (docs/IMPROVEMENT_PLAN.md, найденная ошибка) — битая
        загрузка через веб-панель могла уничтожить рабочий индекс. Здесь этот
        разрыв закрыт на уровне оркестрации, без изменения самого build_index
        (там своя, отдельная правка: open_hbk выполняется до os.remove)."""
        platform = find_platform(version)
        if platform is None or not platform.has_help:
            raise ValueError(f"версия {version!r} не установлена или не имеет справки (shcntx_ru.hbk)")

        db_path = self.db_path_for(version)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = db_path.parent / f"syntax_index.sqlite.building-{os.getpid()}-{uuid.uuid4().hex[:8]}"

        hbk_path = platform.hbk_paths["shcntx_ru"]
        fingerprint = hbk_fingerprint(hbk_path)

        if not force and db_path.exists():
            try:
                probe = SyntaxDB(db_path)
                try:
                    row = probe.conn.execute("SELECT hbk_sha256 FROM schema_meta LIMIT 1").fetchone()
                finally:
                    probe.close()
                if row and row[0] == fingerprint["sha256"]:
                    return {"rebuilt": False, "version": version, "path": str(db_path)}
            except Exception:
                pass  # индекс повреждён или собран старой схемой -- пересоберём

        try:
            count = build_index(
                hbk_path, tmp_path,
                platform_version=version, hbk_sha256=fingerprint["sha256"],
                shquery_hbk_path=platform.hbk_paths.get("shquery_ru"),
                shlang_hbk_path=platform.hbk_paths.get("shlang_ru"),
                progress=progress,
            )
            self._validate_built(tmp_path)
            self.evict(version)  # закрыть все соединения ДО замены файла -- обязательно на Windows
            os.replace(tmp_path, db_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        with self._lock:
            if self._default_version is None:
                self._default_version = version

        return {"rebuilt": True, "version": version, "path": str(db_path), "count": count}

    def delete_version(self, version, with_files=False):
        """Удаляет собранный индекс (и, опционально, загруженные .hbk —
        только если версия пришла из каталога загрузок, не из системной
        установки)."""
        self.evict(version)
        db_path = self.db_path_for(version)
        if db_path.exists():
            db_path.unlink()
            sidecar = db_path.parent
            try:
                sidecar.rmdir()
            except OSError:
                pass  # каталог не пуст -- нормально, там могут быть другие файлы
        with self._lock:
            if self._default_version == version:
                self._default_version = None

        if with_files:
            platform = find_platform(version)
            if platform is not None and platform.source == "upload":
                for hbk_path in platform.hbk_paths.values():
                    hbk_path.unlink(missing_ok=True)
                    Path(str(hbk_path) + ".fingerprint.json").unlink(missing_ok=True)
                try:
                    platform.install_path.joinpath("bin").rmdir()
                    platform.install_path.rmdir()
                except OSError:
                    pass

    def close_all(self):
        with self._lock:
            dbs, self._dbs = list(self._dbs.values()), {}
            self._db_stat = {}
        for db in dbs:
            db.close()
