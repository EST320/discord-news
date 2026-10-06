import unittest
from datetime import date
from unittest import mock

from market_pulse import ipo_calendar as ic

MONDAY, FRIDAY = date(2026, 10, 12), date(2026, 10, 16)


def entry(day, symbol, size=0, status="expected", **extra):
    return {
        "date": f"2026-10-{day:02d}",
        "symbol": symbol,
        "name": f"{symbol} Corp",
        "status": status,
        "totalSharesValue": size,
        **extra,
    }


class FormattingTest(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(ic.format_amount(2_040_000_000, "$"), "$2.0B")
        self.assertEqual(ic.format_amount(25_000_000), "25.0M")
        self.assertEqual(ic.format_amount("7500"), "7.5K")
        self.assertEqual(ic.format_amount(950), "950")
        for missing in (None, 0, "", "n/a"):
            self.assertEqual(ic.format_amount(missing), "-")

    def test_prices(self):
        self.assertEqual(ic.format_price("18.00-21.00"), "$18.00 - 21.00")
        self.assertEqual(ic.format_price("15"), "$15.00")
        self.assertEqual(ic.format_price(12.5), "$12.50")
        self.assertEqual(ic.format_price(None), "-")

    def test_price_range_has_one_dollar_sign(self):
        self.assertEqual(ic.format_price("4.00-6.00").count("$"), 1)


class ListingWindowTest(unittest.TestCase):
    def test_runs_from_today_through_next_friday(self):
        # Tuesday, the usual Saturday run, and a Friday.
        self.assertEqual(ic.listing_window(date(2026, 10, 6)), (date(2026, 10, 6), date(2026, 10, 16)))
        self.assertEqual(ic.listing_window(date(2026, 10, 10)), (date(2026, 10, 10), date(2026, 10, 16)))
        self.assertEqual(ic.listing_window(date(2026, 10, 9)), (date(2026, 10, 9), date(2026, 10, 16)))

    def test_this_weeks_remaining_deals_are_included(self):
        start, end = ic.listing_window(date(2026, 10, 6))
        entries = [entry(5, "PAST"), entry(7, "THISWEEK"), entry(13, "NEXTWEEK"), entry(19, "LATER")]
        self.assertEqual([i["symbol"] for i in ic.select_listings(entries, start, end)], ["THISWEEK", "NEXTWEEK"])


class EmptyCalendarTest(unittest.TestCase):
    def run_main(self, dry_run):
        patches = (
            mock.patch.object(ic, "fetch_ipos", return_value=[]),
            mock.patch.object(ic, "post_webhook"),
            mock.patch.object(ic, "DRY_RUN", dry_run),
            mock.patch.dict("os.environ", {ic.FINNHUB_KEY_ENV: "k", ic.WEBHOOK_ENV: "hook"}),
            mock.patch("builtins.print"),
        )
        with patches[0], patches[1] as post, patches[2], patches[3], patches[4]:
            ic.main()
        return post

    def test_an_empty_week_is_announced_instead_of_staying_silent(self):
        post = self.run_main(dry_run=False)
        payload = post.call_args.args[1]
        self.assertIn("No IPOs are scheduled", payload["embeds"][0]["description"])
        self.assertTrue(payload["embeds"][0]["title"].startswith("IPO Calendar"))

    def test_dry_run_posts_nothing(self):
        self.run_main(dry_run=True).assert_not_called()


class SelectListingsTest(unittest.TestCase):
    def symbols(self, entries):
        return [item["symbol"] for item in ic.select_listings(entries, MONDAY, FRIDAY)]

    def test_orders_by_date_then_largest_deal(self):
        entries = [entry(16, "LATE", 900), entry(13, "SMALL", 10), entry(13, "BIG", 500)]
        self.assertEqual(self.symbols(entries), ["BIG", "SMALL", "LATE"])

    def test_keeps_only_expected_and_priced(self):
        entries = [
            entry(13, "EXP"), entry(13, "PRC", status="priced"), entry(13, "UPPER", status="EXPECTED"),
            entry(13, "FILED", status="filed"), entry(13, "GONE", status="withdrawn"), entry(13, "NONE", status=None),
        ]
        self.assertEqual(sorted(self.symbols(entries)), ["EXP", "PRC", "UPPER"])

    def test_drops_entries_outside_the_week_or_without_a_date(self):
        entries = [entry(11, "SUN"), entry(17, "SAT"), entry(12, "MON"), {**entry(13, "BAD"), "date": "soon"}]
        self.assertEqual(self.symbols(entries), ["MON"])

    def test_drops_entries_with_neither_name_nor_symbol(self):
        entries = [{**entry(13, ""), "name": ""}, {**entry(13, ""), "name": "Nameless SPAC"}]
        self.assertEqual([i["name"] for i in ic.select_listings(entries, MONDAY, FRIDAY)], ["Nameless SPAC"])

    def test_all_caps_names_are_title_cased(self):
        listing = ic.select_listings([{**entry(13, "ACRB"), "name": "ACME ROBOTICS INC"}], MONDAY, FRIDAY)[0]
        self.assertEqual(listing["name"], "Acme Robotics Inc")


class LayoutTest(unittest.TestCase):
    def listings(self):
        return ic.select_listings(
            [entry(13, "BIG", 500), entry(13, "SMALL", 10), entry(15, "THU", 5)], MONDAY, FRIDAY
        )

    def test_listings_are_grouped_under_their_date(self):
        groups = ic.group_by_date(self.listings())
        self.assertEqual([day for day, _ in groups], [date(2026, 10, 13), date(2026, 10, 15)])
        self.assertEqual([[i["symbol"] for i in items] for _, items in groups], [["BIG", "SMALL"], ["THU"]])
        self.assertEqual(ic.group_by_date([]), [])

    def test_card_height_grows_with_days_and_rows(self):
        one_day = ic.group_by_date(self.listings()[:2])
        two_days = ic.group_by_date(self.listings())
        extra = ic.card_height(two_days) - ic.card_height(one_day)
        self.assertAlmostEqual(extra, ic.DAY_HEIGHT + ic.ROW_HEIGHT + ic.ROW_GAP)


if __name__ == "__main__":
    unittest.main()
