# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Непривилегированный пользователь + точки монтирования томов -- ДО установки
# зависимостей (стабильный слой, редко инвалидируется). Каталоги должны
# существовать и принадлежать 1000:1000 уже В ОБРАЗЕ: иначе Docker создаёт
# точку монтирования свежего именованного тома как root:root, и первая же
# сборка индекса падает PermissionError.
RUN groupadd -g 1000 app \
 && useradd -u 1000 -g 1000 -m -d /home/app -s /usr/sbin/nologin app \
 && mkdir -p /app /data/index /data/hbk \
 && chown -R 1000:1000 /app /data /home/app

WORKDIR /app

COPY requirements.txt .
# --only-binary=:all: -- внятный "no matching distribution", если для какой-то
# платформы вдруг не окажется wheel (lxml/rapidfuzz без него собирались бы из
# исходников, а компилятора в образе нет и не должно быть)
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY *.py ./
COPY web/ ./web/
COPY LICENSE .

RUN python -m compileall -q .

# Функциональная проверка FTS5 на сборке образа, а не молчаливая деградация в
# рантайме: syntax_db.py ловит sqlite3.OperationalError именно на этот случай
# (search()/suggest_completions() иначе тихо теряют результаты без единого
# сообщения). Использует РЕАЛЬНЫЙ _DDL из syntax_db.py (не копию параметров
# от руки) и реальные запросы тех же форм, что suggest_completions/search
# выполняют в рантайме -- значит не может незаметно разойтись со схемой.
RUN python - <<'PY'
import sqlite3, sys
from syntax_db import _DDL

conn = sqlite3.connect(":memory:")
opts = {row[0] for row in conn.execute("pragma compile_options")}
assert "ENABLE_FTS5" in opts, f"SQLite собран без FTS5 (sqlite {sqlite3.sqlite_version})"

conn.executescript(_DDL)
conn.execute(
    "INSERT INTO entry(kind, language, owner_ru, name_ru, fqn, name_tokens, description, descr_stem) "
    "VALUES ('method','bsl','Глобальный контекст','СтрДлина','Глобальный контекст.СтрДлина',"
    "'стр длина','Длина строки','стр длин')"
)
conn.execute(
    "INSERT INTO fts_name(rowid,name_ru,name_en,name_tokens) "
    "SELECT entry_id,name_ru,name_en,name_tokens FROM entry"
)
conn.execute(
    "INSERT INTO fts_doc(rowid,name_ru,name_en,name_tokens,signature,description,descr_stem) "
    "SELECT entry_id,name_ru,name_en,name_tokens,signature,description,descr_stem FROM entry"
)

assert conn.execute("SELECT count(*) FROM fts_name WHERE fts_name MATCH '\"стр\"*'").fetchone()[0] == 1, \
    "префиксный запрос к fts_name (suggest_completion) не работает"
assert conn.execute("SELECT count(*) FROM fts_doc WHERE fts_doc MATCH 'длина'").fetchone()[0] == 1, \
    "полнотекстовый запрос к fts_doc (search) не работает"
print(f"FTS5 OK, sqlite {sqlite3.sqlite_version}", file=sys.stderr)
PY

# Лицензионное ограничение (docs/IMPROVEMENT_PLAN.md): .hbk -- проприетарная
# документация 1С, ни она, ни собранные из неё индексы не должны попадать в
# образ. .dockerignore уже исключает их из контекста сборки -- эта проверка
# ловит регрессию, если кто-то расширит контекст, не подумав про .dockerignore.
RUN ! find . -name '*.hbk' -o -name '*.sqlite' -o -name '*.sqlite3' | grep -q .

ENV SYNTAX_MCP_TRANSPORT=http \
    SYNTAX_MCP_HOST=0.0.0.0 \
    SYNTAX_MCP_PORT=8765 \
    SYNTAX_MCP_INDEX_DIR=/data/index \
    SYNTAX_MCP_UPLOAD_DIR=/data/hbk

USER 1000:1000
EXPOSE 8765
VOLUME ["/data/index", "/data/hbk"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "server.py"]
