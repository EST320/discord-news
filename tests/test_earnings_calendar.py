import unittest
from datetime import date, datetime, timezone
from unittest import mock

from matplotlib.font_manager import FontProperties

from market_pulse import earnings_calendar as ec
from market_pulse import theme

MONDAY = date(2026, 10, 12)
BILLION = 1_000_000_000


def company(symbol, cap_billions, hour="bmo", eps_forecast=None, last_year_eps=None):
    return {"symbol": symbol, "name": f"{symbol} Inc", "market_cap": cap_billions * BILLION,
            "hour": hour, "eps_forecast": eps_forecast, "last_year_eps": last_year_eps}


def item(ticker, hour, cap_billions=50, **extra):
    return {"ticker": ticker, "name": ticker, "hour": hour, "market_cap": cap_billions * BILLION, **extra}


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

    def test_explicit_date(self):
        self.assertEqual(ec.get_next_week_range(date(2026, 10, 6)), (date(2026, 10, 12), date(2026, 10, 16)))


class SelectCompaniesTest(unittest.TestCase):
    MILLION = 1_000_000

    def volumes(self, **billions):
        return {symbol: value * BILLION for symbol, value in billions.items()}

    def test_ranks_by_trading_activity_not_by_size(self):
        companies = [company("SHEL", 263), company("COIN", 50), company("AAPL", 4900)]
        shown, hidden = ec.select_companies(companies, self.volumes(SHEL=0.7, COIN=1.8, AAPL=13.9))
        self.assertEqual([c["ticker"] for c in shown], ["$AAPL", "$COIN", "$SHEL"])
        self.assertEqual(shown[1]["dollar_volume"], 1.8 * BILLION)
        self.assertEqual(hidden, 0)

    def test_market_cap_floor(self):
        companies = [company("SMALL", 4.9), company("EDGE", ec.MIN_MARKET_CAP / BILLION)]
        shown, hidden = ec.select_companies(companies, self.volumes(SMALL=9, EDGE=1))
        self.assertEqual([c["ticker"] for c in shown], ["$EDGE"])
        self.assertEqual(hidden, 0)

    def test_thinly_traded_companies_are_left_out_even_in_a_quiet_week(self):
        companies = [company("JPM", 884), company("HOMB", 5.5), company("BUD", 148)]
        volume = {"JPM": 2.9 * BILLION, "HOMB": 30 * self.MILLION, "BUD": ec.MIN_DOLLAR_VOLUME - 1}
        self.assertEqual(ec.MIN_DOLLAR_VOLUME, 500 * self.MILLION)
        shown, hidden = ec.select_companies(companies, volume)
        self.assertEqual([c["ticker"] for c in shown], ["$JPM"])
        self.assertEqual(hidden, 2)

    def test_busy_day_keeps_the_most_traded_and_counts_the_rest(self):
        companies = [company(f"S{i}", 100) for i in range(40)]
        volume = {f"S{i}": (i + 1) * BILLION for i in range(40)}
        shown, hidden = ec.select_companies(companies, volume)
        self.assertEqual(len(shown), ec.MAX_COMPANIES_PER_DAY)
        self.assertEqual(shown[0]["ticker"], "$S39")
        self.assertEqual(hidden, 40 - ec.MAX_COMPANIES_PER_DAY)

    def test_watchlist_is_always_shown_whatever_its_size_or_volume(self):
        with mock.patch.object(ec, "WATCHLIST", {"INFY", "TINY"}):
            companies = [company("JPM", 884), company("INFY", 44), company("TINY", 1), company("COLD", 300)]
            volume = {"JPM": 2.9 * BILLION, "INFY": 0.2 * BILLION, "TINY": 0.01 * BILLION, "COLD": 0.1 * BILLION}
            shown, hidden = ec.select_companies(companies, volume)
        self.assertEqual([c["ticker"] for c in shown], ["$JPM", "$INFY", "$TINY"])
        self.assertEqual(hidden, 1)

    def test_watchlist_takes_places_from_the_least_traded_on_a_busy_day(self):
        with mock.patch.object(ec, "WATCHLIST", {"QUIET"}):
            companies = [company(f"S{i}", 100) for i in range(40)] + [company("QUIET", 20)]
            volume = {f"S{i}": (i + 1) * BILLION for i in range(40)}
            volume["QUIET"] = 0.1 * BILLION
            shown, _ = ec.select_companies(companies, volume)
            listed = ec.shortlist(companies, volume)
        self.assertEqual(len(shown), ec.MAX_COMPANIES_PER_DAY)
        self.assertEqual(shown[-1]["ticker"], "$QUIET")
        self.assertEqual(shown[0]["ticker"], "$S39")
        # It must also survive the shortlist, or its 20-day volume is never looked up.
        self.assertEqual(listed[0]["symbol"], "QUIET")

    def test_default_watchlist(self):
        self.assertEqual(ec.WATCHLIST, {"INFY", "ERIC", "IBN", "AA"})

    def test_latest_session_volume_stands_in_for_a_missing_average(self):
        companies = [company("AVG", 50), company("ONEDAY", 50)]
        shown, _ = ec.select_companies(companies, self.volumes(AVG=1.0), self.volumes(AVG=9.0, ONEDAY=2.0))
        self.assertEqual([(c["ticker"], c["dollar_volume"]) for c in shown],
                         [("$ONEDAY", 2.0 * BILLION), ("$AVG", 1.0 * BILLION)])

    def test_without_any_volume_data_it_falls_back_to_the_largest(self):
        shown, hidden = ec.select_companies([company("MID", 50), company("BIG", 500), company("TINY", 1)], {})
        self.assertEqual([c["ticker"] for c in shown], ["$BIG", "$MID"])
        self.assertEqual(hidden, 0)

    def test_shortlist_uses_one_session_of_volume_then_size(self):
        companies = [company(f"S{i}", 100 + i) for i in range(50)] + [company("TINY", 1)]
        by_volume = ec.shortlist(companies, {"S0": 5 * BILLION, "S1": 9 * BILLION})
        self.assertEqual(len(by_volume), ec.SHORTLIST_PER_DAY)
        self.assertEqual([c["symbol"] for c in by_volume[:3]], ["S1", "S0", "S49"])
        self.assertEqual(ec.shortlist(companies, {})[0]["symbol"], "S49")


