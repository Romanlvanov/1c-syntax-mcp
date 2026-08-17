"""Чтение .hbk (справка 1С) без внешних зависимостей.

.hbk — контейнер формата 1Cv8 (не 7z; сам 7-Zip определяет его лишь эвристикой:
`Type = zip`, `Offset = 1656`, "There are data after the end of archive"). Формат
контейнера: 16-байтный заголовок (magic, page_size=512, storage_ver), затем корневой
каталог — цепочка блоков вида `\\r\\n%08x %08x %08x \\r\\n` (doc_size, block_size,
next_page), а сам каталог — список троек (name_addr, data_addr, 0x7FFFFFFF) на
именованные элементы: Book, FileStorage, IndexMainData, IndexPackBlock, MainData,
PackBlock, PackLookup. `FileStorage` — обычный ZIP (Deflate).

Ранняя версия этого модуля искала ZIP-сигнатуру эвристически (годится только для
больших `shcntx_*.hbk`); полный разбор контейнера работает единообразно для всех
файлов справки — проверено на всех четырёх типах (`shcntx_ru`, `shcntx_root`,
`shquery_ru`, `shlang_ru`, `shclang_ru`): число членов ZIP совпадает с 7z для
`shcntx_ru.hbk` (52 807) и с независимым замером для остальных.

Отдельная тонкость: не у каждого файла есть все 7 элементов данных — `shquery_ru.hbk`
не несёт собственных `IndexMainData`/`IndexPackBlock`, и адрес данных для них равен
терминатору `0x7FFFFFFF` (что означает "элемент пуст", а не смещение для чтения).
"""

import io
import struct
import zipfile
from pathlib import Path

_BLOCK_HEADER_SIZE = 31  # b"\r\n%08x %08x %08x \r\n"
_NULL_ADDR = 0x7FFFFFFF  # терминатор: "элемента с таким адресом нет"


class HbkFormatError(ValueError):
    """Не удалось разобрать контейнер .hbk или найти в нём FileStorage."""


def _read_block_header(raw: bytes, offset: int):
    header = raw[offset : offset + _BLOCK_HEADER_SIZE]
    if len(header) < _BLOCK_HEADER_SIZE or header[:2] != b"\r\n":
        return None
    try:
        doc_size, block_size, next_page = header[2:-2].split()
        return int(doc_size, 16), int(block_size, 16), int(next_page, 16)
    except ValueError:
        return None


def _read_chained_doc(raw: bytes, offset: int) -> bytes:
    """Следует цепочке блоков, начиная с offset, и возвращает собранный документ."""
    if offset == _NULL_ADDR:
        return b""
    header = _read_block_header(raw, offset)
    if header is None:
        raise HbkFormatError(f"некорректный заголовок блока по смещению {offset}")

    remaining, _, _ = header
    out = bytearray()
    cursor = offset
    while cursor != _NULL_ADDR and remaining > 0:
        block = _read_block_header(raw, cursor)
        if block is None:
            raise HbkFormatError(f"некорректный заголовок блока по смещению {cursor}")
        _, block_size, next_page = block
        take = min(block_size, remaining)
        out += raw[cursor + _BLOCK_HEADER_SIZE : cursor + _BLOCK_HEADER_SIZE + take]
        remaining -= take
        cursor = next_page
    return bytes(out)


def read_elements(path) -> dict:
    """Разбирает контейнер .hbk целиком и возвращает {имя_элемента: bytes}.

    Пустые/отсутствующие элементы (data_addr == 0x7FFFFFFF, например
    IndexMainData/IndexPackBlock у shquery_ru.hbk) возвращаются как b"".
    """
    raw = Path(path).read_bytes()
    root = _read_chained_doc(raw, 16)
    if len(root) % 4 != 0:
        raise HbkFormatError(f"{path}: длина корневого каталога не кратна 4 байтам")

    word_count = len(root) // 4
    words = struct.unpack(f"<{word_count}I", root)
    if len(words) % 3 != 0:
        raise HbkFormatError(f"{path}: корневой каталог не состоит из троек адресов")

    elements = {}
    for i in range(0, len(words), 3):
        name_addr, data_addr = words[i], words[i + 1]
        name_bytes = _read_chained_doc(raw, name_addr)
        name = name_bytes[20:].decode("utf-16le", errors="ignore").rstrip("\x00")
        elements[name] = _read_chained_doc(raw, data_addr)
    return elements


def open_hbk(path) -> zipfile.ZipFile:
    """Открывает FileStorage-элемент .hbk как обычный zipfile.ZipFile, без 7z."""
    elements = read_elements(path)
    file_storage = elements.get("FileStorage")
    if not file_storage:
        raise HbkFormatError(f"{path}: элемент FileStorage пуст или отсутствует")
    return zipfile.ZipFile(io.BytesIO(file_storage))


def read_text(zf: zipfile.ZipFile, name: str, encoding: str = "utf-8-sig") -> str:
    """Читает один член архива как текст; отсутствие файла — обычный KeyError zipfile."""
    return zf.read(name).decode(encoding, errors="ignore")
