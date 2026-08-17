"""Структурный разбор страниц справки 1С через lxml.

Заменяет regex-парсинг server.py: секция "Параметры" у 100% элементов терялась
из-за лукахеда `(?=<p class="|<h1|<div|$)`, который срабатывал на самом первом
`<div class="V8SH_rubric">`, которым эта секция всегда открывается. 9794 из 9800
таких блоков содержат незакрытый `<p>` — regex не может выразить границу секции
надёжно, а lxml восстанавливает дерево корректно.

Ключевой факт о разметке (проверено на реальном корпусе): все смысловые узлы одной
страницы — маркеры секций (`p.V8SH_chapter`), блоки параметров (`div.V8SH_rubric`)
и текст между ними — лежат на одном уровне вложенности (прямые дети `<body>`), то
есть текст между двумя элементами хранится в `.tail` ПРЕДЫДУЩЕГО элемента, а не
внутри следующего. Разбор построен на этом факте, а не на рекурсивном обходе всего
поддерева, — иначе текст заголовка параметра задваивается с его описанием.
"""

import re

from lxml import html as lxml_html

CHAPTER_CLASS = "V8SH_chapter"
RUBRIC_CLASS = "V8SH_rubric"
TITLE_CLASS = "V8SH_pagetitle"
OWNER_CLASS = "V8SH_title"
HEADING_CLASS = "V8SH_heading"

_SINCE_RE = re.compile(r"Доступен,\s*начиная\s*с\s*версии\s*([\d.]+)")
_RU_EN_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_PARAM_HEAD_RE = re.compile(r"^[<«]?\s*(.+?)\s*[>»]?\s*\((.+?)\)\s*$")
_TYPE_RE = re.compile(r"^Тип:\s*([^.]+)\.\s*(.*)$", re.DOTALL)
_SIMPLE_TITLE_RE = re.compile(r"^(?:Функция|Оператор|Ключевое слово)?\s*(.+?)\s*\(([^()]*)\)\s*$")


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _split_ru_en(text):
    m = _RU_EN_RE.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text, ""


def _section_text(marker, siblings):
    """Текст секции: tail маркера + text_content()+tail каждого следующего сиблинга."""
    parts = []
    if marker.tail and marker.tail.strip():
        parts.append(marker.tail.strip())
    for el in siblings:
        if el.tag in ("script", "style"):
            continue
        content = el.text_content()
        if content and content.strip():
            parts.append(content.strip())
        if el.tail and el.tail.strip():
            parts.append(el.tail.strip())
    return _clean(" ".join(parts))


def _parse_param_head(head):
    m = _PARAM_HEAD_RE.match(head)
    if m:
        name = m.group(1).strip("<>«» ")
        required_word = m.group(2).strip()
        return name, ("необязательный" not in required_word.lower())
    return head.strip("<>«» "), True


def _parse_parameters(siblings):
    """siblings — прямые дети body внутри секции "Параметры"."""
    params = []
    rubric_positions = [
        i for i, el in enumerate(siblings)
        if el.tag == "div" and el.get("class") == RUBRIC_CLASS
    ]
    for j, pos in enumerate(rubric_positions):
        rubric = siblings[pos]
        name, required = _parse_param_head(_clean(rubric.text_content()))

        next_pos = rubric_positions[j + 1] if j + 1 < len(rubric_positions) else len(siblings)
        body_text = _section_text(rubric, siblings[pos + 1 : next_pos])

        type_match = _TYPE_RE.match(body_text)
        if type_match:
            param_type, description = type_match.group(1).strip(), type_match.group(2).strip()
        else:
            param_type, description = "", body_text

        params.append(
            {"name": name, "required": required, "type": param_type, "description": description}
        )
    return params


def parse_page(html_bytes):
    """Разбирает одну страницу справки 1С (.html) в структурированный словарь.

    Возвращает dict: title_ru/title_en, owner_ru/owner_en, member_ru/member_en,
    sections (имя секции -> текст), parameters (список {name, required, type,
    description}), since (строка версии или None).
    """
    doc = lxml_html.fromstring(html_bytes)
    body = doc.find("body")
    if body is None:
        body = doc

    result = {
        "title_ru": "", "title_en": "",
        "owner_ru": "", "owner_en": "",
        "member_ru": "", "member_en": "",
        "sections": {}, "parameters": [], "since": None,
    }

    h1 = doc.find(".//h1")
    if h1 is not None:
        result["title_ru"], result["title_en"] = _split_ru_en(_clean(h1.text_content()))

    owner_p = doc.find(f'.//p[@class="{OWNER_CLASS}"]')
    if owner_p is not None:
        result["owner_ru"], result["owner_en"] = _split_ru_en(_clean(owner_p.text_content()))

    heading_p = doc.find(f'.//p[@class="{HEADING_CLASS}"]')
    if heading_p is not None:
        result["member_ru"], result["member_en"] = _split_ru_en(_clean(heading_p.text_content()))

    children = list(body)
    chapter_positions = [
        i for i, el in enumerate(children)
        if el.tag == "p" and el.get("class") == CHAPTER_CLASS
    ]
    for j, pos in enumerate(chapter_positions):
        marker = children[pos]
        name = _clean(marker.text_content()).rstrip(":")
        next_pos = chapter_positions[j + 1] if j + 1 < len(chapter_positions) else len(children)
        siblings = children[pos + 1 : next_pos]

        result["sections"][name] = _section_text(marker, siblings)
        if name == "Параметры":
            result["parameters"] = _parse_parameters(siblings)

    since_match = _SINCE_RE.search(doc.text_content())
    if since_match:
        result["since"] = since_match.group(1).rstrip(".")

    return result


def parse_simple_page(html_bytes):
    """Разбирает страницу справки языка запросов (shquery_ru.hbk) или встроенного
    языка (shlang_ru.hbk) — заметно более простой и НЕОДНОРОДНЫЙ HTML без разметки
    V8SH_chapter/V8SH_rubric, на которой построен parse_page(): обычные `<H1>`,
    иногда `<PRE>` с сигнатурой, остальное — произвольные `<P>`/`<BLOCKQUOTE>` без
    единого соглашения о разметке секций (проверено: "Функция СокрЛП (TrimAll)",
    "Левое внешнее соединение" без английского варианта, "Функция ЕСТЬNULL " с
    пробелом перед закрывающим тегом — единообразного формата нет).

    Возвращает dict: title_ru, title_en, signature (текст первого <pre>, если есть),
    description (текст body после заголовка и сигнатуры, тегов нет), since.
    """
    doc = lxml_html.fromstring(html_bytes)

    title_ru, title_en = "", ""
    h1 = doc.find(".//h1")
    if h1 is not None:
        h1_text = _clean(h1.text_content())
        m = _SIMPLE_TITLE_RE.match(h1_text)
        if m:
            title_ru, title_en = m.group(1).strip(), m.group(2).strip()
        else:
            title_ru = h1_text

    signature = ""
    pre = doc.find(".//pre")
    if pre is not None:
        signature = _clean(pre.text_content())

    body = doc.find(".//body")
    if body is None:
        body = doc
    full_text = _clean(body.text_content())
    description = full_text
    if h1 is not None:
        description = description.replace(_clean(h1.text_content()), "", 1).strip()
    if signature:
        description = description.replace(signature, "", 1).strip()

    since_match = _SINCE_RE.search(full_text)
    return {
        "title_ru": title_ru,
        "title_en": title_en,
        "signature": signature,
        "description": description,
        "since": since_match.group(1).rstrip(".") if since_match else None,
    }
