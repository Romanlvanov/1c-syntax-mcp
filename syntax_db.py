"""SQLite+FTS5 индекс справки 1С — замена syntax_tree.json и SyntaxIndex.

Схема и обоснование — docs/IMPROVEMENT_PLAN.md, разделы 5.5-5.7 и 6.1-6.3.
Реализованы Этапы 1 (данные, FTS5, алиасы, дизамбигуация) и 2 (нормализация,
раскладка, гомоглифы, fuzzy). Union нескольких версий платформы — Этап 3, язык
запросов — Этап 4.

Что чинит относительно старого server.py (см. docs/IMPROVEMENT_PLAN.md, раздел 3):
  - свойства и события больше не теряются (были "propertie" != "property", и
    events не обрабатывались вовсе);
  - секция "Параметры" не пустая — structured-парсинг через html_parser;
  - рубрики каталога (catalogNNN) не индексируются как объекты;
  - поиск ранжирован по строгим уровням (точное имя -> алиасы -> раскладка/
    гомоглифы -> fuzzy -> bm25), а не substring-скан с break;
  - get_by_name возвращает СПИСОК кандидатов при коллизии владельцев, а не items[0];
  - точное совпадение работает для ЛЮБОГО регистра кириллицы (см. ниже).

Отдельно найденная и здесь же исправленная ошибка: SQLite-коллация `NOCASE`
складывает регистр только для ASCII, а не для кириллицы — `'СтрДлина' = 'стрдлина'
COLLATE NOCASE` даёт false. Поэтому сравнение имён идёт не через COLLATE, а через
заранее вычисленные при сборке индекса колонки `*_norm` (normalization.norm(),
одна и та же функция и при индексации, и при запросе).
"""

import datetime
import json
import os
import re
import sqlite3
from pathlib import PurePosixPath

from rapidfuzz import fuzz, process

from aliases import rows as alias_rows
from hbk_reader import open_hbk
from html_parser import parse_page, parse_simple_page
from morphology import stem_text, stem_tokens
from normalization import is_latin_only, norm, qwerty_to_jcuken, skeleton
from segmentation import index_tokens

SCHEMA_VERSION = 4

_CATEGORY_DIRS = {"methods": "method", "properties": "property", "ctors": "ctor", "events": "event"}
_CATALOG_RUBRIC_RE = re.compile(r"^catalog\d+$")

# закрытая форма: k правок на n-символьном имени дают ratio = 100(1-k/n);
# при n<7 однобуквенная опечатка неотличима от другого настоящего имени — не гадаем
_FUZZY_MIN_NAME_LEN = 7
_FUZZY_SCORE_CUTOFF = 85

