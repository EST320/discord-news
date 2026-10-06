import unittest
from datetime import date, datetime, timezone
from unittest import mock

from market_pulse import earnings_calendar as ec

MONDAY = date(2026, 10, 12)
BILLION = 1_000_000_000


class NextWeekRangeTest(unittest.TestCase):
    def range_on(self, year, month, day):
        fake_now = datetime(year, month, day, 12, tzinfo=timezone.utc)
        with mock.patch.object(ec, "datetime", wraps=datetime) as fake:
            fake.now.return_value = fake_now
            return ec.get_next_week_range()

    def test_any_day_of_the_week_maps_to_next_monday_to_friday(self):
        expected = (date(2026, 10, 12), date(2026, 10, 16))
        for day in (5, 9, 11):  # Monday, Friday, Sunday of the same week
            self.assertEqual(self.range_on(2026, 10, day), expected)

    def test_crosses_year_boundary(self):
        self.assertEqual(self.range_on(2026, 12, 31), (date(2027, 1, 4), date(2027, 1, 8)))


class GroupByDayTest(unittest.TestCase):
    CAPS = {"BIG": 500 * BILLION, "MID": 50 * BILLION, "SMALL": 1 * BILLION, "LATE": 200 * BILLION}

    def group(self, entries):
        def fake_profile(symbol, cache):
            return {"name": f"{symbol} Inc", "market_cap": self.CAPS.get(symbol, 20 * BILLION)}

        with mock.patch.object(ec, "fetch_profile", side_effect=fake_profile) as profile:
            grouped = ec.group_by_day(entries, MONDAY)
        return grouped, profile

    def test_filters_small_caps_and_orders_by_session_then_market_cap(self):
        entries = [
            {"date": "2026-10-12", "symbol": "LATE", "hour": "amc"},
            {"date": "2026-10-12", "symbol": "MID", "hour": "bmo"},
            {"date": "2026-10-12", "symbol": "SMALL", "hour": "bmo"},
            {"date": "2026-10-12", "symbol": "BIG", "hour": "bmo"},
        ]
        grouped, _ = self.group(entries)
        self.assertEqual([c["ticker"] for c in grouped["Mon"]], ["$BIG", "$MID", "$LATE"])
        self.assertEqual(grouped["Tue"], [])

    def test_out_of_week_and_incomplete_entries_cost_no_profile_request(self):
        entries = [
            {"date": "2026-10-11", "symbol": "BIG"},   # Sunday before
            {"date": "2026-10-17", "symbol": "BIG"},   # Saturday after
            {"date": "2026-10-13", "symbol": None},
            {"symbol": "BIG"},
        ]
        grouped, profile = self.group(entries)
        self.assertTrue(all(not day for day in grouped.values()))
        profile.assert_not_called()

    def test_caps_companies_per_day(self):
        entries = [{"date": "2026-10-16", "symbol": f"S{i}", "hour": "bmo"} for i in range(40)]
        grouped, _ = self.group(entries)
        self.assertEqual(len(grouped["Fri"]), ec.MAX_COMPANIES_PER_DAY)


class LayoutTest(unittest.TestCase):
    def item(self, ticker, hour, cap=20 * BILLION):
        return {"ticker": ticker, "name": ticker, "hour": hour, "market_cap": cap}

    def test_market_cap_labels(self):
        self.assertEqual(ec.format_market_cap(245 * BILLION), "$245B")
        self.assertEqual(ec.format_market_cap(1200 * BILLION), "$1.2T")
        self.assertEqual(ec.format_market_cap(ec.MIN_MARKET_CAP), "$10B")

    def test_sessions_keep_display_order_and_drop_empty_ones(self):
        items = [self.item("$A", "amc"), self.item("$B", "bmo"), self.item("$C", "dmh"), self.item("$D", "bmo")]
        sessions = ec.split_sessions(items)
        self.assertEqual([key for key, _ in sessions], ["bmo", "amc", ""])
        self.assertEqual([[i["ticker"] for i in group] for _, group in sessions], [["$B", "$D"], ["$A"], ["$C"]])
        self.assertEqual(ec.split_sessions([self.item("$A", "amc")]), [("amc", [self.item("$A", "amc")])])

    def test_column_height_grows_with_reports(self):
        empty = ec.day_content_height([])
        one = ec.day_content_height([self.item("$A", "bmo")])
        two_sessions = ec.day_content_height([self.item("$A", "bmo"), self.item("$B", "amc")])
        self.assertEqual(empty, ec.ROW_HEIGHT)
        self.assertAlmostEqual(one, ec.SESSION_HEIGHT + ec.ROW_HEIGHT)
        self.assertAlmostEqual(two_sessions, 2 * (ec.SESSION_HEIGHT + ec.ROW_HEIGHT))

    def test_nothing_is_drawn_for_an_empty_week(self):
        self.assertFalse(ec.draw_card({day: [] for day in ec.DAY_LABELS}, MONDAY))


if __name__ == "__main__":
    unittest.main()
