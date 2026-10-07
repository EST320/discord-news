import unittest
from unittest import mock

import requests

from market_pulse import yahoo


def chart(closes, volumes):
    return {"chart": {"result": [{"indicators": {"quote": [{"close": closes, "volume": volumes}]}}]}}


def response(payload, status=200):
    fake = mock.Mock(status_code=status)
    fake.json.return_value = payload
    fake.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(f"status {status}")
    return fake


class SymbolTest(unittest.TestCase):
    def test_share_classes_use_a_dash(self):
        self.assertEqual(yahoo.yahoo_symbol("BRK/B"), "BRK-B")
        self.assertEqual(yahoo.yahoo_symbol("bf.b "), "BF-B")
        self.assertEqual(yahoo.yahoo_symbol("COIN"), "COIN")


class DollarVolumesTest(unittest.TestCase):
    def test_close_times_volume_skipping_gaps(self):
        payload = chart([10.0, None, 12.0, 11.0], [100, 100, 0, 200])
        self.assertEqual(yahoo.dollar_volumes(payload), [1000.0, 2200.0])

    def test_empty_or_malformed_payloads(self):
        for payload in ({}, {"chart": {"result": None}}, {"chart": {"result": [{}]}}):
            self.assertEqual(yahoo.dollar_volumes(payload), [])


class AverageDollarVolumeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(yahoo.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, *answers, days=20):
        with mock.patch.object(yahoo.SESSION, "get", side_effect=list(answers)) as get:
            return yahoo.fetch_average_dollar_volume("BRK/B", days), get

    def test_averages_only_the_most_recent_sessions(self):
        # One huge session 25 days ago must not leak into a 20-day average.
        closes, volumes = [10.0] * 30, [1_000_000] * 5 + [100] * 25
        value, get = self.fetch(response(chart(closes, volumes)))
        self.assertEqual(value, 1000.0)
        self.assertTrue(get.call_args.args[0].endswith("/BRK-B"))

    def test_a_single_spike_is_diluted(self):
        closes, volumes = [10.0] * 20, [100] * 19 + [10_000]
        value, _ = self.fetch(response(chart(closes, volumes)))
        self.assertAlmostEqual(value, (19 * 1000 + 100_000) / 20)

    def test_unknown_symbol_or_no_data_is_none(self):
        self.assertIsNone(self.fetch(response({}, status=404))[0])
        self.assertIsNone(self.fetch(response(chart([], [])))[0])

    def test_retries_then_gives_up_quietly(self):
        value, get = self.fetch(requests.ConnectionError("x"), requests.ConnectionError("y"))
        self.assertIsNone(value)
        self.assertEqual(get.call_count, yahoo.MAX_RETRIES)

    def test_bulk_lookup_leaves_out_what_yahoo_cannot_serve(self):
        with mock.patch.object(yahoo, "fetch_average_dollar_volume", side_effect=[5.0, None, 7.0]):
            self.assertEqual(yahoo.average_dollar_volumes(["A", "B", "C"]), {"A": 5.0, "C": 7.0})


if __name__ == "__main__":
    unittest.main()
