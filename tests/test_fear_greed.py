import unittest
from datetime import datetime, timedelta, timezone

from market_pulse import fear_greed as fg


class RatingTest(unittest.TestCase):
    def test_band_boundaries(self):
        cases = [
            (0, "Extreme Fear"), (24.9, "Extreme Fear"),
            (25, "Fear"), (44.9, "Fear"),
            (45, "Neutral"), (54.9, "Neutral"),
            (55, "Greed"), (74.9, "Greed"),
            (75, "Extreme Greed"), (100, "Extreme Greed"),
        ]
        for value, label in cases:
            self.assertEqual(fg.rating_label(value), label, value)

    def test_every_band_has_its_own_color(self):
        colors = {fg.rating_color(v) for v in (10, 30, 50, 60, 90)}
        self.assertEqual(len(colors), 5)


class CommentaryTest(unittest.TestCase):
    def test_trend_suffix(self):
        self.assertEqual(fg.build_cnn_commentary(43.2, None), "Fear (43.2).")
        self.assertEqual(fg.build_cnn_commentary(43.2, 43.8), "Fear (43.2) (-0.6).")
        self.assertEqual(fg.build_crypto_commentary(70.0, 65.0), "Greed (70.0) (+5.0).")
        self.assertEqual(fg.build_crypto_commentary(70.0, 70.3), "Greed (70.0) (flat).")


class HistoryTest(unittest.TestCase):
    def test_cnn_history_picks_nearest_point(self):
        now = datetime.now(timezone.utc)

        def point(days_ago, value):
            return {"x": (now - timedelta(days=days_ago)).timestamp() * 1000, "y": value}

        data = {
            "fear_and_greed": {"score": 43.2},
            "fear_and_greed_historical": {"data": [point(30, 45), point(7, 29), point(1, 43), {"x": None, "y": 1}]},
        }
        self.assertEqual(
            fg.build_cnn_history(data),
            [("Now", 43.2), ("Yesterday", 43.0), ("Last week", 29.0), ("Last month", 45.0)],
        )

    def test_cnn_history_without_series_repeats_current_value(self):
        history = fg.build_cnn_history({"fear_and_greed": {"score": 50}})
        self.assertEqual([value for _, value in history], [50.0] * 4)

    def test_crypto_history_falls_back_to_oldest_entry(self):
        entries = [{"value": str(v)} for v in (70, 65, 60)]
        self.assertEqual(
            fg.build_crypto_history(entries),
            [("Now", 70.0), ("Yesterday", 65.0), ("Last week", 60.0), ("Last month", 60.0)],
        )


if __name__ == "__main__":
    unittest.main()