_DDL = """
CREATE TABLE schema_meta(
  index_schema_version INTEGER NOT NULL,
  platform_version TEXT,
  hbk_sha256 TEXT,
  built_at TEXT
);

CREATE TABLE entry(
  entry_id      INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,        -- object|method|property|event|ctor|sdbl|bsl_syntax
  language      TEXT NOT NULL DEFAULT 'bsl',  -- bsl|sdbl, docs/IMPROVEMENT_PLAN.md §5.4
  owner_ru      TEXT NOT NULL DEFAULT '',
  owner_en      TEXT NOT NULL DEFAULT '',
  owner_ru_norm TEXT NOT NULL DEFAULT '',
  name_ru       TEXT NOT NULL,
  name_en       TEXT NOT NULL DEFAULT '',
  name_ru_norm  TEXT NOT NULL DEFAULT '',
  name_en_norm  TEXT NOT NULL DEFAULT '',
  name_ru_skel  TEXT NOT NULL DEFAULT '',  -- гомоглиф-скелет, docs/IMPROVEMENT_PLAN.md §6.3
  name_en_skel  TEXT NOT NULL DEFAULT '',
  fqn           TEXT NOT NULL,
  fqn_norm      TEXT NOT NULL DEFAULT '',
  signature     TEXT NOT NULL DEFAULT '',
  params_json   TEXT NOT NULL DEFAULT '[]',
  return_type   TEXT NOT NULL DEFAULT '',
  description   TEXT NOT NULL DEFAULT '',
  availability  TEXT NOT NULL DEFAULT '',
  since         TEXT NOT NULL DEFAULT '',
  name_tokens   TEXT NOT NULL DEFAULT '',
  descr_stem    TEXT NOT NULL DEFAULT ''  -- Snowball-основы описания, docs/IMPROVEMENT_PLAN.md §6.2
);
-- обычные (не NOCASE) индексы: сравнение всегда идёт по уже нормализованным колонкам
CREATE INDEX ix_entry_name_ru_norm ON entry(name_ru_norm);
CREATE INDEX ix_entry_name_en_norm ON entry(name_en_norm);
CREATE INDEX ix_entry_fqn_norm     ON entry(fqn_norm);
CREATE INDEX ix_entry_owner_norm   ON entry(owner_ru_norm);
CREATE INDEX ix_entry_name_ru_skel ON entry(name_ru_skel);
CREATE INDEX ix_entry_name_en_skel ON entry(name_en_skel);
CREATE INDEX ix_entry_language     ON entry(language);

-- имена + сегменты, с префиксным индексом -- suggest_completion
CREATE VIRTUAL TABLE fts_name USING fts5(
  name_ru, name_en, name_tokens,
  content='entry', content_rowid='entry_id',
  tokenize="unicode61 remove_diacritics 2", prefix='2 3 4'
);

-- полный текст без префиксного индекса -- search_syntax; descr_stem -- отдельная
-- стеммированная колонка (см. morphology.py), а не замена исходного текста
CREATE VIRTUAL TABLE fts_doc USING fts5(
  name_ru, name_en, name_tokens, signature, description, descr_stem,
  content='entry', content_rowid='entry_id',
  tokenize="unicode61 remove_diacritics 2"
);

-- phrase хранится уже нормализованной (aliases.py) -- сравнение через '=', не COLLATE
CREATE TABLE alias(phrase TEXT NOT NULL, target_name TEXT NOT NULL);
CREATE INDEX ix_alias_phrase ON alias(phrase);
"""

_BM25_WEIGHTS_DOC = (10.0, 6.0, 6.0, 2.0, 1.0, 0.5)  # name_ru,name_en,name_tokens,signature,description,descr_stem

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
_STOPWORDS = frozenset("как что не в на и или для с по из к у о а но это".split())

# приоритет типов при равном ранге автодополнения: функции/объекты чаще нужны при
# наборе кода, чем совпавшее по префиксу значение перечисления/свойство-тёзка
_KIND_PRIORITY = {"method": 0, "object": 1, "ctor": 2, "property": 3, "event": 4}


def _query_tokens(text):
    return [t for t in _TOKEN_RE.findall(norm(text)) if t not in _STOPWORDS and len(t) > 1]


def _build_match(tokens, mode="OR"):
    if not tokens:
        return None
    quoted = ['"' + t.replace('"', '""') + '"*' for t in tokens]
    return f" {mode} ".join(quoted)


def _implied_directories(namelist):
    """Множество путей-директорий, выведенных из полных путей файлов в архиве."""
    dirs = set()
    for path in namelist:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return dirs


def _classify_member_path(path):
    """objects/.../<owner>/methods/.../Name.st -> (kind, owner_dir) | None."""
    parts = path.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in _CATEGORY_DIRS:
            return _CATEGORY_DIRS[parts[i]], "/".join(parts[:i])
    return None


def _read_html(zf, html_path):
    try:
        return parse_page(zf.read(html_path))
    except KeyError:
        return None


def _finalize_entry(entry):
    """Добавляет нормализованные/скелет/стем-колонки к уже собранному словарю записи."""
    entry["owner_ru_norm"] = norm(entry["owner_ru"])
    entry["name_ru_norm"] = norm(entry["name_ru"])
    entry["name_en_norm"] = norm(entry["name_en"])
    entry["name_ru_skel"] = skeleton(entry["name_ru"])
    entry["name_en_skel"] = skeleton(entry["name_en"])
    entry["fqn_norm"] = norm(entry["fqn"])
    stem_source = norm(
        f"{entry['name_ru']} {entry['name_en']} {entry['name_tokens']} {entry['description']}"
    )
    entry["descr_stem"] = stem_text(stem_source)
    return entry


