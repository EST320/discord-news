import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from discord_news import truth_social as ts


def raw(post_id, content="hello", age_seconds=60, **extra):
    created = datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc)
    return {"id": post_id, "content": content, "created_at": created.isoformat(), **extra}


def empty_state():
    return {"seen": {}, "hashes": {}}


class ParsingTest(unittest.TestCase):
    def test_parse_timestamp_variants(self):
        ts_value, iso = ts.parse_timestamp("2026-10-05T16:32:00Z")
        self.assertEqual(iso, "2026-10-05T16:32:00+00:00")
        self.assertEqual(ts.parse_timestamp(ts_value)[1], iso)
        # Naive timestamps are taken as UTC.
        self.assertEqual(ts.parse_timestamp("2026-10-05T16:32:00")[0], ts_value)
        self.assertEqual(ts.parse_timestamp("not a date"), (None, None))
        self.assertEqual(ts.parse_timestamp(None), (None, None))

    def test_clean_html_content(self):
        cleaned = ts.clean_html_content("<p>Tom &amp; Jerry<br/>second   line</p><p>next</p>")
        self.assertEqual(cleaned, "Tom & Jerry\nsecond line\nnext")

    def test_media_type_detection(self):
        self.assertEqual(ts.get_first_media({"media": ["https://x/a.JPG?s=1"]})["type"], "image")
        video = ts.get_first_media({"media": [{"url": "https://x/a.mp4", "preview_url": "https://x/p.jpg"}]})
        self.assertEqual((video["type"], video["preview_url"]), ("video", "https://x/p.jpg"))
        self.assertIsNone(ts.get_first_media({"media": []}))

    def test_content_hash_ignores_case_and_whitespace(self):
        self.assertEqual(
            ts.make_content_hash("Hello   World\n", None),
            ts.make_content_hash("hello world", None),
        )
        self.assertNotEqual(
            ts.make_content_hash("hello", None),
            ts.make_content_hash("hello", {"url": "https://x/a.jpg", "preview_url": None}),
        )

    def test_item_without_id_or_content_is_dropped(self):
        self.assertIsNone(ts.item_to_post({"content": "x"}))
        self.assertIsNone(ts.item_to_post({"id": 1, "content": "<p> </p>"}))

    def test_post_url_falls_back_to_profile_link(self):
        post = ts.item_to_post(raw(42))
        self.assertEqual(post["url"], "https://truthsocial.com/@realDonaldTrump/42")


class CollectNewPostsTest(unittest.TestCase):
    def collect(self, raw_posts, state=None):
        self.log = io.StringIO()
        with redirect_stdout(self.log):
            return ts.collect_new_posts(raw_posts, state or empty_state())

    def ids(self, *args, **kwargs):
        return [p["id"] for p in self.collect(*args, **kwargs)]

    def test_returns_recent_posts_oldest_first(self):
        self.assertEqual(self.ids([raw(2, "b", 10), raw(1, "a", 20)]), ["1", "2"])

    def test_skips_seen_id(self):
        state = {"seen": {"1": time.time()}, "hashes": {}}
        self.assertEqual(self.ids([raw(1, "a"), raw(2, "b")], state), ["2"])

    def test_skips_repost_of_same_content_under_new_id(self):
        first = ts.item_to_post(raw(1, "same text"))
        state = {"seen": {}, "hashes": {first["content_hash"]: time.time()}}
        self.assertEqual(self.ids([raw(2, "Same   TEXT"), raw(3, "other")], state), ["3"])
        self.assertIn("duplicate content: 2", self.log.getvalue())

    def test_old_posts_are_dropped_silently(self):
        first = ts.item_to_post(raw(1, "same text"))
        state = {"seen": {}, "hashes": {first["content_hash"]: time.time()}}
        old = raw(2, "same text", ts.MAX_POST_AGE_SECONDS + 60)
        self.assertEqual(self.ids([old], state), [])
        self.assertEqual(self.log.getvalue(), "")

    def test_skips_unparseable_and_future_timestamps(self):
        untimed = {"id": 1, "content": "a", "created_at": "garbage"}
        future = raw(2, "b", -3600)
        self.assertEqual(self.ids([untimed, future, raw(3, "c")]), ["3"])


class StateTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ts, "STATE_FILE", Path(tempfile.mkdtemp()) / "seen_trump.json")
        self.state_file = patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(ts.load_state(), empty_state())
        self.state_file.write_text("{broken", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ts.load_state(), empty_state())

    def test_wrong_shapes_are_reset(self):
        self.state_file.write_text('{"seen": [], "hashes": "x"}', encoding="utf-8")
        self.assertEqual(ts.load_state(), empty_state())

    def test_save_prunes_expired_entries(self):
        now = time.time()
        expired = now - ts.RETENTION_SECONDS - 10
        ts.save_state({"seen": {"old": expired, "new": now}, "hashes": {"h_old": expired, "h_new": now}})
        state = ts.load_state()
        self.assertEqual((list(state["seen"]), list(state["hashes"])), (["new"], ["h_new"]))


class DescriptionTest(unittest.TestCase):
    def test_falls_back_to_original_when_translation_fails(self):
        with mock.patch.object(ts, "translate_text", return_value=None):
            self.assertEqual(ts.build_description({"content": "original", "media": None}), "original")

    def test_long_translation_is_split_across_embeds(self):
        post = {"url": "https://x", "timestamp": None}
        embeds = ts.build_embeds(post, "x" * (ts.MAX_TRANSLATED_LEN + 5), "card.png")
        self.assertEqual([len(e["description"]) for e in embeds], [ts.MAX_TRANSLATED_LEN, 5])
        self.assertIn("image", embeds[0])
        self.assertNotIn("image", embeds[1])


if __name__ == "__main__":
    unittest.main()
