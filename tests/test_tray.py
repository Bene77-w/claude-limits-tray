"""Tests für Symbol, Tooltip und Menü (ohne laufendes Tray)."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_limits_tray as clt  # noqa: E402
import providers as p  # noqa: E402


class PresentationTest(unittest.TestCase):
    def test_levels_and_duration(self):
        self.assertEqual([clt._level(x) for x in (0, 49.9, 50, 80, 100, 130)],
                         ["ok", "ok", "warn", "hot", "crit", "crit"])
        self.assertEqual([clt._duration(s) for s in (5, 125, 7300, 90000)], ["5 s", "2 Min", "2 Std", "1 T"])

    def test_icon_states_differ(self):
        def alpha_max(img):
            return img.getchannel("A").getextrema()[1]

        empty = clt.render_icon(None, 32)
        self.assertGreater(alpha_max(empty), 0)
        self.assertNotEqual(empty.tobytes(), clt.render_icon(0, 32).tobytes())
        self.assertGreater(alpha_max(clt.render_icon(40, 32)), alpha_max(clt.render_icon(40, 32, stale=True)))
        self.assertNotEqual(clt.render_icon(40, 22, letter="A").tobytes(),
                            clt.render_icon(40, 22, letter="B").tobytes())

    def test_pixmaps_format(self):
        for size, _h, data in clt.icon_pixmaps(75, letter="C"):
            self.assertEqual(len(data), size * size * 4)

    def test_account_summary(self):
        account = p.Account("codex:.codex-b", "Codex B", "B")
        self.assertEqual(clt.account_summary(account), "Codex B: lädt …")
        account.plan = "plus"
        account.limits = [p.Limit("primary", "5-Stunden-Limit", 72, None), p.Limit("secondary", "Wochen-Limit", 40, None)]
        self.assertEqual(clt.account_summary(account), "Codex B · Plus: 5 h 72 % · Woche 40 %")
        soon = datetime.now(timezone.utc) + timedelta(hours=2, minutes=5, seconds=30)
        account.limits.append(p.Limit("x", "Woche · Sonnet", 3, soon))
        self.assertTrue(clt.account_summary(account).endswith("Sonnet 3 % (↻ 2 Std 5 Min)"))
        account.error = "HTTP 500"
        self.assertTrue(clt.account_summary(account).endswith("(veraltet)"))

    def test_menu_props(self):
        self.assertEqual(clt.menu_item_props(3)["type"], "separator")
        self.assertEqual(dict(clt.menu_item_props(1, ["label"])), {"label": "Fenster öffnen"})


if __name__ == "__main__":
    unittest.main()
