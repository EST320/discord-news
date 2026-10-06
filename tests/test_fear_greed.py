import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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


def panel(key, heading, value, commentary):
    return {"key": key, "heading": heading, "title": heading.split()[0], "source": "src",
            "value": value, "commentary": commentary, "history": [("Now", value)] * 4}


CNN = panel("cnn_last", "CNN Market Sentiment Tracker", 43.23, "Fear (43.2) (-0.6).")
CRYPTO = panel("crypto_last", "Crypto Market Sentiment Tracker", 70.0, "Greed (70.0) (+5.0).")
MISSING = {"title": "Crypto Market", "source": "alternative.me", "value": None}


class EmbedTest(unittest.TestCase):
    def test_both_indices_share_one_embed_with_their_original_text(self):
        embed = fg.build_embed([CNN, CRYPTO], "fear_greed.png")
        self.assertEqual(embed["image"], {"url": "attachment://fear_greed.png"})
        self.assertEqual(embed["fields"], [
            {"name": "CNN Market Sentiment Tracker",
             "value": "Fear (43.2) (-0.6).\n**Current Value**\n43.23", "inline": True},
            {"name": "Crypto Market Sentiment Tracker",
             "value": "Greed (70.0) (+5.0).\n**Current Value**\n70.00", "inline": True},
        ])
        self.assertEqual(embed["color"], int(fg.rating_color(43.23).lstrip("#"), 16))

    def test_an_index_without_data_is_left_out_of_the_text(self):
        embed = fg.build_embed([CNN, MISSING], "x.png")
        self.assertEqual([field["name"] for field in embed["fields"]], ["CNN Market Sentiment Tracker"])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.state_file = Path(tempfile.mkdtemp()) / "seen_feargreed.json"
        self.post = mock.Mock()
        patches = [
            mock.patch.object(fg, "STATE_FILE", self.state_file),
            mock.patch.object(fg, "post_to_discord", self.post),
            mock.patch.object(fg, "draw_card", return_value=Path("fear_greed.png")),
            mock.patch.object(fg, "DRY_RUN", False),
            mock.patch.dict("os.environ", {fg.WEBHOOK_ENV: "hook"}),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, cnn, crypto):
        def loader(result):
            return mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result)
        with mock.patch.object(fg, "load_cnn", loader(cnn)), mock.patch.object(fg, "load_crypto", loader(crypto)):
            fg.main()

    def test_posts_once_and_records_both_values(self):
        self.run_main(CNN, CRYPTO)
        self.post.assert_called_once()
        self.assertEqual(fg.load_state(), {"cnn_last": 43.23, "crypto_last": 70.0})

    def test_one_failing_index_does_not_block_the_other(self):
        fg.save_state({"cnn_last": 40.0, "crypto_last": 65.0})
        self.run_main(CNN, RuntimeError("alternative.me down"))
        panels = self.post.call_args.args[0]
        self.assertEqual([p["value"] for p in panels], [43.23, None])
        # The failed index keeps its previous value for the next comparison.
        self.assertEqual(fg.load_state(), {"cnn_last": 43.23, "crypto_last": 65.0})

    def test_nothing_is_posted_when_both_fail(self):
        with self.assertRaises(RuntimeError):
            self.run_main(RuntimeError("a"), RuntimeError("b"))
        self.post.assert_not_called()

    def test_dry_run_neither_posts_nor_saves(self):
        with mock.patch.object(fg, "DRY_RUN", True):
            self.run_main(CNN, CRYPTO)
        self.post.assert_not_called()
        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
