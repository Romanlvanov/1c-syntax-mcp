"""Сегментация PascalCase-идентификаторов 1С и словарь-глосса для инициализмов.

Разбивает слитные идентификаторы на подслова по границам регистра, смены алфавита
(кириллица/латиница) и цифра/буква: `СокрЛП` -> `[Сокр, ЛП]`, `ЧтениеJSON` ->
`[Чтение, JSON]`, `HTTPRequest` -> `[HTTP, Request]`. Проверено на реальных
идентификаторах справки (см. docs/IMPROVEMENT_PLAN.md, раздел 8): ~130 тыс.
имён/с, без внешних зависимостей.

Вклад в NL-ранжирование измерен отдельно (docs/IMPROVEMENT_PLAN.md §4.4): сама по
себе сегментация даёт малый прирост (ΔMRR ≈ +0.01), реальный прирост даёт GLOSS —
малый (не сотни строк) словарь инициализмов, каждый из которых реально встречается
в корпусе как самостоятельный сегмент (аудит насчитал всего 10 таких кириллических
инициализмов на весь корпус 8.5.1). Однобуквенные сегменты-предлоги (В, С, И, К...)
исключаются как стоп-слова — иначе они засоряют IDF в FTS5.
"""

import re

_CYR = set("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя")

# однобуквенные и предложные сегменты, не несущие самостоятельного смысла в поиске
STOPWORD_SEGMENTS = frozenset(
    "в с и к о у из на по для от до".split() + list("абвгдежзийклмнопрстуфхцчшщъыьэюя")
)

# инициализмы, реально встречающиеся как самостоятельный сегмент в справке 1С
# (докучен по данным аудита; не полный словарь морфем — только то, что измерено)
GLOSS = {
    "лп": "лишние пробелы слева справа",
    "сокр": "сократить обрезать отсечь",
    "ос": "операционная система",
    "субд": "система управления базами данных",
    "ив": "источник времени",
    "нпп": "неразрывный пробел",
    "пс": "перевод строки",
    "вк": "возврат каретки",
    "пф": "перевод формы страницы",
}


def _char_class(ch):
    if ch.isdigit():
        return "D"
    if ch in _CYR:
        return "CU" if ch.isupper() else "CL"
    if ch.isalpha():
        return "LU" if ch.isupper() else "LL"
    return "X"


def _script(cls):
    if cls in ("CU", "CL"):
        return "C"
    if cls in ("LU", "LL"):
        return "L"
    return cls


def segment(name: str) -> list:
    """СокрЛП -> [Сокр, ЛП]; ЧтениеJSON -> [Чтение, JSON]; HTTPRequest -> [HTTP, Request]."""
    if not name:
        return []
    classes = [_char_class(c) for c in name]
    out, current = [], name[0]
    for i in range(1, len(name)):
        prev, cur = classes[i - 1], classes[i]
        if cur == "X" or prev == "X":
            brk = True
        elif prev == "D" and cur != "D":
            brk = True  # 64Строка -> 64|Строка
        elif prev != "D" and cur == "D":
            brk = False  # Base|64 остаётся Base64
        elif _script(prev) != _script(cur):
            brk = True  # ЧтениеJSON, XMLСтрока
        elif prev in ("CL", "LL") and cur in ("CU", "LU"):
            brk = True  # ЗначениеЗаполнено
        elif (
            prev in ("CU", "LU")
            and cur in ("CU", "LU")
            and i + 1 < len(name)
            and classes[i + 1] in ("CL", "LL")
        ):
            brk = True  # HTTPRequest -> HTTP|Request
        else:
            brk = False
        if brk:
            out.append(current)
            current = name[i]
        else:
            current += name[i]
    out.append(current)
    return [s for s in out if s.isalnum()]


def index_tokens(*names) -> str:
    """Готовая строка токенов для колонки name_tokens: сегменты + глосса, без стоп-слов."""
    tokens = []
    for name in names:
        for segment_text in segment(name or ""):
            low = segment_text.lower()
            if low in STOPWORD_SEGMENTS:
                continue
            tokens.append(low)
            if low in GLOSS:
                tokens.extend(GLOSS[low].split())
    return " ".join(tokens)
