import unittest
from datetime import date
from unittest import mock

from market_pulse import market_close as mc

DAY = 86400


def series(*closes, last_ts=1_000_000):
    count = len(closes)
    return {
        "timestamp": [last_ts - DAY * (count - 1 - i) for i in range(count)],
        "close": list(closes),
    }


def quote(price, previous):
    return mc.parse_quote(series(previous, price))


def sample_quotes(**overrides):
    """A quote for every symbol: +1% by default, overridable per symbol."""
    quotes = {symbol: quote(101, 100) for symbol in mc.ALL_SYMBOLS}
    quotes.update(overrides)
    return quotes


class ParseQuoteTest(unittest.TestCase):
    def test_change_is_measured_from_the_previous_close(self):
        q = mc.parse_quote(series(90, 100, 102))
        self.assertEqual((q["price"], q["previous"], q["bar_ts"]), (102, 100, 1_000_000))
        self.assertAlmostEqual(q["change"], 2)
        self.assertAlmostEqual(q["change_pct"], 2.0)

    def test_missing_closes_are_skipped(self):
        self.assertAlmostEqual(mc.parse_quote(series(100, None, 110))["change_pct"], 10.0)

    def test_unusable_series(self):
        for entry in (None, {}, series(100), series(None, 100), series(0, 100), {"timestamp": None, "close": None}):
            self.assertIsNone(mc.parse_quote(entry), entry)

    def test_intraday_needs_a_handful_of_points(self):
        self.assertEqual(mc.parse_intraday({"close": [1, None, 2, 3, 4, 5]}), [1, 2, 3, 4, 5])
        self.assertEqual(mc.parse_intraday({"close": [1, 2]}), [])
        self.assertEqual(mc.parse_intraday(None), [])

    def test_range_is_the_low_and_high_close(self):
        closes = [None, 5.0] + [10.0] * 30 + [42.5, 7.0]
        self.assertEqual(mc.parse_range({"close": closes}), (5.0, 42.5))
        self.assertIsNone(mc.parse_range({"close": [1, 2, 3]}))
        self.assertIsNone(mc.parse_range(None))


class FetchSparkTest(unittest.TestCase):
    def test_symbols_are_requested_in_batches_and_merged(self):
        def fake_get(url, params, **kwargs):
            response = mock.Mock()
            response.json.return_value = {s: series(1, 2) for s in params["symbols"].split(",")}
            return response

        with mock.patch.object(mc.requests, "get", side_effect=fake_get) as get:
            payload = mc.fetch_spark(mc.ALL_SYMBOLS, "5d", "1d")

        self.assertEqual(set(payload), set(mc.ALL_SYMBOLS))
        batch_sizes = [len(call.kwargs["params"]["symbols"].split(",")) for call in get.call_args_list]
        self.assertTrue(all(size <= mc.SPARK_BATCH_SIZE for size in batch_sizes))
        self.assertEqual(sum(batch_sizes), len(mc.ALL_SYMBOLS))

    def test_decoration_fetch_failures_do_not_block_the_summary(self):
        with mock.patch.object(mc, "fetch_spark", side_effect=RuntimeError("down")), \
                mock.patch("builtins.print"):
            self.assertEqual(mc.load_intraday(), {})
            self.assertEqual(mc.load_ranges(), {})

    def test_ranges_cover_the_macro_symbols_and_skip_thin_data(self):
        year = {"close": [10 + i % 7 for i in range(250)]}
        payload = {symbol: year for symbol in mc.RANGE_SYMBOLS}
        payload["BTC-USD"] = {"close": [1, 2, 3]}
        with mock.patch.object(mc, "fetch_spark", return_value=payload) as fetch:
            ranges = mc.load_ranges()
        fetch.assert_called_once_with(mc.RANGE_SYMBOLS, "1y", "1d")
        self.assertEqual(ranges["^VIX"], (10, 16))
        self.assertNotIn("BTC-USD", ranges)