class LoadWeekTest(unittest.TestCase):
    def load(self, screener):
        def fake_earnings(day):
            if day == date(2026, 10, 13):
                return [company("HOT", 50), company("COLD", 300), company("TINY", 1)]
            return []

        patches = (
            mock.patch.object(ec.nasdaq, "fetch_earnings", side_effect=fake_earnings),
            mock.patch.object(ec.nasdaq, "fetch_dollar_volumes", **screener),
            mock.patch.object(ec.yahoo, "average_dollar_volumes",
                              return_value={"HOT": 3 * BILLION, "COLD": 0.5 * BILLION}),
            mock.patch("builtins.print"),
        )
        with patches[0] as earnings, patches[1], patches[2] as averages, patches[3]:
            grouped, hidden = ec.load_week(MONDAY)
        return grouped, hidden, earnings, averages

    def test_week_is_fetched_one_day_at_a_time_and_ranked_by_average_volume(self):
        grouped, hidden, earnings, averages = self.load({"return_value": {"HOT": 1.0, "COLD": 2.0}})
        self.assertEqual([call.args[0] for call in earnings.call_args_list],
                         [date(2026, 10, d) for d in (12, 13, 14, 15, 16)])
        # Only companies above the cap floor are looked up, once each.
        averages.assert_called_once_with(["COLD", "HOT"], ec.DOLLAR_VOLUME_DAYS)
        self.assertEqual([c["ticker"] for c in grouped["Tue"]], ["$HOT", "$COLD"])
        self.assertEqual([len(grouped[day]) for day in ec.DAY_LABELS], [0, 2, 0, 0, 0])
        self.assertEqual(hidden["Tue"], 0)

    def test_screener_failure_does_not_stop_the_calendar(self):
        grouped, _, _, _ = self.load({"side_effect": RuntimeError("blocked")})
        self.assertEqual([c["ticker"] for c in grouped["Tue"]], ["$HOT", "$COLD"])


class FormattingTest(unittest.TestCase):
    def test_market_cap_labels(self):
        self.assertEqual(ec.format_market_cap(245 * BILLION), "$245B")
        self.assertEqual(ec.format_market_cap(1200 * BILLION), "$1.2T")
        self.assertEqual(ec.format_market_cap(ec.MIN_MARKET_CAP), "$5B")

    def test_eps_labels(self):
        self.assertEqual(ec.format_eps(5.94), "$5.94")
        self.assertEqual(ec.format_eps(-0.15), "-$0.15")

    def test_eps_trend_against_last_year(self):
        self.assertEqual(ec.eps_trend({"eps_forecast": 5.94, "last_year_eps": 5.07}), 1)
        self.assertEqual(ec.eps_trend({"eps_forecast": 0.14, "last_year_eps": 0.20}), -1)
        self.assertEqual(ec.eps_trend({"eps_forecast": 1.00, "last_year_eps": 1.00}), 0)
        self.assertEqual(ec.eps_trend({"eps_forecast": 1.00, "last_year_eps": None}), 0)
        self.assertEqual(ec.eps_trend({"eps_forecast": None, "last_year_eps": 1.00}), 0)


