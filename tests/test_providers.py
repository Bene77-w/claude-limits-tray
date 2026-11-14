"""Tests für Datenquellen, Aktivitätserkennung und Auswahl (ohne Netzwerk, ohne GTK)."""

import base64
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
import providers as p  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x", code, "x", msg, None)


def _jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


CODEX_PLUS = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 42, "limit_window_seconds": 18000, "reset_at": 1792727004},
        "secondary_window": {"used_percent": 7, "limit_window_seconds": 604800, "reset_after_seconds": 600},
    },
}
CODEX_FREE = {
    "plan_type": "free",
    "rate_limit": {
        "primary_window": {"used_percent": 1, "limit_window_seconds": 2592000, "reset_at": 1795033425},
        "secondary_window": None,
    },
}


def _rollout_line(ts, used, limit_id="codex"):
    return json.dumps({
        "timestamp": ts, "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {
            "limit_id": limit_id, "plan_type": "plus",
            "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1792458598},
            "secondary": {"used_percent": 41.0, "window_minutes": 10080, "resets_at": 1792874734},
        }},
    })


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)


# --------------------------------------------------------------------- Claude


class ClaudeParseTest(unittest.TestCase):
    def test_utilization_is_percent_even_when_small(self):
        limits = p.parse_claude_usage(
            {"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 0.0}}
        )
        self.assertEqual([(l.key, l.percent) for l in limits], [("five_hour", 1.0), ("seven_day", 0.0)])

    def test_order_nulls_and_over_100(self):
        limits = p.parse_claude_usage(
            {"seven_day": {"utilization": 31}, "five_hour": {"utilization": 112.5},
             "seven_day_opus": None, "seven_day_sonnet": {"utilization": None}}
        )
        self.assertEqual([(l.key, l.percent) for l in limits], [("five_hour", 112.5), ("seven_day", 31.0)])

    def test_nested_and_empty(self):
        self.assertEqual(p.parse_claude_usage({"usage": {"five_hour": {"utilization": 5}}})[0].percent, 5.0)
        for payload in ({}, {"five_hour": None}, [], "x"):
            with self.assertRaises(RuntimeError):
                p.parse_claude_usage(payload)

    def test_reset_parsing(self):
        self.assertEqual(p.parse_reset("2026-10-23T00:39:59+00:00").tzinfo, timezone.utc)
        self.assertIsNotNone(p.parse_reset("2026-10-23T00:39:59").tzinfo)
        self.assertEqual(p.parse_reset(1792711808000), p.parse_reset(1792711808))
        self.assertIsNone(p.parse_reset("kaputt"))


class ClaudeAuthTest(TempDirTest):
    def write(self, data):
        path = self.dir / "cred.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_claude_login_and_plan(self):
        path = self.write({"claudeAiOauth": {"accessToken": "ok", "subscriptionType": "pro",
                                              "expiresAt": (time.time() + 3600) * 1000}})
        self.assertEqual(p.load_claude_auth(path), ("ok", "pro"))

    def test_env_wins(self):
        path = self.write({"claudeAiOauth": {"accessToken": "file"}})
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "env"
        self.assertEqual(p.load_claude_auth(path)[0], "env")

    def test_never_uses_other_tokens(self):
        path = self.write({"mcpOAuth": {"notion|abc": {"accessToken": "NOTION-SECRET"}},
                           "x": {"token_type": "Bearer"}})
        with self.assertRaises(p.AuthError) as ctx:
            p.load_claude_auth(path)
        self.assertNotIn("NOTION-SECRET", str(ctx.exception))

    def test_expired_missing_corrupt(self):
        path = self.write({"claudeAiOauth": {"accessToken": "old", "expiresAt": (time.time() - 60) * 1000}})
        with self.assertRaises(p.AuthError):
            p.load_claude_auth(path)
        with self.assertRaises(p.AuthError):
            p.load_claude_auth(self.dir / "fehlt.json")
        with self.assertRaises(RuntimeError) as ctx:
            p.load_claude_auth(self.write("{halb"))
        self.assertNotIsInstance(ctx.exception, p.AuthError)  # nur vorübergehend


class ClaudeFetchTest(TempDirTest):
    def setUp(self):
        super().setUp()
        cred = self.dir / "cred.json"
        cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
        self.account = p.ClaudeAccount(projects=self.dir, credentials=cred)

    def fetch_with(self, side_effect):
        with mock.patch.object(p, "_urlopen", side_effect=side_effect):
            return self.account.fetch()

    def test_success(self):
        body = json.dumps({"five_hour": {"utilization": 18.0}}).encode()
        limits, _plan = self.fetch_with(lambda *a, **k: _Resp(body))
        self.assertEqual(limits[0].percent, 18.0)

    def test_error_mapping(self):
        cases = [
            (_http_error(401), p.AuthError, p.AUTH_RETRY_SECONDS),
            (_http_error(403), p.AuthError, p.AUTH_RETRY_SECONDS),
            (_http_error(429, {"Retry-After": "600"}), p.RateLimitError, 600),
            (_http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), p.RateLimitError,
             p.ERROR_RETRY_SECONDS),
            (_http_error(429, {"Retry-After": "0"}), p.RateLimitError, p.ERROR_RETRY_SECONDS),
            (_http_error(500), RuntimeError, p.ERROR_RETRY_SECONDS),
            (urllib.error.URLError("offline"), p.NetworkError, p.NETWORK_RETRY_SECONDS),
            (TimeoutError(), p.NetworkError, p.NETWORK_RETRY_SECONDS),
            (p.http.client.IncompleteRead(b""), p.NetworkError, p.NETWORK_RETRY_SECONDS),
            (_http_error(302), RuntimeError, p.ERROR_RETRY_SECONDS),
        ]
        for error, exc_type, delay in cases:
            with self.subTest(error=error):
                with self.assertRaises(exc_type) as ctx:
                    self.fetch_with(error)
                self.assertEqual(p.retry_delay(ctx.exception), delay)

    def test_invalid_json(self):
        with self.assertRaises(RuntimeError):
            self.fetch_with(lambda *a, **k: _Resp(b"<html>"))

    def test_redirects_are_not_followed(self):
        handler = p._NoRedirect()
        req = p.urllib.request.Request("https://chatgpt.com/x", headers={"Authorization": "Bearer t"})
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/"))
        self.assertTrue(any(isinstance(h, p._NoRedirect) for h in p._OPENER.handlers))