class MarketTradedTodayTest(unittest.TestCase):
    def test_bar_from_this_session(self):
        self.assertTrue(mc.market_traded_today({"^GSPC": {"bar_ts": 2_000_000 - 8 * 3600}}, 2_000_000))

    def test_holiday_or_weekend_bar_is_stale(self):
        self.assertFalse(mc.market_traded_today({"^GSPC": {"bar_ts": 2_000_000 - 32 * 3600}}, 2_000_000))

    def test_no_index_data(self):
        self.assertFalse(mc.market_traded_today({}, 2_000_000))


class FormattingTest(unittest.TestCase):
    def test_percent_arrow_and_color(self):
        self.assertEqual(mc.format_pct(0.584), "+0.58%")
        self.assertEqual(mc.format_pct(-3.2), "-3.20%")
        self.assertEqual([mc.arrow(v) for v in (1, -1, 0.001)], ["▲", "▼", "—"])
        self.assertEqual(mc.change_color(1), mc.UP)
        self.assertEqual(mc.change_color(-1), mc.DOWN)
        self.assertEqual(mc.change_color(0.001), mc.FLAT)

    def test_green_means_up(self):
        self.assertFalse(mc.RED_UP)
        self.assertEqual((mc.UP, mc.DOWN), (mc.GREEN, mc.RED))

    def test_yield_moves_are_shown_in_basis_points(self):
        q = {"change": -0.042, "change_pct": -0.79}
        self.assertEqual(mc.format_macro_change(q, "bps"), "-4.2 基点")
        self.assertEqual(mc.format_macro_change(q, "pct"), "-0.79%")

    def test_date_in_chinese(self):
        self.assertEqual(mc.format_date(date(2026, 10, 6)), "2026年10月6日 周二")
        self.assertEqual(mc.format_date(date(2026, 10, 11)), "2026年10月11日 周日")

    def test_heat_grows_with_the_move_and_saturates(self):
        self.assertEqual(mc.heat_color(0, 2), mc.PANEL)
        small, big, huge = (mc.heat_color(pct, 2) for pct in (0.2, 2, 9))
        self.assertNotEqual(small, big)
        self.assertEqual(big, huge)

    def test_symbol_lists(self):
        self.assertEqual(len(mc.ALL_SYMBOLS), len(set(mc.ALL_SYMBOLS)))
        self.assertEqual(len(mc.ALL_SYMBOLS), 23)
        self.assertEqual(mc.INTRADAY_SYMBOLS, ["^GSPC", "^IXIC", "^DJI", "RSP", "SOXX", "IWM"])
        self.assertEqual(mc.RANGE_SYMBOLS, ["^VIX", "^TNX", "DX-Y.NYB", "GC=F", "CL=F", "BTC-USD"])


class RecapTest(unittest.TestCase):
    def test_sectors_sorted_and_counted(self):
        quotes = sample_quotes(XLU=quote(103, 100), XLV=quote(99, 100), XLE=quote(100, 100))
        rows = mc.sector_rows(quotes)
        self.assertEqual((rows[0][0], rows[-1][0]), ("公用事业", "医疗保健"))
        self.assertEqual(mc.breadth(rows), (9, 1))

    def test_embed_only_captions_the_image_with_the_date(self):
        embed = mc.build_embed(sample_quotes(), date(2026, 10, 6), "market_close.png")
        self.assertEqual(embed, {
            "description": "美股收盘 · 2026年10月6日 周二",
            "color": int(mc.UP.lstrip("#"), 16),
            "image": {"url": "attachment://market_close.png"},
        })

    def test_embed_color_follows_the_sp500(self):
        down = mc.build_embed(sample_quotes(**{"^GSPC": quote(99, 100)}), date(2026, 10, 6), "x.png")
        self.assertEqual(down["color"], int(mc.DOWN.lstrip("#"), 16))
        self.assertEqual(mc.build_embed({}, date(2026, 10, 6), "x.png")["color"], int(mc.FLAT.lstrip("#"), 16))


if __name__ == "__main__":
    unittest.main()
