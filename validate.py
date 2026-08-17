"""Честная проверка вызова функции 1С: существование + арность, где это выводимо.

Что проверяется реально (docs/IMPROVEMENT_PLAN.md, раздел 6.5/7):
  - существование имени в индексе;
  - арность вызова против структурных параметров (min = число обязательных,
    max = общее число параметров), когда параметры распарсены структурно;
  - однократное разрешение вызова через точку (`Получатель.Метод`), если тип
    получателя выводится из простого локального присваивания вида
    `Х = Новый Тип(...)` в тех же переданных строках кода.

Что честно НЕ проверяется и явно помечается как "не выведено", а не подменяется
угадыванием: реальный вывод типов, поток управления, объекты метаданных
конфигурации, вызовы через несколько уровней владения.
"""

import re

_ASSIGN_NEW_RE = re.compile(r"^\s*(\w+)\s*=\s*(?:Новый|New)\s+([\w.]+)", re.IGNORECASE)
_CALL_RE = re.compile(r"^\s*([\w.]+)\s*\(")


def _skip_quoted(text, i):
    """i указывает на открывающую '"'; возвращает индекс сразу после закрывающей,
    с учётом удвоенной кавычки "" как экранирования внутри строки 1С."""
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':
                j += 2
                continue
            return j + 1
        j += 1
    return n


def split_top_level(text, separator):
    """Разбивает text по separator на верхнем уровне вложенности, не заходя внутрь
    круглых/квадратных скобок и строковых литералов."""
    parts = []
    current = []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            end = _skip_quoted(text, i)
            current.append(text[i:end])
            i = end
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return [p for p in parts if p != ""]


def _find_matching_paren(text, open_idx):
    depth = 0
    i, n = open_idx, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_quoted(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def check_arity(item, args, raw_name):
    """item — запись из SyntaxDB (с полем 'params'), args — список текстов аргументов."""
    params = item.get("params") or []
    if not params and item.get("signature"):
        return {
            "ok": None,
            "note": f"«{raw_name}» существует; структурные параметры недоступны — "
            "арность не проверялась (не гарантия корректности, а отсутствие данных)",
            "signature": item.get("signature"),
        }
    min_arity = sum(1 for p in params if p.get("required"))
    max_arity = len(params)
    n = len(args)
    if min_arity <= n <= max_arity:
        return {
            "ok": True,
            "note": f"«{raw_name}» существует, арность корректна ({n} аргументов, "
            f"ожидается {min_arity}" + (f"-{max_arity}" if max_arity != min_arity else "") + ")",
            "signature": item.get("signature"),
        }
    expected = str(min_arity) if max_arity == min_arity else f"{min_arity}-{max_arity}"
    return {
        "ok": False,
        "note": f"«{raw_name}»: передано {n} аргументов, ожидается {expected}",
        "signature": item.get("signature"),
    }


def check_call(syntax_db, code, language="bsl"):
    """Разбирает code (одно или несколько ;-разделённых простых утверждений,
    последнее — сама проверяемая инструкция) и возвращает словарь-результат:
    {"ok": True|False|None, "note": str, "signature": str?}. ok=None означает
    "не выведено", а не "прошло проверку"."""
    statements = split_top_level(code, ";")
    if not statements:
        return {"ok": False, "note": "Не удалось распознать код"}

    type_map = {}
    for stmt in statements[:-1]:
        m = _ASSIGN_NEW_RE.match(stmt)
        if m:
            type_map[m.group(1).lower()] = m.group(2)

    call_stmt = statements[-1]
    m = _CALL_RE.match(call_stmt)
    if not m:
        return {"ok": False, "note": "Не удалось распознать вызов функции в коде"}

    raw_name = m.group(1)
    open_paren = call_stmt.index("(", m.start())
    close_paren = _find_matching_paren(call_stmt, open_paren)
    args_text = call_stmt[open_paren + 1 : close_paren] if close_paren is not None else call_stmt[open_paren + 1 :]
    args = split_top_level(args_text, ",")

    if "." in raw_name:
        receiver, method = raw_name.rsplit(".", 1)
        receiver_type = type_map.get(receiver.lower())
        if receiver_type:
            item, candidates = syntax_db.get_by_name(method, owner=receiver_type, language=language)
            if item is None:
                return {
                    "ok": False,
                    "note": f"Метод «{method}» не найден у типа «{receiver_type}» "
                    f"(из «{receiver} = Новый {receiver_type}(...)»)",
                }
            return check_arity(item, args, raw_name)
        return {
            "ok": None,
            "note": f"Тип получателя «{receiver}» не выводится из переданного кода — "
            f"существование и арность «{method}» не проверялись (undecidable)",
        }

    item, candidates = syntax_db.get_by_name(raw_name, language=language)
    if item is None and len(candidates) == 1:
        item = candidates[0]
    if item is None:
        if candidates:
            return {
                "ok": None,
                "note": f"«{raw_name}» существует, но имя неоднозначно "
                f"({len(candidates)} объектов-владельцев) — арность не проверялась. "
                "Используйте get_by_fqn с точным владельцем.",
            }
        return {"ok": False, "note": f"«{raw_name}» не найдена в синтаксисе 1С"}
    result = check_arity(item, args, raw_name)
    if candidates and len(candidates) > 1:
        result["note"] += (
            f" (имя неоднозначно — {len(candidates)} владельцев; проверено против "
            f"{item.get('owner_ru') or 'первого найденного'})"
        )
    return result