def _make_entry(kind, owner_page, member_page, fallback_name_ru, fallback_name_en):
    owner_ru = owner_page and owner_page.get("owner_ru") or (member_page or {}).get("owner_ru", "")
    owner_en = owner_page and owner_page.get("owner_en") or (member_page or {}).get("owner_en", "")
    page = member_page or {}
    name_ru = page.get("member_ru") or fallback_name_ru
    name_en = page.get("member_en") or fallback_name_en
    if not owner_ru and page.get("owner_ru"):
        owner_ru, owner_en = page["owner_ru"], page["owner_en"]

    fqn = f"{owner_ru}.{name_ru}" if owner_ru else name_ru
    params = page.get("parameters") or []
    sections = page.get("sections") or {}
    return _finalize_entry({
        "kind": kind,
        "language": "bsl",
        "owner_ru": owner_ru, "owner_en": owner_en,
        "name_ru": name_ru, "name_en": name_en,
        "fqn": fqn,
        "signature": sections.get("Синтаксис", ""),
        "params_json": json.dumps(params, ensure_ascii=False),
        "return_type": sections.get("Возвращаемое значение", ""),
        "description": sections.get("Описание", ""),
        "availability": sections.get("Доступность", ""),
        "since": page.get("since") or "",
        "name_tokens": index_tokens(name_ru, name_en),
    })


def _parse_st(text):
    ru = re.search(r'"ru"[^,]*,[^,]*,[^,]*,[^,]*,"([^"]*)"', text)
    en = re.search(r'"en"[^,]*,[^,]*,[^,]*,[^,]*,"([^"]*)"', text)
    ru_name = (ru.group(1) if ru else "").split("(")[0].strip()
    en_name = (en.group(1) if en else "").split("(")[0].strip()
    return ru_name, en_name


def _iter_entries(zf):
    namelist = zf.namelist()
    directories = _implied_directories(namelist)

    st_paths = [n for n in namelist if n.startswith("objects/") and n.endswith(".st")]
    html_paths = {n for n in namelist if n.startswith("objects/") and n.endswith(".html")}

    owner_page_cache = {}

    def owner_page_for(owner_dir):
        if owner_dir in owner_page_cache:
            return owner_page_cache[owner_dir]
        html_path = owner_dir + ".html"
        page = _read_html(zf, html_path) if html_path in html_paths else None
        owner_page_cache[owner_dir] = page
        return page

    for st_path in st_paths:
        classified = _classify_member_path(st_path)
        if classified is None:
            continue
        kind, owner_dir = classified
        try:
            ru_name, en_name = _parse_st(zf.read(st_path).decode("utf-8-sig", errors="ignore"))
        except Exception:
            ru_name = en_name = ""

        html_path = st_path[: -len(".st")] + ".html"
        member_page = _read_html(zf, html_path) if html_path in html_paths else None
        owner_page = owner_page_for(owner_dir)

        fallback = ru_name or PurePosixPath(st_path).stem
        yield _make_entry(kind, owner_page, member_page, fallback, en_name)

    # объекты: .html с одноимённой директорией, кроме рубрик каталога (catalogNNN)
    for html_path in html_paths:
        object_dir = html_path[: -len(".html")]
        if object_dir not in directories:
            continue
        if _CATALOG_RUBRIC_RE.match(PurePosixPath(object_dir).name):
            continue
        page = _read_html(zf, html_path)
        if page is None:
            continue
        name_ru = page.get("title_ru") or PurePosixPath(object_dir).name
        name_en = page.get("title_en") or ""
        sections = page.get("sections") or {}
        yield _finalize_entry({
            "kind": "object",
            "language": "bsl",
            "owner_ru": "", "owner_en": "",
            "name_ru": name_ru, "name_en": name_en,
            "fqn": name_ru,
            "signature": "", "params_json": "[]",
            "return_type": "",
            "description": sections.get("Описание", ""),
            "availability": sections.get("Доступность", ""),
            "since": page.get("since") or "",
            "name_tokens": index_tokens(name_ru, name_en),
        })


_SDBL_SKIP_MEMBERS = frozenset({"__categories__"})


