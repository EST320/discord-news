"""The on-demand market summary: the reply to the /market slash command."""

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from market_pulse import discord
from market_pulse import market_close as mc


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class NewYorkTimeTest(unittest.TestCase):
    def test_daylight_and_standard_time(self):
        self.assertEqual(mc.new_york_time(utc(2026, 10, 7, 14, 32)), datetime(2026, 10, 7, 10, 32))
        self.assertEqual(mc.new_york_time(utc(2026, 12, 15, 14, 32)), datetime(2026, 12, 15, 9, 32))

    def test_clocks_go_forward_on_the_second_sunday_of_march(self):
        self.assertEqual(mc.new_york_time(utc(2026, 3, 8, 6, 59)), datetime(2026, 3, 8, 1, 59))
        self.assertEqual(mc.new_york_time(utc(2026, 3, 8, 7, 0)), datetime(2026, 3, 8, 3, 0))

    def test_clocks_go_back_on_the_first_sunday_of_november(self):
        self.assertEqual(mc.new_york_time(utc(2026, 11, 1, 5, 59)), datetime(2026, 11, 1, 1, 59))
        self.assertEqual(mc.new_york_time(utc(2026, 11, 1, 6, 0)), datetime(2026, 11, 1, 1, 0))

    def test_other_years(self):
        # 2027: daylight time from March 14 to November 7.
        self.assertEqual(mc.new_york_time(utc(2027, 3, 13, 17, 0)).hour, 12)
        self.assertEqual(mc.new_york_time(utc(2027, 3, 15, 17, 0)).hour, 13)
        self.assertEqual(mc.new_york_time(utc(2027, 11, 8, 17, 0)).hour, 12)


class SessionStatusTest(unittest.TestCase):
    def test_during_the_session(self):
        self.assertEqual(mc.session_status(date(2026, 10, 7), utc(2026, 10, 7, 14, 32)), "盘中 10:32 纽约时间")

    def test_after_the_close(self):
        self.assertEqual(mc.session_status(date(2026, 10, 7), utc(2026, 10, 7, 20, 0)), "已收盘")
        self.assertEqual(mc.session_status(date(2026, 10, 7), utc(2026, 10, 8, 2, 0)), "已收盘")

    def test_before_the_open_on_a_weekday(self):
        self.assertEqual(mc.session_status(date(2026, 10, 6), utc(2026, 10, 7, 12, 0)), "未开盘 · 上一交易日")

    def test_weekend_and_holiday(self):
        self.assertEqual(mc.session_status(date(2026, 10, 9), utc(2026, 10, 10, 15, 0)), "休市 · 上一交易日")
        # A weekday holiday: mid-session hours, but the newest bar is still the previous session's.
        self.assertEqual(mc.session_status(date(2026, 11, 25), utc(2026, 11, 26, 16, 0)), "休市 · 上一交易日")


class EmbedTitleTest(unittest.TestCase):
    def test_on_demand_caption_carries_the_status(self):
        quotes = {"^GSPC": mc.parse_quote({"timestamp": [1, 2], "close": [100, 101]})}
        embed = mc.build_embed(quotes, date(2026, 10, 7), "x.png", title="美股行情 · 盘中 10:32 纽约时间")
        self.assertEqual(embed["description"], "美股行情 · 盘中 10:32 纽约时间 · 2026年10月7日 周三")


class OnDemandMainTest(unittest.TestCase):
    BAR_TS = int(utc(2026, 10, 6, 13, 30).timestamp())

    def run_main(self, env):
        quotes = {symbol: {"price": 101, "previous": 100, "change": 1, "change_pct": 1.0, "bar_ts": self.BAR_TS}
                  for symbol in mc.ALL_SYMBOLS}
        chart = mock.Mock(spec=Path)
        chart.name = "market_close.png"
        chart.read_bytes.return_value = b"png"
        patches = (
            mock.patch.object(mc, "load_quotes", return_value=quotes),
            mock.patch.object(mc, "load_intraday", return_value={}),
            mock.patch.object(mc, "load_ranges", return_value={}),
            mock.patch.object(mc, "draw_card", return_value=chart),
            mock.patch.object(mc, "post_webhook"),
            mock.patch.object(mc, "edit_interaction_response"),
            mock.patch.object(mc, "DRY_RUN", False),
            # clear=True: only the variables this case sets are visible to main().
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("builtins.print"),
        )
        with patches[0], patches[1], patches[2], patches[3] as draw, patches[4] as webhook, \
                patches[5] as interaction, patches[6], patches[7], patches[8]:
            mc.main()
        return draw, webhook, interaction

    def test_replies_to_the_interaction_even_when_the_market_is_closed(self):
        # The newest bar is days old, so the scheduled post would be skipped.
        draw, webhook, interaction = self.run_main({mc.INTERACTION_TOKEN_ENV: "tok", mc.APPLICATION_ID_ENV: "app"})
        webhook.assert_not_called()
        application_id, token, payload = interaction.call_args.args
        self.assertEqual((application_id, token), ("app", "tok"))
        self.assertTrue(payload["embeds"][0]["description"].startswith("美股行情 · "))
        self.assertEqual(interaction.call_args.kwargs["file"], ("market_close.png", b"png"))
        self.assertIsNotNone(draw.call_args.kwargs["status"])

    def test_scheduled_run_still_skips_a_closed_market_and_uses_the_webhook(self):
        draw, webhook, interaction = self.run_main({mc.WEBHOOK_ENV: "hook"})
        draw.assert_not_called()
        webhook.assert_not_called()
        interaction.assert_not_called()


class EditInteractionResponseTest(unittest.TestCase):
    def test_image_reply_is_a_multipart_patch_to_the_original_message(self):
        with mock.patch.object(discord.requests, "patch") as patch:
            discord.edit_interaction_response("app", "tok", {"embeds": [{"description": "x"}]}, file=("m.png", b"png"))
        url = patch.call_args.args[0]
        self.assertEqual(url, "https://discord.com/api/v10/webhooks/app/tok/messages/@original")
        body = json.loads(patch.call_args.kwargs["data"]["payload_json"])
        self.assertEqual(body["attachments"], [{"id": 0, "filename": "m.png"}])
        self.assertEqual(patch.call_args.kwargs["files"]["files[0]"], ("m.png", b"png", "image/png"))

    def test_text_reply_is_plain_json(self):
        with mock.patch.object(discord.requests, "patch") as patch:
            discord.edit_interaction_response("app", "tok", {"content": "hi"})
        self.assertEqual(patch.call_args.kwargs["json"], {"content": "hi"})

    def test_errors_are_raised(self):
        failing = mock.Mock()
        failing.raise_for_status.side_effect = discord.requests.HTTPError("404")
        with mock.patch.object(discord.requests, "patch", return_value=failing):
            with self.assertRaises(discord.requests.HTTPError):
                discord.edit_interaction_response("app", "tok", {"content": "hi"})


if __name__ == "__main__":
    unittest.main()
