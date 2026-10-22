"""Tests für Datenlogik, Token-Auswahl und Icon (ohne Netzwerk, ohne Tray)."""

import email.message
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_limits_tray as clt  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(clt.USAGE_URL, code, "x", msg, None)


class ParseUsageTest(unittest.TestCase):
    def test_utilization_is_percent_even_when_small(self):
        limits = clt.parse_usage(
            {"five_hour": {"utilization": 1.0, "resets_at": None},
             "seven_day": {"utilization": 0.0, "resets_at": None}}
        )
        self.assertEqual([(l.key, l.percent) for l in limits], [("five_hour", 1.0), ("seven_day", 0.0)])

    def test_order_nulls_and_over_100(self):
        limits = clt.parse_usage(
            {"seven_day": {"utilization": 31}, "five_hour": {"utilization": 112.5},
             "seven_day_opus": None, "seven_day_sonnet": {"utilization": None}}
        )
        self.assertEqual([(l.key, l.percent) for l in limits], [("five_hour", 112.5), ("seven_day", 31.0)])

    def test_nested_usage_key(self):
        limits = clt.parse_usage({"usage": {"five_hour": {"utilization": 5}}})
        self.assertEqual(limits[0].percent, 5.0)

    def test_no_limits_raises(self):
        for payload in ({}, {"five_hour": None}, [], "x"):
            with self.assertRaises(RuntimeError):
                clt.parse_usage(payload)

    def test_reset_parsing(self):
        aware = clt._parse_reset("2026-10-23T00:39:59.760388+00:00")
        self.assertEqual(aware.tzinfo, timezone.utc)
        naive = clt._parse_reset("2026-10-23T00:39:59")
        self.assertIsNotNone(naive.tzinfo)  # sonst TypeError beim Countdown
        clt._countdown(naive)
        self.assertEqual(clt._parse_reset(1792711808000), clt._parse_reset(1792711808))
        self.assertIsNone(clt._parse_reset("kaputt"))


class LoadTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".credentials.json"
        patcher = mock.patch.object(clt, "CREDENTIALS", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    def write(self, data):
        self.path.write_text(json.dumps(data) if not isinstance(data, str) else data)

    def test_claude_login(self):
        self.write({"claudeAiOauth": {"accessToken": "ok", "expiresAt": (time.time() + 3600) * 1000}})
        self.assertEqual(clt.load_token(), "ok")

    def test_env_wins(self):
        self.write({"claudeAiOauth": {"accessToken": "file"}})
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "env"
        self.assertEqual(clt.load_token(), "env")

    def test_never_uses_other_tokens(self):
        self.write({"mcpOAuth": {"notion|abc": {"accessToken": "NOTION-SECRET"}},
                    "x": {"token_type": "Bearer"}})
        with self.assertRaises(clt.AuthError) as ctx:
            clt.load_token()
        self.assertNotIn("NOTION-SECRET", str(ctx.exception))

    def test_expired(self):
        self.write({"claudeAiOauth": {"accessToken": "old", "expiresAt": (time.time() - 60) * 1000}})
        with self.assertRaises(clt.AuthError):
            clt.load_token()

    def test_missing_and_corrupt(self):
        with self.assertRaises(clt.AuthError):
            clt.load_token()
        self.write("{halb")
        with self.assertRaises(RuntimeError) as ctx:
            clt.load_token()
        self.assertNotIsInstance(ctx.exception, clt.AuthError)  # nur vorübergehend


class FetchUsageTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(clt, "load_token", return_value="t")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch_with(self, side_effect):
        with mock.patch.object(clt.urllib.request, "urlopen", side_effect=side_effect):
            return clt.fetch_usage()

    def test_success(self):
        body = json.dumps({"five_hour": {"utilization": 18.0}}).encode()
        limits = self.fetch_with(lambda *a, **k: _Resp(body))
        self.assertEqual(limits[0].percent, 18.0)

    def test_error_mapping(self):
        cases = [
            (_http_error(401), clt.AuthError, clt.AUTH_RETRY_SECONDS),
            (_http_error(403), clt.AuthError, clt.AUTH_RETRY_SECONDS),
            (_http_error(429, {"Retry-After": "600"}), clt.RateLimitError, 600),
            (_http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), clt.RateLimitError,
             clt.ERROR_RETRY_SECONDS),
            (_http_error(429, {"Retry-After": "0"}), clt.RateLimitError, clt.ERROR_RETRY_SECONDS),
            (_http_error(500), RuntimeError, clt.ERROR_RETRY_SECONDS),
            (urllib.error.URLError("offline"), clt.NetworkError, clt.NETWORK_RETRY_SECONDS),
            (TimeoutError(), clt.NetworkError, clt.NETWORK_RETRY_SECONDS),
        ]
        for error, exc_type, delay in cases:
            with self.subTest(error=error):
                with self.assertRaises(exc_type) as ctx:
                    self.fetch_with(error)
                self.assertEqual(clt.retry_delay(ctx.exception), delay)

    def test_rate_limit_backoff_grows(self):
        exc = clt.RateLimitError("429", retry_after=0)
        delays = [clt.retry_delay(exc, n) for n in range(1, 7)]
        self.assertEqual(delays, [180, 360, 720, 1440, 1800, 1800])
        self.assertEqual(clt.retry_delay(clt.RateLimitError("429", retry_after=900), 1), 900)

    def test_invalid_json(self):
        with self.assertRaises(RuntimeError):
            self.fetch_with(lambda *a, **k: _Resp(b"<html>"))


class PresentationTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual([clt._level(p) for p in (0, 49.9, 50, 80, 100, 130)],
                         ["ok", "ok", "warn", "hot", "crit", "crit"])

    def test_duration(self):
        self.assertEqual([clt._duration(s) for s in (5, 125, 7300, 90000)], ["5 s", "2 Min", "2 Std", "1 T"])

    def test_icon_states_differ(self):
        def alpha_max(img):
            return img.getchannel("A").getextrema()[1]

        empty = clt.render_icon(None, 32)
        fresh = clt.render_icon(40, 32)
        stale = clt.render_icon(40, 32, stale=True)
        self.assertGreater(alpha_max(empty), 0)
        self.assertNotEqual(empty.tobytes(), clt.render_icon(0, 32).tobytes())
        self.assertGreater(alpha_max(fresh), alpha_max(stale))

    def test_pixmaps_format(self):
        for size, _h, data in clt.icon_pixmaps(75):
            self.assertEqual(len(data), size * size * 4)

    def test_menu_props(self):
        self.assertEqual(clt.menu_item_props(3)["type"], "separator")
        self.assertEqual(dict(clt.menu_item_props(1, ["label"])), {"label": "Fenster öffnen"})


if __name__ == "__main__":
    unittest.main()