def _iter_sdbl_entries(zf):
    """shquery_ru.hbk — язык запросов (SDBL). Разметка проще и неоднородна (обычные
    <H1>/<PRE>, без V8SH_chapter) — используем parse_simple_page, единый kind='sdbl',
    без структурных параметров (их в этой разметке просто нет как отдельных блоков).
    language='sdbl' — обязателен, иначе коллизии имён с BSL (TrimAll/StrFind/Lower/
    Upper/Left/Right/Int/Round/Pow/Sqrt/тригонометрия/Exp/Log/Log10/UUID/ValueList/
    Parameters/StoredDataSize — 26 совпадений, см. docs/IMPROVEMENT_PLAN.md §5.4)
    сделают точное совпадение по имени неоднозначным без возможности разрешить его."""
    for member_name in zf.namelist():
        if member_name in _SDBL_SKIP_MEMBERS:
            continue
        try:
            page = parse_simple_page(zf.read(member_name))
        except Exception:
            continue
        name_ru = page.get("title_ru") or member_name
        name_en = page.get("title_en") or member_name
        yield _finalize_entry({
            "kind": "sdbl",
            "language": "sdbl",
            "owner_ru": "", "owner_en": "",
            "name_ru": name_ru, "name_en": name_en,
            "fqn": name_ru,
            "signature": page.get("signature", ""),
            "params_json": "[]",
            "return_type": "",
            "description": page.get("description", ""),
            "availability": "",
            "since": page.get("since") or "",
            "name_tokens": index_tokens(name_ru, name_en),
        })


def _iter_bsl_syntax_entries(zf):
    """shlang_ru.hbk — операторы, директивы препроцессора, примитивные типы,
    аннотации встроенного языка BSL (не отдельный язык -- language='bsl', но
    отдельный kind='bsl_syntax', т.к. это не член объекта). Разметка неоднородна:
    часть страниц использует V8SH_pagetitle/V8SH_title (как shcntx), часть -- голый
    <H1>; часть членов существует с суффиксом .html, часть без -- пробуем оба через
    parse_simple_page, которому оба варианта разметки одинаково безразличны."""
    for member_name in zf.namelist():
        if member_name in _SDBL_SKIP_MEMBERS or member_name.endswith(".st"):
            continue
        try:
            page = parse_simple_page(zf.read(member_name))
        except Exception:
            continue
        name_ru = page.get("title_ru") or member_name
        name_en = page.get("title_en") or member_name
        yield _finalize_entry({
            "kind": "bsl_syntax",
            "language": "bsl",
            "owner_ru": "", "owner_en": "",
            "name_ru": name_ru, "name_en": name_en,
            "fqn": name_ru,
            "signature": page.get("signature", ""),
            "params_json": "[]",
            "return_type": "",
            "description": page.get("description", ""),
            "availability": "",
            "since": page.get("since") or "",
            "name_tokens": index_tokens(name_ru, name_en),
        })


