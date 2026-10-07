import unittest
from datetime import date
from unittest import mock

from market_pulse import ipo_calendar as ic

MONDAY, FRIDAY = date(2026, 10, 12), date(2026, 10, 16)


def deal(day, symbol, size=0, name=None, month=10, **extra):
    return {
        "date": date(2026, month, day),
        "symbol": symbol,
        "name": f"{symbol} Corp" if name is None else name,
        "exchange": "NYSE",
        "price": "10.00",
        "shares": 1_000_000.0,
        "deal_size": float(size),
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

    def test_spac_detection(self):
        self.assertTrue(ic.is_spac("AfterNext Acquisition I Corp."))
        self.assertTrue(ic.is_spac("PINE TREE ACQUISITION CORP"))
        self.assertFalse(ic.is_spac("TRex Bio, Inc."))


class ListingWindowTest(unittest.TestCase):
    def test_runs_from_today_through_next_friday(self):
        # Tuesday, the usual Saturday run, and a Friday.
        self.assertEqual(ic.listing_window(date(2026, 10, 6)), (date(2026, 10, 6), date(2026, 10, 16)))
        self.assertEqual(ic.listing_window(date(2026, 10, 10)), (date(2026, 10, 10), date(2026, 10, 16)))
        self.assertEqual(ic.listing_window(date(2026, 10, 9)), (date(2026, 10, 9), date(2026, 10, 16)))

    def test_months_the_window_touches(self):
        self.assertEqual(ic.months_in(date(2026, 10, 6), date(2026, 10, 16)), ["2026-10"])
        self.assertEqual(ic.months_in(date(2026, 10, 28), date(2026, 11, 6)), ["2026-10", "2026-11"])
        self.assertEqual(ic.months_in(date(2026, 12, 30), date(2027, 1, 8)), ["2026-12", "2027-01"])

    def test_both_months_are_fetched_when_the_window_spans_two(self):
        with mock.patch.object(ic.nasdaq, "fetch_upcoming_ipos", side_effect=[[deal(30, "OCT")], [deal(3, "NOV", month=11)]]) as fetch:
            deals = ic.fetch_deals(date(2026, 10, 28), date(2026, 11, 6))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["2026-10", "2026-11"])
        self.assertEqual([d["symbol"] for d in deals], ["OCT", "NOV"])


class SelectListingsTest(unittest.TestCase):
    def symbols(self, deals, start=MONDAY, end=FRIDAY):
        return [item["symbol"] for item in ic.select_listings(deals, start, end)]

    def test_orders_by_date_then_largest_deal(self):
        self.assertEqual(self.symbols([deal(16, "LATE", 900), deal(13, "SMALL", 10), deal(13, "BIG", 500)]),
                         ["BIG", "SMALL", "LATE"])

    def test_drops_deals_outside_the_window(self):
        self.assertEqual(self.symbols([deal(11, "SUN"), deal(17, "SAT"), deal(12, "MON"), deal(16, "FRI")]),
                         ["MON", "FRI"])

    def test_this_weeks_remaining_deals_are_included(self):
        start, end = ic.listing_window(date(2026, 10, 6))
        deals = [deal(5, "PAST"), deal(7, "THISWEEK"), deal(13, "NEXTWEEK"), deal(19, "LATER")]
        self.assertEqual(self.symbols(deals, start, end), ["THISWEEK", "NEXTWEEK"])

    def test_duplicates_from_overlapping_months_are_dropped(self):
        self.assertEqual(self.symbols([deal(13, "DUP", 5), deal(13, "DUP", 5)]), ["DUP"])

    def test_drops_deals_with_neither_name_nor_symbol(self):
        listings = ic.select_listings([deal(13, "", name=""), deal(13, "", name="Nameless SPAC")], MONDAY, FRIDAY)
        self.assertEqual([i["name"] for i in listings], ["Nameless SPAC"])

    def test_names_are_tidied_and_spacs_flagged(self):
        listings = ic.select_listings(
            [deal(13, "ACRB", 9, name="ACME ROBOTICS INC"), deal(13, "PAXGU", 5, name="Pine Tree Acquisition Corp.")],
            MONDAY, FRIDAY,
        )
        self.assertEqual([(i["name"], i["spac"]) for i in listings],
                         [("Acme Robotics Inc", False), ("Pine Tree Acquisition Corp.", True)])


class LayoutTest(unittest.TestCase):
    def listings(self):
        return ic.select_listings([deal(13, "BIG", 500), deal(13, "SMALL", 10), deal(15, "THU", 5)], MONDAY, FRIDAY)

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


class EmptyCalendarTest(unittest.TestCase):
    def run_main(self, dry_run):
        patches = (
            mock.patch.object(ic, "fetch_deals", return_value=[]),
            mock.patch.object(ic, "post_webhook"),
            mock.patch.object(ic, "DRY_RUN", dry_run),
            mock.patch.dict("os.environ", {ic.WEBHOOK_ENV: "hook"}),
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


if __name__ == "__main__":
    unittest.main()
