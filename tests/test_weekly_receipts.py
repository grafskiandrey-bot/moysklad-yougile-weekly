import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import weekly_receipts as w


class WeekTests(unittest.TestCase):
    def test_week(self):
        self.assertEqual(w.week_window(date(2026, 10, 14)), (date(2026, 10, 12), date(2026, 10, 18)))

    def test_comment_arrival(self):
        order = {"description": "Оплатили 02.10.2026\nДата прибытия в МСК:\nожидаем с 27-28.10",
                 "deliveryPlannedMoment": "2026-10-27 11:03:00"}
        found = w.planned_arrival(order, date(2026, 10, 26), date(2026, 11, 1))
        self.assertEqual((found[0], found[1]), (date(2026, 10, 27), date(2026, 10, 28)))

    def test_production_is_not_receipt(self):
        self.assertIsNone(w.planned_arrival(
            {"description": "готовность 15-18.10 задержка производства"},
            date(2026, 10, 12), date(2026, 10, 18)))

    def test_newer_estimate_takes_precedence(self):
        order = {"description": "Дата прибытия: 13.10.2026\n_______\nожидаем 19.10"}
        found = w.planned_arrival(order, date(2026, 10, 12), date(2026, 10, 18))
        self.assertEqual(found[0], date(2026, 10, 19))

    def test_no_date_fallback(self):
        order = {"description": "В пути", "deliveryPlannedMoment": "2026-10-16 09:00:00"}
        self.assertEqual(w.planned_arrival(order, date(2026, 10, 12), date(2026, 10, 18))[2],
                         "плановая дата МС")

    def test_partial_receipt(self):
        class Dummy:
            def pages(self, path, **kwargs):
                return iter([{"quantity": 10, "shipped": 3,
                              "assortment": {"id": "11111111-1111-1111-1111-111111111111",
                                             "name": "Test", "article": "111"}}])

        with patch.object(w, "accepted_quantities", return_value=({}, False)):
            products = w.pending_products(Dummy(), {"id": "order"})
            self.assertEqual(products[0]["quantity"], Decimal("7"))

    def test_message_marker(self):
        a, b = w.week_window(date(2026, 10, 12))
        self.assertIn("[MS-WEEKLY:2026-10-12]", w.format_message([], a, b))

    def test_idempotency(self):
        self.assertTrue(w.already_sent([{"text": "[MS-WEEKLY:2026-10-12]"}], date(2026, 10, 12)))


if __name__ == "__main__":
    unittest.main()