def build_index(hbk_path, db_path, platform_version=None, hbk_sha256=None,
                shquery_hbk_path=None, shlang_hbk_path=None):
    """Строит SQLite-индекс из shcntx_ru.hbk, опционально дополняя языком запросов
    (shquery_ru.hbk, language='sdbl') и синтаксисом встроенного языка (shlang_ru.hbk,
    kind='bsl_syntax'). Отдельный шаг, не побочный эффект запуска."""
    if os.path.exists(db_path):
        os.remove(db_path)

    zf = open_hbk(hbk_path)
    db = sqlite3.connect(db_path)
    db.executescript(_DDL)

    insert_entry = (
        "INSERT INTO entry(kind, language, owner_ru, owner_en, owner_ru_norm, name_ru, name_en, "
        "name_ru_norm, name_en_norm, name_ru_skel, name_en_skel, fqn, fqn_norm, signature, "
        "params_json, return_type, description, availability, since, name_tokens, descr_stem) "
        "VALUES (:kind,:language,:owner_ru,:owner_en,:owner_ru_norm,:name_ru,:name_en,"
        ":name_ru_norm,:name_en_norm,:name_ru_skel,:name_en_skel,:fqn,:fqn_norm,:signature,"
        ":params_json,:return_type,:description,:availability,:since,:name_tokens,:descr_stem)"
    )
    count = 0
    for entry in _iter_entries(zf):
        db.execute(insert_entry, entry)
        count += 1

    if shquery_hbk_path is not None:
        for entry in _iter_sdbl_entries(open_hbk(shquery_hbk_path)):
            db.execute(insert_entry, entry)
            count += 1

    if shlang_hbk_path is not None:
        for entry in _iter_bsl_syntax_entries(open_hbk(shlang_hbk_path)):
            db.execute(insert_entry, entry)
            count += 1

    db.commit()

    db.execute(
        "INSERT INTO fts_name(rowid,name_ru,name_en,name_tokens) "
        "SELECT entry_id,name_ru,name_en,name_tokens FROM entry"
    )
    db.execute(
        "INSERT INTO fts_doc(rowid,name_ru,name_en,name_tokens,signature,description,descr_stem) "
        "SELECT entry_id,name_ru,name_en,name_tokens,signature,description,descr_stem FROM entry"
    )
    db.executemany("INSERT INTO alias(phrase, target_name) VALUES (?,?)", alias_rows())
    db.execute(
        "INSERT INTO schema_meta VALUES (?,?,?,?)",
        (SCHEMA_VERSION, platform_version, hbk_sha256, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    db.close()
    return count


def diff_versions(db_path_a, db_path_b):
    """Сравнивает два собранных индекса разных версий платформы по fqn_norm
    (только language='bsl' — SDBL/shlang не версионируются платформой так же).
    'Изменено' — отличается сигнатура или описание. Оба индекса должны быть уже
    собраны (docs/IMPROVEMENT_PLAN.md §5.2, §8 Этап 3): сборка — явный шаг, не
    побочный эффект вызова diff."""
    conn_a = sqlite3.connect(str(db_path_a))
    conn_b = sqlite3.connect(str(db_path_b))

    def snapshot(conn):
        return {
            row[0]: (row[1] or "", row[2] or "")
            for row in conn.execute(
                "SELECT fqn_norm, signature, description FROM entry WHERE language='bsl' AND fqn_norm != ''"
            )
        }

    a, b = snapshot(conn_a), snapshot(conn_b)
    conn_a.close()
    conn_b.close()

    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    return {
        "added": added, "removed": removed, "changed": changed,
        "added_count": len(added), "removed_count": len(removed), "changed_count": len(changed),
        "total_a": len(a), "total_b": len(b),
    }


class SyntaxDB:
    """Читающий доступ к собранному индексу. Один экземпляр на процесс сервера."""

    def __init__(self, db_path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        version = self.conn.execute(
            "SELECT index_schema_version FROM schema_meta LIMIT 1"
        ).fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Индекс собран другой версией схемы ({version}); ожидается {SCHEMA_VERSION}. "
                "Пересоберите индекс: python server.py --build-index"
            )
        self._fuzzy_names = None  # ленивая инициализация -- строится при первом fuzzy-запросе
        self._fuzzy_name_to_ids = None
        self._alias_phrases = None

    def close(self):
        self.conn.close()

    def _row_to_dict(self, row):
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json") or "[]")
        return d

    def get_by_fqn(self, fqn, language="bsl"):
        """language по умолчанию 'bsl' -- сохраняет однозначность существующих
        вызовов; language=None снимает фильтр (искать в обоих языках)."""
        sql = "SELECT * FROM entry WHERE fqn_norm = ?"
        params = [norm(fqn)]
        if language:
            sql += " AND language = ?"
            params.append(language)
        row = self.conn.execute(sql + " LIMIT 1", params).fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_name(self, name, owner=None, language="bsl"):
        """Точный поиск по имени (ru или en, любой регистр). При коллизии без owner
        — список кандидатов. language по умолчанию 'bsl' -- без явного запроса
        SDBL-варианты (26 имён совпадают с BSL, docs/IMPROVEMENT_PLAN.md §5.4) не
        превращают однозначный сегодня вызов в неоднозначный; language=None снимает
        фильтр.

        Возвращает (item_or_none, candidates): если найден единственный кандидат —
        (item, []); если несколько и owner не сужает выбор — (None, candidates);
        если ничего не найдено — (None, []).
        """
        name_norm = norm(name)
        lang_clause, lang_params = ("", []) if not language else (" AND language = ?", [language])

        if owner:
            row = self.conn.execute(
                "SELECT * FROM entry WHERE (name_ru_norm = ? OR name_en_norm = ?) "
                "AND owner_ru_norm = ?" + lang_clause + " LIMIT 1",
                [name_norm, name_norm, norm(owner)] + lang_params,
            ).fetchone()
            return (self._row_to_dict(row), []) if row else (None, [])

        rows = self.conn.execute(
            "SELECT * FROM entry WHERE (name_ru_norm = ? OR name_en_norm = ?)" + lang_clause,
            [name_norm, name_norm] + lang_params,
        ).fetchall()
        if not rows:
            return None, []
        if len(rows) == 1:
            return self._row_to_dict(rows[0]), []
        return None, [self._row_to_dict(r) for r in rows]

    def suggest_completions(self, prefix, limit=10, kind=None):
        """Ранжирование НЕ по bm25(): при префиксном запросе сотни коротких имён
        (значения перечислений и т.п.) получают от FTS5 идентичный ранг из-за
        length-нормализации bm25 -- измерено на реальных данных: для префикса "стр"
        575 совпадений, СтрДлина/СтрНайти оказываются на позициях 53/57 из 575 с тем
        же rank, что и сотни разных "Строка"/"Страна"/"Странница", и просто не
        попадают в окно предвыборки. Вместо этого сортируем по приоритету типа
        (метод/объект — то, что чаще ищут при наборе кода) и длине имени."""
        prefix_norm = norm(prefix)
        if not prefix_norm:
            return []
        match = f'"{prefix_norm}"*'
        sql = (
            "SELECT e.name_ru, e.name_en, e.kind, e.owner_ru FROM fts_name "
            "JOIN entry e ON e.entry_id = fts_name.rowid "
            "WHERE fts_name MATCH ?"
        )
        params = [match]
        if kind:
            sql += " AND e.kind = ?"
            params.append(kind)
        sql += " LIMIT 1000"  # достаточно с запасом; корпус для одного префикса мал (измерено: <600)
        try:
            rows = [dict(r) for r in self.conn.execute(sql, params)]
        except sqlite3.OperationalError:
            return []
        rows.sort(key=lambda r: (_KIND_PRIORITY.get(r["kind"], 9), len(r["name_ru"]), r["name_ru"]))
        return rows[:limit]

    def _ensure_fuzzy_index(self):
        if self._fuzzy_names is not None:
            return
        name_to_ids = {}
        for row in self.conn.execute("SELECT entry_id, name_ru_norm, name_en_norm FROM entry"):
            for n in (row["name_ru_norm"], row["name_en_norm"]):
                if n and len(n) >= _FUZZY_MIN_NAME_LEN:
                    name_to_ids.setdefault(n, []).append(row["entry_id"])
        self._fuzzy_name_to_ids = name_to_ids
        self._fuzzy_names = list(name_to_ids.keys())

    def search(self, query, limit=10, kind=None, owner=None, language=None):
        """Строгие уровни (docs/IMPROVEMENT_PLAN.md §5.7): точное имя/fqn -> алиасы ->
        раскладка/гомоглифы -> fuzzy -> bm25. Уровень не может быть переранжирован
        более поздним — раннее совпадение просто исключает запись из более поздних.

        language=None (по умолчанию) ищет по обоим языкам сразу -- в отличие от
        get_by_name/get_by_fqn, здесь это не создаёт неоднозначности: search_syntax
        возвращает СПИСОК, а не один ответ, и язык каждого результата виден в поле
        item['language']. Передайте language='bsl'|'sdbl', чтобы сузить явно."""
        query_norm = norm(query.strip())
        seen = set()
        results = []

        def add(row, tier):
            item = self._row_to_dict(row)
            if item["entry_id"] in seen:
                return
            seen.add(item["entry_id"])
            item["tier"] = tier
            results.append(item)

        def matches_filters(row):
            if kind and row["kind"] != kind:
                return False
            if owner and norm(row["owner_ru"]) != norm(owner):
                return False
            if language and row["language"] != language:
                return False
            return True

        # T0 -- точное имя или fqn, любой регистр кириллицы
        for row in self.conn.execute(
            "SELECT * FROM entry WHERE name_ru_norm = ? OR name_en_norm = ? OR fqn_norm = ?",
            (query_norm, query_norm, query_norm),
        ):
            if matches_filters(row):
                add(row, "exact")

        # T1 -- алиасы намерений (docs/IMPROVEMENT_PLAN.md §4.4). Сравнение — подстрока
        # нормализованного запроса, не только точный токен: часть алиасов — короткие
        # фразы ("содержит значение"), а не одно слово, и токенизация их бы разбила.
        if len(results) < limit:
            if self._alias_phrases is None:
                self._alias_phrases = [
                    (r["phrase"], r["target_name"])
                    for r in self.conn.execute("SELECT phrase, target_name FROM alias")
                ]
            alias_targets = [target for phrase, target in self._alias_phrases if phrase in query_norm]
            for target in alias_targets:
                for row in self.conn.execute(
                    "SELECT * FROM entry WHERE name_ru_norm = ?", (norm(target),)
                ):
                    if matches_filters(row):
                        add(row, "alias")

        # T2 -- раскладка ЙЦУКЕН/QWERTY и гомоглиф-скелет: точное совпадение
        # производного ключа, guard'ы из docs/IMPROVEMENT_PLAN.md §6.3
        if len(results) < limit:
            if is_latin_only(query) and len(query_norm) >= 5:
                layout_norm = norm(qwerty_to_jcuken(query))
                for row in self.conn.execute(
                    "SELECT * FROM entry WHERE name_ru_norm = ? OR name_en_norm = ?",
                    (layout_norm, layout_norm),
                ):
                    if matches_filters(row):
                        add(row, "layout")

            query_skel = skeleton(query)
            if query_skel and query_skel != query_norm:
                for row in self.conn.execute(
                    "SELECT * FROM entry WHERE name_ru_skel = ? OR name_en_skel = ?",
                    (query_skel, query_skel),
                ):
                    if matches_filters(row):
                        add(row, "homoglyph")

        # T3 -- fuzzy (rapidfuzz), только для имён от 7 символов (см. _FUZZY_MIN_NAME_LEN).
        # Дедуп по лучшему счёту, а не через set(): порядок обхода set() по строкам
        # рандомизирован между процессами (хеш-сид Python) -- без этого MRR был
        # недетерминирован между запусками при равных top-5, и заодно результаты
        # fuzzy вообще не были отсортированы по релевантности.
        if len(results) < limit and len(query_norm) >= _FUZZY_MIN_NAME_LEN:
            self._ensure_fuzzy_index()
            candidates = [query_norm]
            if is_latin_only(query):
                candidates.append(norm(qwerty_to_jcuken(query)))
            best_score = {}
            for cand in candidates:
                for matched_name, score, _idx in process.extract(
                    cand, self._fuzzy_names, scorer=fuzz.ratio,
                    score_cutoff=_FUZZY_SCORE_CUTOFF, limit=limit * 3,
                ):
                    if score > best_score.get(matched_name, -1):
                        best_score[matched_name] = score
            for matched_name in sorted(best_score, key=lambda n: (-best_score[n], n)):
                for entry_id in self._fuzzy_name_to_ids.get(matched_name, ()):
                    row = self.conn.execute(
                        "SELECT * FROM entry WHERE entry_id = ?", (entry_id,)
                    ).fetchone()
                    if row is not None and matches_filters(row):
                        add(row, "fuzzy")

        # T4 -- bm25 по описанию/сигнатуре/сегментам (OR как recall-проход).
        # kind/owner/language фильтруются В SQL, а не постфактум в Python: иначе
        # редкая целевая запись (например единственная запись SDBL среди тысяч BSL-
        # совпадений на общеупотребимые слова) обрезается самим LIMIT раньше, чем до
        # неё доходит фильтр -- измерено: "количество записей в запросе" с
        # language='sdbl' возвращал 0 результатов, хотя нужная запись существует и
        # просто не попадала в окно предвыборки до фильтрации по языку (тот же класс
        # бага, что был найден и исправлен в suggest_completions).
        if len(results) < limit:
            raw_tokens = _query_tokens(query) or [query_norm]
            # стеммированные токены запроса дописываются в тот же OR-список: они
            # бьют только по колонке descr_stem (raw-колонки не содержат основ),
            # раздельная колонка с меньшим весом -- проще, чем городить отдельный
            # MATCH-проход с column-фильтром для одного дополнительного сигнала
            all_tokens = list(dict.fromkeys(raw_tokens + stem_tokens(raw_tokens)))
            match = _build_match(all_tokens)
            if match:
                extra_where, extra_params = "", []
                if kind:
                    extra_where += " AND e.kind = ?"
                    extra_params.append(kind)
                if owner:
                    extra_where += " AND e.owner_ru_norm = ?"
                    extra_params.append(norm(owner))
                if language:
                    extra_where += " AND e.language = ?"
                    extra_params.append(language)
                sql = (
                    f"SELECT e.*, bm25(fts_doc,{','.join(str(w) for w in _BM25_WEIGHTS_DOC)}) AS score "
                    "FROM fts_doc JOIN entry e ON e.entry_id = fts_doc.rowid "
                    "WHERE fts_doc MATCH ?" + extra_where + " ORDER BY score LIMIT ?"
                )
                try:
                    for row in self.conn.execute(sql, [match] + extra_params + [limit * 4]):
                        add(row, "bm25")
                        if len(results) >= limit:
                            break
                except sqlite3.OperationalError:
                    pass

        return results[:limit]