class ClaudeActivityTest(TempDirTest):
    def session(self, name, model, age):
        path = self.dir / "proj" / f"{name}.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"message": {"model": model}}) + "\n"
                        + json.dumps({"message": {"model": "<synthetic>"}}) + "\n")
        os.utime(path, (time.time() - age, time.time() - age))
        return path.stat().st_mtime

    def test_ignores_non_claude_models(self):
        claude_mtime = self.session("a", "claude-opus-5-5", 300)
        self.session("b", "deepseek-flash", 10)  # claude-dsh im selben Ordner
        account = p.ClaudeAccount(projects=self.dir)
        self.assertEqual(account.scan_activity(), claude_mtime)

    def test_many_newer_dsh_sessions(self):
        claude_mtime = self.session("claude", "claude-opus-5-5", 600)
        for i in range(10):
            self.session(f"dsh{i}", "deepseek-flash", 10 + i)
        self.assertEqual(p.ClaudeAccount(projects=self.dir).scan_activity(), claude_mtime)

    def test_model_before_large_tool_output(self):
        path = self.dir / "proj" / "big.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n"
                        + json.dumps({"toolUseResult": "x" * (600 * 1024)}) + "\n")
        self.assertEqual(p.ClaudeAccount(projects=self.dir).scan_activity(), path.stat().st_mtime)

    def test_none_without_claude(self):
        self.session("b", "deepseek-flash", 10)
        self.assertIsNone(p.ClaudeAccount(projects=self.dir).scan_activity())


# ---------------------------------------------------------------------- Codex


