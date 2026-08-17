"""Стемминг Snowball для сопоставления словоформ (docs/IMPROVEMENT_PLAN.md §6.2).

Отдельная стеммированная колонка, а не кастомный FTS5-токенайзер: регистрация
Python-токенайзера через stdlib `sqlite3` невозможна в принципе — FTS5 отдаёт свой
API только через `sqlite3_bind_pointer(..., "fts5_api_ptr")`, которого у stdlib
`sqlite3` нет (доступно только через APSW). Поэтому одна и та же функция стемминга
применяется и при сборке индекса (к описанию/именам), и при обработке запроса —
несовпадение нормализации между индексом и запросом уже один раз ломало точный
поиск в этом проекте (см. normalization.py) и делает выдачу непредсказуемой.

Snowball — суффиксный стеммер, не лемматизатор: детерминирован и не требует
словаря (в отличие от pymorphy3, ~8.6 МБ данных), но не режет префиксы и не
связывает разные части речи одного корня (`группировка` и `сгруппировать` дают
разные основы). Закрывает словоизменение («регулярные»/«регулярному»), не
закрывает словообразование или синонимию — это упирается в измеренные результаты
Этапа 5, а не в обещание.
"""

import re

import snowballstemmer

_ru_stemmer = snowballstemmer.stemmer("russian")
_en_stemmer = snowballstemmer.stemmer("english")
_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
_CYR_RE = re.compile(r"[а-яё]")
_LAT_RE = re.compile(r"[a-z]")


def stem_tokens(tokens):
    """Список нормализованных (нижний регистр, без ё) токенов -> их основы."""
    ru_idx, ru_words = [], []
    en_idx, en_words = [], []
    out = list(tokens)
    for i, t in enumerate(tokens):
        if _CYR_RE.search(t):
            ru_idx.append(i)
            ru_words.append(t)
        elif _LAT_RE.search(t):
            en_idx.append(i)
            en_words.append(t)
    if ru_words:
        for i, stem in zip(ru_idx, _ru_stemmer.stemWords(ru_words)):
            out[i] = stem
    if en_words:
        for i, stem in zip(en_idx, _en_stemmer.stemWords(en_words)):
            out[i] = stem
    return out


def stem_text(normalized_text):
    """normalized_text — уже приведённый к нижнему регистру/без ё текст (см.
    normalization.norm). Возвращает строку основ через пробел, для FTS5-колонки
    или для дозаполнения OR-запроса."""
    tokens = _TOKEN_RE.findall(normalized_text or "")
    if not tokens:
        return ""
    return " ".join(stem_tokens(tokens))
