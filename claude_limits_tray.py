#!/usr/bin/env python3
"""Claude-Limits-Tray für Cinnamon/GTK.

Linksklick auf das Tray-Icon öffnet ein kleines Fenster mit dem
5-Stunden- und Wochen-Limit des Claude-Abonnements.

Datenquelle: GET https://api.anthropic.com/api/oauth/usage
Auth: OAuth-Access-Token des Claude-Code-Logins aus ~/.claude/.credentials.json
(wird nur im Speicher gehalten, nie ausgegeben).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk, Gdk

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11
except (ImportError, ValueError):  # z. B. unter Wayland
    GdkX11 = None
from PIL import Image, ImageDraw

APP_ID = "claude-limits-tray"
SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_SERVICE = f"{SNI_IFACE}-{os.getpid()}-1"
SNI_PATH = "/StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
MENU_IFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
CONTROL_NAME = "org.bene.ClaudeLimitsTray"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CACHE_FILE = Path.home() / ".cache" / "claude-limits-tray" / "usage.json"

BG_REFRESH_SECONDS = 300    # Hintergrund-Aktualisierung
UI_REFRESH_SECONDS = 60     # Aktualisierung bei offenem Fenster
NETWORK_RETRY_SECONDS = 60  # offline, DNS, Timeout
ERROR_RETRY_SECONDS = 180   # Serverfehler, kaputte Antwort
AUTH_RETRY_SECONDS = 300    # Token fehlt/abgelaufen (Dateiänderung löst früher aus)
RATE_LIMIT_MAX_SECONDS = 1800  # Obergrenze des Backoffs bei wiederholtem 429

EXPIRED_MSG = (
    "Token abgelaufen. Claude Code einmal starten (oder /login ausführen); "
    "die Anzeige aktualisiert sich danach von selbst."
)

# Reihenfolge und Beschriftung der angezeigten Limits
LIMIT_KEYS = [
    ("five_hour", "5-Stunden-Limit"),
    ("seven_day", "Wochen-Limit"),
    ("seven_day_sonnet", "Woche · Sonnet"),
    ("seven_day_opus", "Woche · Opus"),
    ("seven_day_oauth_apps", "Woche · Apps"),
    ("seven_day_cowork", "Woche · Cowork"),
]

# Farbstufen: Ring im Tray und Balken im Fenster nutzen dieselben Werte
LEVEL_RGB = {
    "ok": (86, 182, 110),
    "warn": (240, 190, 60),
    "hot": (240, 133, 45),
    "crit": (229, 72, 77),
}


# ---------------------------------------------------------------- Datenmodell


@dataclass
class Limit:
    key: str
    label: str
    percent: float
    resets_at: datetime | None


class AuthError(RuntimeError):
    """Token fehlt, ist abgelaufen oder wird vom Server abgelehnt."""


class NetworkError(RuntimeError):
    """Server nicht erreichbar (offline, DNS, Timeout)."""


class RateLimitError(RuntimeError):
    """HTTP 429 - mit Wartezeit aus dem Retry-After-Header."""

    def __init__(self, message, retry_after=ERROR_RETRY_SECONDS):
        super().__init__(message)
        self.retry_after = retry_after


def retry_delay(exc: Exception, streak: int = 1) -> int:
    """Wartezeit bis zum nächsten automatischen Versuch nach einem Fehler.

    `streak` zählt aufeinanderfolgende 429-Antworten. Der Server schickt oft
    `Retry-After: 0`, sperrt aber weiter - darum verdoppelt sich die Pause.
    """
    if isinstance(exc, RateLimitError):
        backoff = ERROR_RETRY_SECONDS * 2 ** (max(1, streak) - 1)
        return min(RATE_LIMIT_MAX_SECONDS, max(exc.retry_after, backoff))
    if isinstance(exc, AuthError):
        return AUTH_RETRY_SECONDS
    if isinstance(exc, NetworkError):
        return NETWORK_RETRY_SECONDS
    return ERROR_RETRY_SECONDS


def _epoch_seconds(value: float) -> float:
    # Claude Code speichert Zeitpunkte in Millisekunden
    return value / 1000.0 if value > 1e11 else float(value)


def load_token() -> str:
    """Nur der Claude-Code-Login zählt - andere Tokens (z. B. MCP) nie senden."""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env
    try:
        data = json.loads(CREDENTIALS.read_text())
    except FileNotFoundError:
        raise AuthError(
            "Keine Claude-Anmeldung gefunden (~/.claude/.credentials.json fehlt)."
        ) from None
    except ValueError:
        # Claude Code schreibt die Datei vermutlich gerade neu
        raise RuntimeError("Anmeldedaten gerade nicht lesbar, neuer Versuch folgt.") from None
    except OSError as exc:
        raise AuthError(f"Anmeldedaten nicht lesbar: {type(exc).__name__}") from None

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise AuthError("Kein Claude-Login gefunden. Claude Code starten und /login ausführen.")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and _epoch_seconds(expires) < time.time():
        raise AuthError(EXPIRED_MSG)
    return token


def load_cache():
    """Zuletzt erfolgreich geladene Werte (überlebt Neustarts)."""
    try:
        data = json.loads(CACHE_FILE.read_text())
        limits = [
            Limit(e["key"], e["label"], float(e["percent"]), _parse_reset(e.get("resets_at")))
            for e in data["limits"]
        ]
        return limits, float(data.get("ts") or 0.0)
    except (OSError, ValueError, KeyError, TypeError):
        return [], 0.0


def save_cache(limits):
    payload = {
        "ts": time.time(),
        "limits": [
            {
                "key": l.key,
                "label": l.label,
                "percent": l.percent,
                "resets_at": l.resets_at.isoformat() if l.resets_at else None,
            }
            for l in limits
        ],
    }
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, CACHE_FILE)  # atomar: nie eine halb geschriebene Datei
    except OSError:
        pass


def _parse_reset(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(_epoch_seconds(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Ohne Zeitzone als UTC werten, sonst scheitert der Vergleich mit "jetzt"
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_usage(payload) -> list[Limit]:
    """Antwort des Usage-Endpoints in Limits übersetzen.

    `utilization` ist ein Prozentwert (0..100, über 100 möglich). Keine
    Umrechnung aus 0..1 raten: 1.0 heißt 1 %, nicht 100 %.
    """
    data = payload.get("usage", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Unerwartete Antwort vom Server.")
    limits = []
    for key, label in LIMIT_KEYS:
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            pct = float(entry["utilization"])
        except (KeyError, TypeError, ValueError):
            continue
        limits.append(Limit(key, label, max(0.0, pct), _parse_reset(entry.get("resets_at"))))
    if not limits:
        raise RuntimeError("Antwort enthielt keine Limit-Daten.")
    return limits


def fetch_usage(timeout: float = 15.0) -> list[Limit]:
    token = load_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-limits-tray/1.1",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError(EXPIRED_MSG) from exc
        if exc.code == 403:
            raise AuthError(
                "Zugriff verweigert (HTTP 403): Token ungültig oder ohne Berechtigung "
                "für Nutzungsdaten. In Claude Code neu anmelden (/login)."
            ) from exc
        if exc.code == 429:
            try:
                wait = int(float((exc.headers or {}).get("retry-after") or ERROR_RETRY_SECONDS))
            except (TypeError, ValueError):
                wait = ERROR_RETRY_SECONDS  # z. B. Retry-After als HTTP-Datum
            raise RateLimitError("Zu viele Anfragen (HTTP 429)", max(0, min(wait, RATE_LIMIT_MAX_SECONDS))) from exc
        raise RuntimeError(f"Serverfehler (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"Keine Verbindung: {exc.reason}") from exc
    except OSError as exc:  # Timeout beim Lesen u. Ä.
        raise NetworkError(f"Keine Verbindung: {type(exc).__name__}") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        raise RuntimeError("Antwort des Servers war kein gültiges JSON.") from None
    return parse_usage(payload)


def demo_limits() -> list[Limit]:
    now = datetime.now(timezone.utc)
    return [
        Limit("five_hour", "5-Stunden-Limit", 34, now + timedelta(hours=2, minutes=17)),
        Limit("seven_day", "Wochen-Limit", 61, now + timedelta(days=3, hours=4)),
        Limit("seven_day_sonnet", "Woche · Sonnet", 22, now + timedelta(days=3, hours=4)),
    ]


# ------------------------------------------------------------------- Darstellung


def _level(percent: float) -> str:
    if percent >= 100:
        return "crit"
    if percent >= 80:
        return "hot"
    if percent >= 50:
        return "warn"
    return "ok"


def _duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} Min"
    if s < 86400:
        return f"{s // 3600} Std"
    return f"{s // 86400} T"


def _countdown(dt: datetime | None) -> str:
    if not dt:
        return "unbekannt"
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "jetzt"
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} T {hours} Std"
    if hours:
        return f"{hours} Std {minutes} Min"
    return f"{minutes} Min"


def _local_time(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone().strftime("%d.%m. %H:%M")


def _reset_text(limit: Limit) -> str:
    if not limit.resets_at:
        return "Reset-Zeitpunkt unbekannt"
    return f"Zurückgesetzt in {_countdown(limit.resets_at)} · {_local_time(limit.resets_at)}"


def render_icon(percent: float | None, size: int = 32, stale: bool = False) -> Image.Image:
    """Ringdiagramm: gefüllter Teil = verbrauchtes Limit.

    Ohne Daten erscheint ein gepunkteter Ring, bei veralteten Werten (Fehler)
    ein blasser Ring - beides soll nicht wie ein gültiger Wert aussehen.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 10)
    width = max(3, size // 5)
    box = (pad, pad, size - pad - 1, size - pad - 1)
    if percent is None:
        for start in range(-90, 270, 30):
            draw.arc(box, start, start + 14, fill=(150, 150, 150, 220), width=width)
        return img
    draw.arc(box, 0, 360, fill=(130, 130, 130, 110), width=width)
    sweep = min(percent, 100.0) / 100.0 * 360.0
    if sweep > 0.5:
        alpha = 120 if stale else 255
        draw.arc(box, -90, -90 + sweep, fill=LEVEL_RGB[_level(percent)] + (alpha,), width=width)
    return img


def icon_pixmaps(percent: float | None, stale: bool = False):
    """ARGB32-Pixmaps im SNI-Format (a(iiay))."""
    out = []
    for size in (22, 32, 48, 64):
        r, g, b, a = render_icon(percent, size, stale).split()
        argb = Image.merge("RGBA", (a, r, g, b)).tobytes()  # A,R,G,B
        out.append(dbus.Struct((size, size, dbus.ByteArray(argb)), signature="iiay"))
    return dbus.Array(out, signature="(iiay)")


# ------------------------------------------------------------------ Tray (SNI)


class StatusNotifierItem(dbus.service.Object):
    def __init__(self, bus, app: "LimitsApp"):
        super().__init__(bus, SNI_PATH)
        self.app = app

    # --- Methoden -------------------------------------------------------
    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        self.app.open_window()

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        self.app.force_refresh()

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        pass  # Host öffnet das Menü selbst über die Property "Menu"

    @dbus.service.method(SNI_IFACE, in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    # --- Properties -----------------------------------------------------
    def _props(self) -> dict:
        pixmaps = icon_pixmaps(self.app.icon_percent(), stale=self.app.is_stale())
        title, text = self.app.tooltip_lines()
        tooltip = dbus.Struct(
            ("", pixmaps, dbus.String(title), dbus.String(text)), signature="sa(iiay)ss"
        )
        return {
            "Category": dbus.String("ApplicationStatus"),
            "Id": dbus.String(APP_ID),
            "Title": dbus.String("Claude Limits"),
            "Status": dbus.String("Active"),
            "WindowId": dbus.Int32(0),
            "IconName": dbus.String(""),
            "IconPixmap": pixmaps,
            "OverlayIconName": dbus.String(""),
            "OverlayIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionIconName": dbus.String(""),
            "AttentionIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionMovieName": dbus.String(""),
            "ToolTip": tooltip,
            "ItemIsMenu": dbus.Boolean(False),
            "Menu": dbus.ObjectPath(MENU_PATH),
        }

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        props = self._props()
        if property_name not in props:
            raise dbus.exceptions.DBusException(
                f"Unknown property {property_name}", name="org.freedesktop.DBus.Error.InvalidArgs"
            )
        return props[property_name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        return dbus.Dictionary(self._props(), signature="sv")

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface_name, changed, invalidated):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewToolTip(self):
        pass

    def refresh_icon(self):
        props = self._props()
        self.NewIcon()
        self.NewToolTip()
        self.PropertiesChanged(
            SNI_IFACE,
            dbus.Dictionary(
                {"IconPixmap": props["IconPixmap"], "ToolTip": props["ToolTip"]}, signature="sv"
            ),
            dbus.Array([], signature="s"),
        )


# ------------------------------------------------------------------- dbusmenu

MENU_ITEMS = {
    0: {"children-display": "submenu"},
    1: {"label": "Fenster öffnen"},
    2: {"label": "Aktualisieren"},
    3: {"type": "separator"},
    4: {"label": "Beenden"},
}
MENU_CHILDREN = {0: [1, 2, 3, 4]}
MENU_ACTIONS = {1: "open", 2: "refresh", 4: "quit"}


def menu_item_props(item_id: int, names=()) -> dbus.Dictionary:
    """Properties eines Menüeintrags; `names` filtert (leer = alle)."""
    props = {"enabled": dbus.Boolean(True), "visible": dbus.Boolean(True)}
    props.update({k: dbus.String(v) for k, v in MENU_ITEMS[item_id].items()})
    if names:
        props = {k: v for k, v in props.items() if k in names}
    return dbus.Dictionary(props, signature="sv")


def _invalid_args(message):
    return dbus.exceptions.DBusException(message, name="org.freedesktop.DBus.Error.InvalidArgs")


class Menu(dbus.service.Object):
    PROPS = {
        "Version": dbus.UInt32(3),
        "TextDirection": dbus.String("ltr"),
        "Status": dbus.String("normal"),
        "IconThemePath": dbus.Array([], signature="s"),
    }

    def __init__(self, bus, app: "LimitsApp"):
        super().__init__(bus, MENU_PATH)
        self.app = app
        self.revision = 1

    def _layout(self, item_id, depth, names):
        children = []
        if depth != 0:  # -1 = beliebig tief
            children = [
                self._layout(child, depth - 1, names) for child in MENU_CHILDREN.get(item_id, [])
            ]
        return dbus.Struct(
            (dbus.Int32(item_id), menu_item_props(item_id, names), dbus.Array(children, signature="v")),
            signature="ia{sv}av",
        )

    @dbus.service.method(MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        parent_id = int(parent_id)
        if parent_id not in MENU_ITEMS:
            raise _invalid_args(f"Unknown item {parent_id}")
        return dbus.UInt32(self.revision), self._layout(
            parent_id, int(recursion_depth), list(property_names)
        )

    @dbus.service.method(MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):
        wanted = [int(i) for i in ids] or list(MENU_ITEMS)
        return dbus.Array(
            [
                dbus.Struct((dbus.Int32(i), menu_item_props(i, list(property_names))), signature="ia{sv}")
                for i in wanted
                if i in MENU_ITEMS
            ],
            signature="(ia{sv})",
        )

    @dbus.service.method(MENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, item_id, name):
        props = menu_item_props(int(item_id)) if int(item_id) in MENU_ITEMS else {}
        if name not in props:
            raise _invalid_args(f"Unknown property {name}")
        return props[name]

    @dbus.service.method(MENU_IFACE, in_signature="isvu", out_signature="")
    def Event(self, item_id, event_id, data, timestamp):
        if event_id != "clicked":
            return
        action = MENU_ACTIONS.get(int(item_id))
        if action == "open":
            self.app.open_window()
        elif action == "refresh":
            self.app.force_refresh()
        elif action == "quit":
            self.app.quit()

    @dbus.service.method(MENU_IFACE, in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        unknown = []
        for item_id, event_id, data, timestamp in events:
            if int(item_id) in MENU_ITEMS:
                self.Event(item_id, event_id, data, timestamp)
            else:
                unknown.append(dbus.Int32(item_id))
        return dbus.Array(unknown, signature="i")

    @dbus.service.method(MENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, item_id):
        return False

    @dbus.service.method(MENU_IFACE, in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], signature="i"), dbus.Array([], signature="i")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        if property_name not in self.PROPS:
            raise _invalid_args(f"Unknown property {property_name}")
        return self.PROPS[property_name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        return dbus.Dictionary(self.PROPS, signature="sv")

    @dbus.service.signal(MENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass


# ----------------------------------------------------------------------- App

CSS = """
window.limits-card {
  background-color: @theme_bg_color;
  border-radius: 12px;
  border: 1px solid alpha(@theme_fg_color, 0.18);
}
.limits-title { font-weight: bold; font-size: 1.02em; }
.limit-name { font-weight: 600; }
.limit-value { font-weight: bold; font-size: 1.15em; }
.limit-reset { opacity: 0.65; font-size: 0.85em; }
.footer { opacity: 0.6; font-size: 0.8em; }
.hint { opacity: 0.75; font-size: 0.85em; }
progressbar trough { min-height: 8px; border-radius: 4px; }
progressbar progress { min-height: 8px; border-radius: 4px; }
""" + "".join(
    f"progressbar.{name} progress {{ background-color: #{r:02x}{g:02x}{b:02x}; }}\n"
    for name, (r, g, b) in LEVEL_RGB.items()
)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class LimitsApp:
    def __init__(self, bus, demo=False):
        self.bus = bus
        self.demo = demo
        self.limits, cached_ts = ([], 0.0) if demo else load_cache()
        self.last_update: float | None = cached_ts or None
        self.error: str | None = None
        self.rate_limited = False
        self.rate_limit_streak = 0
        self.auth_failed = False
        self._cred_mtime: float | None = None
        self.retry_after = 0.0
        self.refreshing = False
        self._ever_active = False
        self._focus_in_at = 0.0
        self._win_size = (340, 260)
        self.rows: list[dict] = []

        self._build_window()
        self.sni = StatusNotifierItem(bus, self)
        self.menu = Menu(bus, self)

        if self.limits:
            self.render()
        GLib.timeout_add_seconds(1, self._periodic)
        self.refresh()

    # --- Daten ----------------------------------------------------------
    def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.set_footer("Aktualisiere …")

        def work():
            try:
                data = demo_limits() if self.demo else fetch_usage()
            except Exception as exc:  # noqa: BLE001 - jeder Fehler soll im Fenster sichtbar werden
                GLib.idle_add(self._apply, None, exc)
            else:
                GLib.idle_add(self._apply, data, None)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, data, exc):
        self.refreshing = False
        if exc is None:
            self.limits = data
            self.last_update = time.time()
            self.error = None
            self.rate_limited = self.auth_failed = False
            self.rate_limit_streak = 0
            self.retry_after = 0.0
            if not self.demo:
                save_cache(data)
            self.render()
        else:
            self.error = str(exc) or type(exc).__name__
            self.rate_limited = isinstance(exc, RateLimitError)
            self.rate_limit_streak = self.rate_limit_streak + 1 if self.rate_limited else 0
            self.auth_failed = isinstance(exc, AuthError)
            self._cred_mtime = _mtime(CREDENTIALS)
            # Bei Fehlern (z. B. 429) nicht im Minutentakt weiter anfragen
            self.retry_after = time.time() + retry_delay(exc, self.rate_limit_streak)
            self.render_error()
        self._tick()
        try:
            self.sni.refresh_icon()
        except dbus.DBusException:
            pass  # Tray darf nie die App killen
        return False

    def _refresh_due(self, now: float) -> bool:
        if self.auth_failed and _mtime(CREDENTIALS) != self._cred_mtime:
            return True  # Claude Code hat den Token vermutlich erneuert
        if now < self.retry_after:
            return False
        if self.error:
            return True  # Wartezeit nach dem Fehler ist abgelaufen
        interval = UI_REFRESH_SECONDS if self.win.get_visible() else BG_REFRESH_SECONDS
        return self.last_update is None or now - self.last_update >= interval

    def icon_percent(self) -> float | None:
        return max((limit.percent for limit in self.limits), default=None)

    def is_stale(self) -> bool:
        return bool(self.error and self.limits)

    def tooltip_lines(self) -> tuple[str, str]:
        if not self.limits:
            return "Claude Limits", self.error[:110] if self.error else "Lade …"
        values = " · ".join(f"{l.label.split(' · ')[0]}: {l.percent:.0f} %" for l in self.limits[:2])
        if self.error:
            return "Claude Limits (veraltet)", f"{values}\n{self.error[:110]}"
        return "Claude Limits", values

    # --- UI -------------------------------------------------------------
    def _build_window(self):
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("Claude Limits")
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_position(Gtk.WindowPosition.NONE)
        self.win.set_border_width(0)
        self.win.set_decorated(False)
        self.win.set_keep_above(True)
        visual = self.win.get_screen().get_rgba_visual()
        if visual is not None:
            self.win.set_visual(visual)
            self.win.set_app_paintable(True)
            self.win.connect("draw", self._on_draw)
        self.win.get_style_context().add_class("limits-card")
        self.win.connect("delete-event", self._hide_window)
        self.win.connect("focus-out-event", self._maybe_hide)
        self.win.connect("focus-in-event", self._on_focus_in)
        self.win.connect("key-press-event", self._on_key)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_size_request(340, -1)
        self.outer = outer

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        header.set_margin_start(16)
        header.set_margin_end(10)
        header.set_margin_top(10)
        header.set_margin_bottom(6)
        header.set_size_request(-1, 30)
        header.connect("button-press-event", self._start_drag)
        title = Gtk.Label(label="Claude Limits")
        title.get_style_context().add_class("limits-title")
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)
        refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh_button.set_relief(Gtk.ReliefStyle.NONE)
        refresh_button.set_tooltip_text("Aktualisieren")
        refresh_button.connect("clicked", lambda *_: self.force_refresh())
        header.pack_end(refresh_button, False, False, 0)
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_tooltip_text("Schließen")
        close_button.connect("clicked", self._hide_window)
        header.pack_end(close_button, False, False, 0)
        outer.pack_start(header, False, False, 0)

        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.rows_box.set_margin_start(16)
        self.rows_box.set_margin_end(16)
        self.rows_box.set_margin_top(4)
        self.rows_box.set_margin_bottom(10)
        outer.pack_start(self.rows_box, False, False, 0)

        self.error_label = Gtk.Label()
        self.error_label.set_line_wrap(True)
        self.error_label.set_max_width_chars(42)
        self.error_label.set_xalign(0)
        self.error_label.get_style_context().add_class("hint")
        self.error_label.set_margin_start(16)
        self.error_label.set_margin_end(16)
        self.error_label.set_margin_bottom(10)
        self.error_label.set_no_show_all(True)
        outer.pack_start(self.error_label, False, False, 0)

        self.footer = Gtk.Label(label="")
        self.footer.get_style_context().add_class("footer")
        self.footer.set_xalign(0)
        self.footer.set_margin_start(16)
        self.footer.set_margin_end(16)
        self.footer.set_margin_bottom(12)
        outer.pack_start(self.footer, False, False, 0)

        self.win.add(outer)

    def _clear_rows(self):
        for child in self.rows_box.get_children():
            self.rows_box.remove(child)
        self.rows = []

    def _add_row(self, limit: Limit):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=limit.label)
        name.get_style_context().add_class("limit-name")
        name.set_xalign(0)
        value = Gtk.Label(label=f"{limit.percent:.0f} %")
        value.get_style_context().add_class("limit-value")
        value.set_xalign(1)
        top.pack_start(name, True, True, 0)
        top.pack_end(value, False, False, 0)

        bar = Gtk.ProgressBar()
        bar.set_fraction(min(limit.percent, 100.0) / 100.0)
        bar.set_show_text(False)
        bar.get_style_context().add_class(_level(limit.percent))

        reset = Gtk.Label(label=_reset_text(limit))
        reset.get_style_context().add_class("limit-reset")
        reset.set_xalign(0)

        box.pack_start(top, False, False, 0)
        box.pack_start(bar, False, False, 0)
        box.pack_start(reset, False, False, 0)
        self.rows_box.pack_start(box, False, False, 0)
        self.rows.append({"limit": limit, "reset": reset})

    def render(self):
        self._clear_rows()
        for limit in self.limits:
            self._add_row(limit)
        self.rows_box.set_opacity(1.0)
        self.error_label.hide()
        self.rows_box.show_all()
        if self.win.get_visible():
            self._fit(place=False)

    def render_error(self):
        text = self.error or "Unbekannter Fehler"
        if self.rate_limited:
            text = "Anfrage-Limit erreicht (HTTP 429) – automatische Pause bis zum nächsten Versuch."
        self.error_label.set_text(text)
        self.error_label.show()
        if self.limits:
            self.rows_box.set_opacity(0.55)  # Werte bleiben sichtbar, aber erkennbar veraltet
        else:
            self._clear_rows()
        if self.win.get_visible():
            self._fit(place=False)

    def set_footer(self, text):
        self.footer.set_text(text)

    def _tick(self):
        """Laufende Countdowns und Statuszeile aktualisieren."""
        if not self.win.get_visible():
            return
        for row in self.rows:
            row["reset"].set_text(_reset_text(row["limit"]))
        if self.refreshing:
            return  # Fußzeile zeigt "Aktualisiere …"
        now = time.time()
        parts = []
        if self.error:
            if self.retry_after > now:
                parts.append(f"Nächster Versuch in {_duration(self.retry_after - now)}")
            if self.last_update:
                parts.append(f"Werte von vor {_duration(now - self.last_update)}")
        elif self.last_update:
            parts.append(f"Aktualisiert vor {_duration(now - self.last_update)}")
            parts.append(f"auto alle {UI_REFRESH_SECONDS} s")
        self.set_footer(" · ".join(parts))

    # --- Verhalten ------------------------------------------------------
    def _fit(self, place=True):
        """Größe vor dem Anzeigen bestimmen, damit kein Zwischenzustand flackert."""
        self.outer.show_all()
        self.error_label.set_visible(bool(self.error))
        natural = self.outer.get_preferred_size()[1]
        self._win_size = (max(340, natural.width), natural.height)
        self.win.resize(*self._win_size)
        self.win.show()
        if place:
            self._place_near_pointer()
        self.win.queue_draw()

    def _place_near_pointer(self):
        """Fenster neben den Mauszeiger setzen, aber komplett in der Arbeitsfläche."""
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display else None
        if seat is None:
            return
        _screen, x, y = seat.get_pointer().get_position()
        area = display.get_monitor_at_point(x, y).get_workarea()
        w, h = self._win_size
        px = min(max(area.x + 8, x - w // 2), area.x + area.width - w - 8)
        py = y - h - 12
        if py < area.y + 8:
            py = y + 24
        py = min(max(area.y + 8, py), area.y + area.height - h - 8)
        self.win.move(int(px), int(py))

    def open_window(self):
        self._ever_active = False
        self._focus_in_at = 0.0
        self._fit()
        self._present()
        self._tick()

    def _present(self):
        # Aufruf kommt per D-Bus, nicht als GTK-Ereignis: ohne aktuellen
        # Zeitstempel hält Muffin das für Fokus-Diebstahl und legt das
        # Fenster hinter das aktive Fenster.
        gdk_win = self.win.get_window()
        if GdkX11 is not None and isinstance(gdk_win, GdkX11.X11Window):
            self.win.present_with_time(GdkX11.x11_get_server_time(gdk_win))
        else:
            self.win.present()

    def _hide_window(self, *_):
        self.win.hide()
        return True

    def _on_focus_in(self, *_):
        self._ever_active = True
        self._focus_in_at = time.time()
        return False

    def _maybe_hide(self, *_):
        GLib.timeout_add(120, self._hide_if_unfocused)
        return False

    def _hide_if_unfocused(self):
        # Schließen erst, wenn der Fokus stabil lag; verhindert, dass das
        # Fenster beim Öffnen über Tray/Menü sofort wieder verschwindet.
        focused_for = time.time() - self._focus_in_at
        if self._ever_active and focused_for > 0.4 and not self.win.is_active():
            self.win.hide()
        return False

    def _on_draw(self, widget, cr):
        """Abgerundeter Hintergrund (Fenster ist app-paintable für die Ecken)."""
        alloc = widget.get_allocation()
        w, h, r = alloc.width, alloc.height, 12.0
        cr.new_sub_path()
        cr.arc(w - r, r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        ctx = widget.get_style_context()
        ok, bg = ctx.lookup_color("theme_bg_color")
        if not ok:
            bg = Gdk.RGBA()
            bg.parse("#1f1f22")
        Gdk.cairo_set_source_rgba(cr, bg)
        cr.fill_preserve()
        ok, border = ctx.lookup_color("theme_fg_color")
        if not ok:
            border = Gdk.RGBA()
            border.parse("#ffffff")
        border.alpha = 0.16  # passt zu hellen und dunklen Themes
        Gdk.cairo_set_source_rgba(cr, border)
        cr.set_line_width(1)
        cr.stroke()
        return False

    def _start_drag(self, _widget, event):
        if event.button == 1:
            self.win.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.win.hide()
            return True
        return False

    def force_refresh(self):
        if not self.win.get_visible():
            self.open_window()
        if self.rate_limited and time.time() < self.retry_after:
            return  # Bei 429 würde jede weitere Anfrage die Sperre nur verlängern
        self.refresh()

    def _periodic(self):
        if not self.refreshing and self._refresh_due(time.time()):
            self.refresh()
        self._tick()
        return True

    def quit(self):
        Gtk.main_quit()


def _cli_print():
    try:
        limits = fetch_usage()
    except AuthError as exc:
        print(f"Auth-Fehler: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    for limit in limits:
        print(f"{limit.label:24s} {limit.percent:6.1f} %  Reset {_local_time(limit.resets_at)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-limits-tray", description="Zeigt die Claude-Nutzungslimits im Tray."
    )
    parser.add_argument("--demo", action="store_true", help="Beispielwerte, ohne Netzwerk")
    parser.add_argument(
        "--print", dest="print_only", action="store_true", help="Werte einmal auf der Konsole ausgeben"
    )
    args = parser.parse_args(argv)
    if args.print_only:
        return _cli_print()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SessionBus()
    except dbus.DBusException as exc:
        print(f"Keine D-Bus-Session erreichbar: {exc}", file=sys.stderr)
        return 1

    # Einzelinstanz: läuft bereits eine, nur deren Fenster öffnen
    try:
        _control = dbus.service.BusName(CONTROL_NAME, bus, do_not_queue=True)  # noqa: F841 - Name halten
    except dbus.exceptions.NameExistsException:
        try:
            bus.get_object(CONTROL_NAME, SNI_PATH).Activate(0, 0, dbus_interface=SNI_IFACE)
        except dbus.DBusException:
            pass
        return 0

    _sni_name = dbus.service.BusName(SNI_SERVICE, bus)  # noqa: F841 - Name halten
    app = LimitsApp(bus, demo=args.demo)

    # Beim StatusNotifierWatcher (Cinnamon-Panel) registrieren, ggf. mit Wiederholung
    def register_sni(remaining=6):
        try:
            watcher = bus.get_object(WATCHER_NAME, WATCHER_PATH)
            watcher.RegisterStatusNotifierItem(SNI_SERVICE, dbus_interface=WATCHER_NAME)
        except dbus.DBusException as exc:
            if remaining > 1:
                GLib.timeout_add_seconds(2, register_sni, remaining - 1)
            else:
                print(f"Konnte Tray-Icon nicht registrieren: {exc}", file=sys.stderr)
        return False

    # Startet das Panel neu (z. B. Cinnamon-Neustart), kommt ein neuer Watcher:
    # dann erneut anmelden, sonst bliebe das Symbol verschwunden.
    def on_watcher_owner(_name, _old_owner, new_owner):
        if new_owner:
            register_sni()

    bus.add_signal_receiver(
        on_watcher_owner,
        signal_name="NameOwnerChanged",
        dbus_interface="org.freedesktop.DBus",
        bus_name="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        arg0=WATCHER_NAME,
    )
    register_sni()

    try:
        Gtk.main()
    except KeyboardInterrupt:
        app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
