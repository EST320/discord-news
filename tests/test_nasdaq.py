import unittest
from datetime import date
from unittest import mock

import requests

from market_pulse import nasdaq


def response(payload, status=200):
    fake = mock.Mock()
    fake.json.return_value = payload
    fake.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(f"status {status}")
    return fake


def ok(data):
    return response({"data": data, "status": {"rCode": 200}})


class ParseNumberTest(unittest.TestCase):
    def test_money_and_counts(self):
        self.assertEqual(nasdaq.parse_number("$883,527,910,000"), 883527910000.0)
        self.assertEqual(nasdaq.parse_number("8,333,334"), 8333334.0)
        self.assertEqual(nasdaq.parse_number("$5.94"), 5.94)

    def test_negative_in_parentheses(self):
        self.assertEqual(nasdaq.parse_number("($0.12)"), -0.12)

    def test_missing_values(self):
        for text in ("", None, "N/A", "--"):
            self.assertIsNone(nasdaq.parse_number(text), text)


class GetDataTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(nasdaq.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        printer = mock.patch("builtins.print")
        printer.start()
        self.addCleanup(printer.stop)

    def test_sends_browser_like_headers(self):
        with mock.patch.object(nasdaq.requests, "get", return_value=ok({"rows": []})) as get:
            nasdaq.get_data("calendar/earnings", {"date": "2026-10-13"})
        headers = get.call_args.kwargs["headers"]
        self.assertIn("Mozilla", headers["User-Agent"])
        self.assertEqual(headers["Origin"], "https://www.nasdaq.com")

    def test_retries_then_succeeds(self):
        answers = [requests.ConnectionError("reset"), ok({"rows": [1]})]
        with mock.patch.object(nasdaq.requests, "get", side_effect=answers) as get:
            self.assertEqual(nasdaq.get_data("x", {}), {"rows": [1]})
        self.assertEqual(get.call_count, 2)

    def test_gives_up_after_max_retries(self):
        with mock.patch.object(nasdaq.requests, "get", side_effect=requests.ConnectionError("down")) as get:
            with self.assertRaises(requests.ConnectionError):
                nasdaq.get_data("x", {})
        self.assertEqual(get.call_count, nasdaq.MAX_RETRIES)

    def test_error_status_in_the_body_is_a_failure(self):
        bad = response({"data": None, "status": {"rCode": 400}})
        with mock.patch.object(nasdaq.requests, "get", return_value=bad):
            with self.assertRaises(RuntimeError):
                nasdaq.get_data("x", {})

    def test_null_data_becomes_an_empty_dict(self):
        with mock.patch.object(nasdaq.requests, "get", return_value=ok(None)):
            self.assertEqual(nasdaq.get_data("x", {}), {})


class FetchEarningsTest(unittest.TestCase):
    ROWS = [
        {"symbol": "JPM", "name": "J P Morgan Chase & Co", "marketCap": "$883,527,910,000",
         "time": "time-pre-market", "epsForecast": "$5.94", "lastYearEPS": "$5.07"},
        {"symbol": "NFLX", "name": "Netflix, Inc.", "marketCap": "$520,000,000,000",
         "time": "time-after-hours", "epsForecast": "", "lastYearEPS": "($0.10)"},
        {"symbol": "TINY", "name": "", "marketCap": "N/A", "time": "time-not-supplied"},
        {"symbol": "", "name": "No Symbol Inc"},
    ]

    def fetch(self, rows):
        with mock.patch.object(nasdaq, "get_data", return_value={"rows": rows}) as get_data:
            companies = nasdaq.fetch_earnings(date(2026, 10, 13))
        get_data.assert_called_once_with("calendar/earnings", {"date": "2026-10-13"})
        return companies

    def test_rows_are_normalised(self):
        jpm, nflx, tiny = self.fetch(self.ROWS)
        self.assertEqual(jpm, {"symbol": "JPM", "name": "J P Morgan Chase & Co", "market_cap": 883527910000.0,
                               "hour": "bmo", "eps_forecast": 5.94, "last_year_eps": 5.07})
        self.assertEqual((nflx["hour"], nflx["eps_forecast"], nflx["last_year_eps"]), ("amc", None, -0.10))
        self.assertEqual((tiny["name"], tiny["market_cap"], tiny["hour"]), ("TINY", 0.0, ""))

    def test_a_day_without_reports(self):
        self.assertEqual(self.fetch(None), [])


class FetchDollarVolumesTest(unittest.TestCase):
    def test_price_times_volume_per_symbol(self):
        rows = [
            {"symbol": "COIN", "lastsale": "$300.00", "volume": "5,000,000"},
            {"symbol": "HALT", "lastsale": "$10.00", "volume": "0"},
            {"symbol": "", "lastsale": "$1.00", "volume": "10"},
            {"symbol": "BAD", "lastsale": "N/A", "volume": "10"},
        ]
        with mock.patch.object(nasdaq, "get_data", return_value={"rows": rows}) as get_data:
            volumes = nasdaq.fetch_dollar_volumes()
        self.assertEqual(get_data.call_args.args[0], "screener/stocks")
        self.assertEqual(volumes, {"COIN": 1_500_000_000.0})

    def test_empty_screener(self):
        with mock.patch.object(nasdaq, "get_data", return_value={}):
            self.assertEqual(nasdaq.fetch_dollar_volumes(), {})


class FetchUpcomingIposTest(unittest.TestCase):
    def fetch(self, data):
        with mock.patch.object(nasdaq, "get_data", return_value=data) as get_data:
            deals = nasdaq.fetch_upcoming_ipos("2026-10")
        get_data.assert_called_once_with("ipo/calendar", {"date": "2026-10"})
        return deals

    def test_rows_are_normalised_and_undated_ones_dropped(self):
        rows = [
            {"proposedTickerSymbol": "TRXB", "companyName": "TRex Bio, Inc.", "proposedExchange": "NASDAQ Global Select",
             "proposedSharePrice": "14.00-16.00", "sharesOffered": "8,333,334", "expectedPriceDate": "10/09/2026",
             "dollarValueOfSharesOffered": "$153,333,344"},
            {"proposedTickerSymbol": "LATER", "companyName": "No Date Yet Corp", "expectedPriceDate": ""},
        ]
        deals = self.fetch({"upcoming": {"upcomingTable": {"rows": rows}}})
        self.assertEqual(deals, [{
            "date": date(2026, 10, 9), "symbol": "TRXB", "name": "TRex Bio, Inc.",
            "exchange": "NASDAQ Global Select", "price": "14.00-16.00",
            "shares": 8333334.0, "deal_size": 153333344.0,
        }])

    def test_a_month_without_upcoming_deals(self):
        for data in ({}, {"upcoming": None}, {"upcoming": {"upcomingTable": {"rows": None}}}):
            self.assertEqual(self.fetch(data), [])


if __name__ == "__main__":
    unittest.main()
