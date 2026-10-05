import unittest
from datetime import date, datetime, timezone
from unittest import mock

from discord_news import earnings_calendar as ec

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


class FormatCellTest(unittest.TestCase):
    def test_cell_markup(self):
        item = {"ticker": "$PEP", "name": "PepsiCo Inc", "hour": "bmo"}
        self.assertEqual(ec.format_cell(item), f"<b>$PEP</b> {ec.ICON_MAP['bmo']}<br>PepsiCo Inc")
        self.assertEqual(ec.format_cell({"ticker": "$X", "name": "X", "hour": ""}), "<b>$X</b><br>X")
        self.assertEqual(ec.format_cell(None), "")


if __name__ == "__main__":
    unittest.main()
