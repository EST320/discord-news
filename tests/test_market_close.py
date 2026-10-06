import unittest

from market_pulse import market_close as mc

DAY = 86400


def series(*closes, last_ts=1_000_000):
    count = len(closes)
    return {
        "timestamp": [last_ts - DAY * (count - 1 - i) for i in range(count)],
        "close": list(closes),
    }


class ParseQuoteTest(unittest.TestCase):
    def test_change_is_measured_from_the_previous_close(self):
        quote = mc.parse_quote(series(90, 100, 102))
        self.assertEqual(quote["price"], 102)
        self.assertAlmostEqual(quote["change"], 2)
        self.assertAlmostEqual(quote["change_pct"], 2.0)
        self.assertEqual(quote["bar_ts"], 1_000_000)

    def test_missing_closes_are_skipped(self):
        quote = mc.parse_quote(series(100, None, 110))
        self.assertAlmostEqual(quote["change_pct"], 10.0)

    def test_unusable_series(self):
        for entry in (None, {}, series(100), series(None, 100), series(0, 100), {"timestamp": None, "close": None}):
            self.assertIsNone(mc.parse_quote(entry), entry)


class MarketTradedTodayTest(unittest.TestCase):
    def quotes(self, bar_age_hours, now=2_000_000):
        return {"^GSPC": {"bar_ts": now - bar_age_hours * 3600}}, now

    def test_bar_from_this_session(self):
        quotes, now = self.quotes(8)
        self.assertTrue(mc.market_traded_today(quotes, now))

    def test_holiday_or_weekend_bar_is_stale(self):
        quotes, now = self.quotes(32)
        self.assertFalse(mc.market_traded_today(quotes, now))

    def test_no_index_data(self):
        self.assertFalse(mc.market_traded_today({}, 2_000_000))


class FormattingTest(unittest.TestCase):
    def test_percent_and_color(self):
        self.assertEqual(mc.format_pct(0.584), "+0.58%")
        self.assertEqual(mc.format_pct(-3.2), "-3.20%")
        self.assertEqual(mc.change_color(1), mc.UP)
        self.assertEqual(mc.change_color(-1), mc.DOWN)
        self.assertEqual(mc.change_color(0.001), mc.FLAT)

    def test_yield_moves_are_shown_in_basis_points(self):
        quote = {"change": -0.042, "change_pct": -0.79}
        self.assertEqual(mc.format_macro_change(quote, "bps"), "-4.2 bps")
        self.assertEqual(mc.format_macro_change(quote, "pct"), "-0.79%")

    def test_summary_lists_available_indices_in_order(self):
        quotes = {"^DJI": {"change_pct": 0.49}, "^GSPC": {"change_pct": 0.58}}
        self.assertEqual(mc.build_summary(quotes), "S&P 500 +0.58% · Dow Jones +0.49%")

    def test_every_symbol_is_requested_once(self):
        self.assertEqual(len(mc.ALL_SYMBOLS), len(set(mc.ALL_SYMBOLS)))
        self.assertEqual(len(mc.ALL_SYMBOLS), 20)


if __name__ == "__main__":
    unittest.main()
