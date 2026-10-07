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
    def test_floor_drops_small_companies_and_largest_come_first(self):
        shown, hidden = ec.select_companies([company("MID", 50), company("SMALL", 19.9), company("BIG", 500)])
        self.assertEqual([c["ticker"] for c in shown], ["$BIG", "$MID"])
        self.assertEqual(hidden, 0)

    def test_floor_is_inclusive(self):
        shown, _ = ec.select_companies([company("EDGE", ec.MIN_MARKET_CAP / BILLION)])
        self.assertEqual(len(shown), 1)

    def test_busy_day_keeps_the_largest_and_counts_the_rest(self):
        companies = [company(f"S{i}", 100 + i) for i in range(40)] + [company("TINY", 1)]
        shown, hidden = ec.select_companies(companies)
        self.assertEqual(len(shown), ec.MAX_COMPANIES_PER_DAY)
        self.assertEqual(shown[0]["ticker"], "$S39")
        self.assertEqual(hidden, 40 - ec.MAX_COMPANIES_PER_DAY)

    def test_week_is_fetched_one_day_at_a_time(self):
        def fake_fetch(day):
            return [company("ONLY", 100)] if day == date(2026, 10, 13) else []

        with mock.patch.object(ec.nasdaq, "fetch_earnings", side_effect=fake_fetch) as fetch:
            grouped, hidden = ec.load_week(MONDAY)

        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [date(2026, 10, d) for d in (12, 13, 14, 15, 16)])
        self.assertEqual([len(grouped[day]) for day in ec.DAY_LABELS], [0, 1, 0, 0, 0])
        self.assertEqual(set(hidden.values()), {0})


class FormattingTest(unittest.TestCase):
    def test_market_cap_labels(self):
        self.assertEqual(ec.format_market_cap(245 * BILLION), "$245B")
        self.assertEqual(ec.format_market_cap(1200 * BILLION), "$1.2T")
        self.assertEqual(ec.format_market_cap(ec.MIN_MARKET_CAP), "$20B")

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