class CodexParseTest(unittest.TestCase):
    def test_plus(self):
        limits, plan = p.parse_codex_usage(CODEX_PLUS, now=1000.0)
        self.assertEqual(plan, "plus")
        self.assertEqual([(l.label, l.percent) for l in limits], [("5-Stunden-Limit", 42.0), ("Wochen-Limit", 7.0)])
        self.assertEqual(limits[1].resets_at.timestamp(), 1600.0)  # aus reset_after_seconds

    def test_free_30_days(self):
        limits, plan = p.parse_codex_usage(CODEX_FREE)
        self.assertEqual((plan, [l.label for l in limits]), ("free", ["30-Tage-Limit"]))

    def test_no_limits(self):
        for payload in ({}, {"rate_limit": {"primary_window": None}}, None):
            with self.assertRaises(RuntimeError):
                p.parse_codex_usage(payload)

    def test_rollout_line(self):
        ts, limits, plan = p.parse_codex_rollout(_rollout_line("2026-10-19T21:08:21.789Z", 42.0).encode())
        self.assertEqual((plan, [l.percent for l in limits]), ("plus", [42.0, 41.0]))
        self.assertEqual(limits[0].label, "5-Stunden-Limit")
        self.assertGreater(ts, 0)
        self.assertIsNone(p.parse_codex_rollout(_rollout_line("2026-10-19T21:08:21Z", 1, "code_review").encode()))
        self.assertIsNone(p.parse_codex_rollout(b"{kaputt"))

    def test_labels(self):
        self.assertEqual([p.window_label(s) for s in (18000, 604800, 2592000, None)],
                         ["5-Stunden-Limit", "Wochen-Limit", "30-Tage-Limit", "Limit"])
        self.assertEqual([p.short_label(x) for x in ("5-Stunden-Limit", "Wochen-Limit", "30-Tage-Limit",
                                                       "Woche · Sonnet")], ["5 h", "Woche", "30 T", "Sonnet"])


class CodexAccountTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.home = self.dir / ".codex-b"
        self.home.mkdir()
        self.account = p.CodexAccount(self.home, "B")

    def auth(self, exp_offset=3600, **tokens):
        tokens = {"access_token": _jwt(time.time() + exp_offset), "account_id": "acc-1", **tokens}
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))

    def test_fetch_sends_token_only_to_chatgpt(self):
        self.auth()
        seen = {}

        def urlopen(req, timeout=0):  # noqa: ARG001
            seen["url"] = req.full_url
            seen["account"] = req.get_header("Chatgpt-account-id")
            return _Resp(json.dumps(CODEX_PLUS).encode())

        with mock.patch.object(p, "_urlopen", side_effect=urlopen):
            limits, plan = self.account.fetch()
        self.assertEqual(seen, {"url": "https://chatgpt.com/backend-api/wham/usage", "account": "acc-1"})
        self.assertEqual((plan, len(limits)), ("plus", 2))

    def test_expired_and_api_key_mode(self):
        self.auth(exp_offset=-60)
        with self.assertRaises(p.AuthError) as ctx:
            self.account.fetch()
        self.assertIn("codex-b", str(ctx.exception))
        (self.home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk", "tokens": None}))
        with self.assertRaises(p.AuthError):
            self.account.fetch()

    def test_activity_and_live_limits(self):
        day = self.home / "sessions" / "2026" / "10" / "23"
        day.mkdir(parents=True)
        old = day / "rollout-old.jsonl"
        old.write_text(_rollout_line("2026-10-23T08:00:00Z", 10.0) + "\n")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        new = day / "rollout-new.jsonl"
        new.write_text(_rollout_line("2026-10-23T09:00:00Z", 20.0) + "\n"
                       + _rollout_line("2026-10-23T09:05:00Z", 25.0) + "\n"
                       + json.dumps({"type": "response_item"}) + "\n")
        self.assertEqual(self.account.scan_activity(), new.stat().st_mtime)
        ts, limits, plan = self.account.live_limits()
        self.assertEqual(limits[0].percent, 25.0)
        self.assertIsNone(self.account.live_limits())  # unverändert -> nichts Neues

    def test_quick_scan_between_full_scans(self):
        """Zwischen vollständigen Scans: neueste zwei Tagesordner + zuletzt gefundene Datei."""
        sessions = self.home / "sessions"
        t0 = time.time() - 7200

        def rollout(day, name, mtime):
            path = sessions / day / f"rollout-{name}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
            os.utime(path, (mtime, mtime))
            return path

        resumed = rollout("2026/09/01", "resumed", t0)
        older = rollout("2026/08/01", "older", t0 - 60)
        rollout("2026/10/22", "yesterday", t0 - 120)
        now = time.time()
        self.assertEqual(self.account.scan_activity(now), t0)  # vollständig: findet die alte Sitzung

        os.utime(resumed, (t0 + 600, t0 + 600))  # zuletzt gefundene Datei läuft weiter
        self.assertEqual(self.account.scan_activity(now + 10), t0 + 600)
        today = rollout("2026/10/23", "today", t0 + 900)  # neue Sitzung im neuesten Tagesordner
        self.assertEqual(self.account.scan_activity(now + 20), today.stat().st_mtime)

        self.account.live_limits()  # Stand der neuesten Sitzung gelesen

        # Alte Sitzung fortgesetzt: Aktivität und Live-Werte erst beim vollständigen Scan
        # (bewusst in Kauf genommen, siehe entscheidungen.md)
        older.write_text(_rollout_line(datetime.fromtimestamp(t0 + 1200, timezone.utc).isoformat(), 50.0) + "\n")
        os.utime(older, (t0 + 1200, t0 + 1200))
        self.assertEqual(self.account.scan_activity(now + 30), t0 + 900)
        self.assertIsNone(self.account.live_limits())
        self.assertEqual(self.account.scan_activity(now + p.FULL_SCAN_SECONDS), t0 + 1200)
        self.assertEqual(self.account.live_limits()[1][0].percent, 50.0)


# ------------------------------------------------------------ Konten/Auswahl


def _account(name, percent=None, activity=None):
    account = p.Account(name, name, name[0])
    if percent is not None:
        account.limits = [p.Limit("k", "Limit", percent, None)]
    account.activity = activity
    return account


class DisplayLimitTest(unittest.TestCase):
    def test_five_hour_preferred_else_next(self):
        claude = p.Account("claude", "Claude", "A")
        claude.limits = [p.Limit("seven_day", "Wochen-Limit", 92, None), p.Limit("five_hour", "5-Stunden-Limit", 20, None)]
        self.assertEqual((claude.display_percent(), claude.max_percent()), (20, 92))
        codex = p.Account("codex:.codex-b", "Codex B", "C")
        codex.limits = [p.Limit("primary", "5-Stunden-Limit", 3, None), p.Limit("secondary", "Wochen-Limit", 50, None)]
        self.assertEqual(codex.display_percent(), 3)
        free = p.Account("codex:.codex", "Codex A", "C")
        free.limits = [p.Limit("primary", "30-Tage-Limit", 1, None)]
        self.assertEqual(free.display_percent(), 1)
        self.assertIsNone(p.Account("x", "x", "x").display_percent())


class ChooseAccountTest(unittest.TestCase):
    def test_active_highest_usage_wins(self):
        now = 10_000.0
        claude = _account("claude", 30, now - 60)
        codex_a = _account("a", 80, now - 7200)  # nicht aktiv
        codex_b = _account("b", 55, now - 30)
        self.assertIs(p.choose_account([claude, codex_a, codex_b], now), codex_b)

    def test_compares_ring_limit_not_week(self):
        now = 10_000.0
        claude = _account("claude", None, now - 10)
        claude.limits = [p.Limit("five_hour", "5-Stunden-Limit", 10, None), p.Limit("seven_day", "Wochen-Limit", 95, None)]
        codex = _account("b", None, now - 10)
        codex.limits = [p.Limit("primary", "5-Stunden-Limit", 40, None), p.Limit("secondary", "Wochen-Limit", 5, None)]
        self.assertIs(p.choose_account([claude, codex], now), codex)

    def test_single_active(self):
        now = 10_000.0
        claude = _account("claude", 30, now - 60)
        codex_b = _account("b", 90, now - 3600)
        self.assertIs(p.choose_account([claude, codex_b], now), claude)

    def test_fallback_last_used_then_first(self):
        now = 10_000.0
        a = _account("a", 10, now - 5000)
        b = _account("b", 20, now - 4000)
        self.assertIs(p.choose_account([a, b], now), b)
        self.assertIs(p.choose_account([_account("x", 5), _account("y", 9)], now).id, "x")
        self.assertIsNone(p.choose_account([_account("x")], now))

    def test_active_without_data_ignored(self):
        now = 10_000.0
        self.assertEqual(p.choose_account([_account("a", None, now), _account("b", 3, now - 9000)], now).id, "b")


class AccountStateTest(unittest.TestCase):
    def test_rate_limit_backoff_grows_and_blocks_manual(self):
        exc = p.RateLimitError("429", retry_after=0)
        self.assertEqual([p.retry_delay(exc, n) for n in range(1, 7)], [180, 360, 720, 1440, 1800, 1800])
        self.assertEqual(p.retry_delay(p.RateLimitError("429", retry_after=900), 1), 900)
        account = _account("a")
        account.apply_error(exc, 0.0)
        account.apply_error(exc, 0.0)
        self.assertEqual(account.retry_after, 360)
        self.assertFalse(account.manual_refresh_allowed(100))
        self.assertFalse(account.refresh_due(100, 60))
        self.assertTrue(account.refresh_due(361, 60))
        account.apply_success([], 400.0)
        self.assertEqual(account.rate_limit_streak, 0)

    def test_refresh_due_interval(self):
        account = _account("a", 5)
        account.last_update = 1000.0
        self.assertFalse(account.refresh_due(1059, 60))
        self.assertTrue(account.refresh_due(1060, 60))


class ApplyLiveTest(unittest.TestCase):
    def test_only_fresh_and_newer(self):
        now = 100_000.0
        account = _account("b")
        limits = [p.Limit("primary", "5-Stunden-Limit", 30, None)]
        self.assertFalse(account.apply_live((now - 4 * 86400, limits, "plus"), now))  # alt
        self.assertFalse(account.apply_live(None, now))
        self.assertTrue(account.apply_live((now - 20, limits, "plus"), now))
        self.assertEqual((account.last_update, account.plan), (now - 20, "plus"))
        self.assertFalse(account.apply_live((now - 20, limits, "plus"), now))  # schon übernommen
        account.last_update = now - 5  # Abruf war neuer
        self.assertFalse(account.apply_live((now - 10, limits, "plus"), now))


class ApplyLiveKeepsBackoffTest(unittest.TestCase):
    def test_live_values_do_not_lift_rate_limit_pause(self):
        now = 100_000.0
        account = _account("b", 5)
        account.apply_error(p.RateLimitError("429", retry_after=0), now - 30)
        pause_until = account.retry_after
        limits = [p.Limit("primary", "5-Stunden-Limit", 30, None)]
        self.assertTrue(account.apply_live((now - 5, limits, "plus"), now))
        self.assertEqual((account.limits[0].percent, account.error), (30, None))
        self.assertFalse(account.is_stale())
        self.assertEqual((account.rate_limited, account.retry_after), (True, pause_until))
        self.assertFalse(account.manual_refresh_allowed(now))
        self.assertFalse(account.refresh_due(now, 60))
        self.assertTrue(account.refresh_due(pause_until + 1, 60))  # danach Abruf zur Bestätigung
        account.apply_error(p.RateLimitError("429", retry_after=0), pause_until + 2)
        self.assertEqual(account.rate_limit_streak, 2)  # Backoff wächst weiter


class DiscoverAndCacheTest(TempDirTest):
    def test_discover_codex_profiles(self):
        for name, auth in ((".codex", True), (".codex-b", True), (".codex-c", False), (".codex-old.bak", True),
                           (".codex-old", True), (".codex-b2", True), (".codex-a", True)):
            (self.dir / name).mkdir()
            if auth:
                (self.dir / name / "auth.json").write_text("{}")
        found = [(a.name, a.short) for a in p.discover_accounts(self.dir)]
        self.assertEqual(found, [("Claude", "A"), ("Codex A", "C"), ("Codex B", "C")])

    def test_cache_roundtrip_and_legacy(self):
        path = self.dir / "usage.json"
        a = _account("claude", 22)
        a.last_update, a.plan = 123.0, "pro"
        b = _account("codex:.codex-b")
        p.save_cache([a, b], path)
        fresh = [_account("claude"), _account("codex:.codex-b")]
        p.restore_cache(fresh, path)
        self.assertEqual((fresh[0].limits[0].percent, fresh[0].last_update, fresh[0].plan), (22.0, 123.0, "pro"))
        self.assertEqual(fresh[1].limits, [])
        path.write_text(json.dumps({"ts": 5.0, "limits": [{"key": "five_hour", "label": "x", "percent": 9}]}))
        legacy = [_account("claude")]
        p.restore_cache(legacy, path)
        self.assertEqual(legacy[0].limits[0].percent, 9.0)


if __name__ == "__main__":
    unittest.main()
