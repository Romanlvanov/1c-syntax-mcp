"""Каноническая нормализация запроса и имён — единая функция для индекса и запроса.

Была найдена и здесь же исправлена реальная ошибка: `sqlite3`-коллация `NOCASE`
складывает регистр только для ASCII, а НЕ для кириллицы (проверено:
`'СтрДлина' = 'стрдлина' COLLATE NOCASE` -> false). Ранняя версия `syntax_db.py`
приводила запрос к нижнему регистру в Python (`_norm`), а затем сравнивала его с
`COLLATE NOCASE` — из-за этого несовпадения уровень T0 (точное совпадение) не
срабатывал вообще ни для одного кириллического запроса, и всё уходило в bm25.
Правильное решение: нормализовать ОБЕ стороны одной и той же функцией на этапе
сборки индекса и на этапе запроса, сравнивать нормализованные значения напрямую
(`=`), без DB-side COLLATE.

Кроме регистра, здесь же — раскладка ЙЦУКЕН<->QWERTY (docs/IMPROVEMENT_PLAN.md,
раздел 6.3) и гомоглиф-скелет (кириллица/латиница, визуально неразличимые буквы).
"""

import re
import unicodedata

_YO = str.maketrans({"ё": "е", "Ё": "Е"})


def norm(text: str) -> str:
    """NFKC + casefold + ё->е. Канонический вид для ЛЮБОГО сравнения имён."""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).translate(_YO).casefold()


# --- гомоглифы: кириллица/латиница, визуально неразличимые буквы -----------
# применяется ПОСЛЕ casefold: часть пар (В/B, К/K, М/M, Н/H, Т/T) в Unicode
# confusables.txt существует только как заглавная — до casefold их нет вовсе.
_HOMOGLYPH_MAP = str.maketrans(
    {
        "а": "a", "в": "b", "с": "c", "е": "e", "н": "h",
        "к": "k", "м": "m", "о": "o", "р": "p", "т": "t",
        "х": "x", "у": "y", "і": "i", "ѕ": "s", "ј": "j",
    }
)


def skeleton(text: str) -> str:
    """Вторичный ключ для сравнения через гомоглифы. Никогда не заменяет norm()."""
    return norm(text).translate(_HOMOGLYPH_MAP)


# --- раскладка ЙЦУКЕН <-> QWERTY (94 клавиши, включая пунктуацию) -----------
_QWERTY = (
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
    "QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>~"
)
_JCUKEN = (
    "йцукенгшщзхъфывапролджэячсмитьбюё"
    "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ"
)
_QWERTY_TO_JCUKEN = str.maketrans(_QWERTY, _JCUKEN)
_JCUKEN_TO_QWERTY = str.maketrans(_JCUKEN, _QWERTY)

_LATIN_ONLY_RE = re.compile(r"^[a-zA-Z\[\];',./`\s-]+$")
_CYRILLIC_ONLY_RE = re.compile(r"^[а-яёА-ЯЁ\s-]+$")


def qwerty_to_jcuken(text: str) -> str:
    """Пользователь набрал кириллицу на раскладке QWERTY: Cnhlkbyf -> Стрдлина."""
    return text.translate(_QWERTY_TO_JCUKEN)


def jcuken_to_qwerty(text: str) -> str:
    return text.translate(_JCUKEN_TO_QWERTY)


def is_latin_only(text: str) -> bool:
    return bool(text) and bool(_LATIN_ONLY_RE.match(text))


def is_cyrillic_only(text: str) -> bool:
    return bool(text) and bool(_CYRILLIC_ONLY_RE.match(text))