class LayoutTest(unittest.TestCase):
    def test_sessions_keep_display_order_and_drop_empty_ones(self):
        items = [item("$A", "amc"), item("$B", "bmo"), item("$C", "dmh"), item("$D", "bmo")]
        sessions = ec.split_sessions(items)
        self.assertEqual([key for key, _ in sessions], ["bmo", "amc", ""])
        self.assertEqual([[i["ticker"] for i in group] for _, group in sessions], [["$B", "$D"], ["$A"], ["$C"]])

    def test_column_height_grows_with_reports_and_the_more_line(self):
        self.assertEqual(ec.day_content_height([]), ec.ROW_HEIGHT)
        one = ec.day_content_height([item("$A", "bmo")])
        self.assertAlmostEqual(one, ec.SESSION_HEIGHT + ec.ROW_HEIGHT)
        self.assertAlmostEqual(ec.day_content_height([item("$A", "bmo"), item("$B", "amc")]),
                               2 * (ec.SESSION_HEIGHT + ec.ROW_HEIGHT))
        self.assertAlmostEqual(ec.day_content_height([item("$A", "bmo")], hidden=5), one + ec.MORE_HEIGHT)

    def test_full_name_is_wrapped_never_cut(self):
        font = FontProperties(size=9.5)
        name = "Taiwan Semiconductor Manufacturing Company Limited"
        lines, _ = ec.layout_name(name, "$2.5T", font, font, 1.84)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), name)
        self.assertNotIn("…", "".join(lines))

    def test_market_cap_shares_the_last_line_only_when_it_fits(self):
        font = FontProperties(size=9.5)
        self.assertEqual(ec.layout_name("Marsh", "$81B", font, font, 1.84), (["Marsh"], True))
        # A name that fills its line leaves no room, so the cap moves below it.
        full_line = "W"
        while theme.text_width(full_line + "W", font) <= 1.84 - 0.08:  # the wrap margin
            full_line += "W"
        self.assertEqual(ec.layout_name(full_line, "$40B", font, font, 1.84), ([full_line], False))

    def test_row_height_follows_the_layout(self):
        short = item("$A", "bmo", layout=(["One line"], True))
        wrapped = item("$B", "bmo", layout=(["First line", "second line"], True))
        cap_below = item("$C", "bmo", layout=(["Fills the whole line"], False))
        self.assertEqual(ec.row_height(item("$D", "bmo")), ec.ROW_HEIGHT)
        self.assertEqual(ec.row_height(short), ec.ROW_HEIGHT)
        self.assertAlmostEqual(ec.row_height(wrapped), ec.ROW_HEIGHT + ec.NAME_LINE_HEIGHT)
        self.assertAlmostEqual(ec.row_height(cap_below), ec.ROW_HEIGHT + ec.NAME_LINE_HEIGHT)
        self.assertAlmostEqual(ec.day_content_height([short, wrapped]),
                               ec.SESSION_HEIGHT + 2 * ec.ROW_HEIGHT + ec.NAME_LINE_HEIGHT)

    def test_nothing_is_drawn_for_an_empty_week(self):
        self.assertFalse(ec.draw_card({day: [] for day in ec.DAY_LABELS}, MONDAY))


class MainTest(unittest.TestCase):
    def run_main(self, drawn, dry_run=False):
        patches = (
            mock.patch.object(ec, "load_week", return_value=({day: [] for day in ec.DAY_LABELS}, {})),
            mock.patch.object(ec, "draw_card", return_value=drawn),
            mock.patch.object(ec, "post_to_discord"),
            mock.patch.object(ec, "DRY_RUN", dry_run),
            mock.patch.dict("os.environ", {ec.WEBHOOK_ENV: "hook"}),
            mock.patch("builtins.print"),
        )
        with patches[0], patches[1], patches[2] as post, patches[3], patches[4], patches[5]:
            ec.main()
        return post

    def test_posts_when_there_is_a_card(self):
        self.run_main(drawn=True).assert_called_once()

    def test_stays_quiet_when_nothing_qualifies(self):
        self.run_main(drawn=False).assert_not_called()

    def test_dry_run_posts_nothing(self):
        self.run_main(drawn=True, dry_run=True).assert_not_called()


if __name__ == "__main__":
    unittest.main()
