"""Замер качества поиска на golden-set из docs/IMPROVEMENT_PLAN.md.

Запуск без pytest:   python tests/test_search_quality.py
Запуск с pytest:     pytest tests/test_search_quality.py -s

Тест НЕ строит индекс сам — если syntax_index.sqlite ещё не собран, тест
пропускается с пояснением. Собрать явно: `python server.py --build-index`
(нужна установленная 1С). Чтобы указать готовый индекс явно:
SYNTAX_MCP_DB=путь/к/syntax_index.sqlite.

Базовые линии recall@5/MRR ниже — сознательно мягкий порог ("не хуже
зафиксированного"), а не точное совпадение: реализация продолжит меняться
(этапы 2-5 плана), и тест не должен требовать держать её нарочно хуже.

Хронология замеров (docs/IMPROVEMENT_PLAN.md, §4.2, §8, §9 — набор из 12 проб
расширен до 42 при закрытии открытого вопроса №1, поэтому числа до и после
расширения не сравнивать напрямую как один ряд):

  На наборе из 12 (§4.2):
  - до Этапа 1 (регекс-парсинг, плоский словарь, JSON):        recall@5=3/12  MRR=3/12
  - после Этапа 1 (SQLite+FTS5, структурный парсинг,
    сегментация+глоссарий, алиасы, дизамбигуация владельца):    recall@5=8/12  MRR=8/12
  - после Этапа 2 (нормализация, раскладка, гомоглифы, fuzzy):  recall@5=10/12 MRR=10/12

  На расширенном наборе из 42 (после Этапов 3-4, §9 открытый вопрос №1):
  - алиасы+сегментация, без морфологии (тот же баг в T4-bm25,
    что был найден и исправлен в suggest_completions — см. syntax_db.py):
                                                                  recall@5=31/42 MRR≈0.61
  - + стемминг Snowball ru/en (morphology.py) — отдельная
    стеммированная колонка, недостающий кусок связки из §6.2:    recall@5=34/42 MRR≈0.71
  - + 3 точечных алиаса на измеренные остатки разрыва:           recall@5=37/42 MRR≈0.78

Решение по Этапу 5 (условному): семантический слой НЕ внедрён. Критерий плана —
превзойти по MRR связку «алиасы + морфология» — предполагает, что эта связка
существует; на момент проверки её как раз не хватало (Этапы 1-4 закрыли разрыв
сегментацией и алиасами, стемминг остался недобавленным). После добавления
недостающей морфологии и трёх алиасов recall@5 вырос с 31/42 до 37/42 без единой
новой зависимости. Оставшиеся 5 расхождений — 1 ограничение самого теста ("Стр"
тестирует ranking suggest_completion, а не search_syntax) и 4 находки на позициях
6-10 (не "не найдено", а вопрос ранжирования). Ни одно измеренное расхождение не
похоже на нерешаемую без эмбеддингов проблему — довод раздела 4.4 плана
подтвердился на большем наборе, а не только на исходных 12 пробах.
"""

import sys
import unittest
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _server_loader import find_syntax_index_db  # noqa: E402
from golden_set import GOLDEN_SET, evaluate  # noqa: E402

BASELINE_RECALL_AT_5 = 37 / 42  # держим точной дробью, не округлением (плавающая точка)
BASELINE_MRR = 0.7845521541950113  # точное измеренное значение; 0.785 в логах -- округление :.3f для печати


class SearchQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_path = find_syntax_index_db()
        if db_path is None:
            raise unittest.SkipTest(
                "syntax_index.sqlite не найден. Соберите индекс явно: "
                "`python server.py --build-index` (нужна установленная 1С), "
                "или укажите готовый файл через SYNTAX_MCP_DB=путь/к/syntax_index.sqlite."
            )
        from syntax_db import SyntaxDB

        cls.db = SyntaxDB(db_path)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "db"):
            cls.db.close()

    def _search_names(self, query, limit, **extra_kwargs):
        for item in self.db.search(query, limit=limit, **extra_kwargs):
            yield item["name_ru"]

    def test_golden_set_recall_and_mrr(self):
        recall5, mrr, details = evaluate(self._search_names, GOLDEN_SET, k=5)

        print(f"\nrecall@5 = {recall5:.3f}  (базовая линия {BASELINE_RECALL_AT_5:.3f})")
        print(f"MRR      = {mrr:.3f}  (базовая линия {BASELINE_MRR:.3f})")
        for d in details:
            mark = "OK  " if d["hit_at_k"] else ("~   " if d["rank"] else "MISS")
            print(f"  [{mark}] {d['query']!r:40s} rank={d['rank']} top5={d['top']}")

        self.assertGreaterEqual(
            recall5,
            BASELINE_RECALL_AT_5,
            "recall@5 упал ниже зафиксированной базовой линии — регрессия поиска",
        )
        self.assertGreaterEqual(
            mrr,
            BASELINE_MRR,
            "MRR упал ниже зафиксированной базовой линии — регрессия поиска",
        )


if __name__ == "__main__":
    unittest.main()
