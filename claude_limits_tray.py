#!/usr/bin/env python3
"""Claude-Limits-Tray für Cinnamon/GTK.

Linksklick auf das Tray-Icon öffnet ein kleines Fenster mit dem
5-Stunden- und Wochen-Limit des Claude-Abonnements.

Datenquelle: GET https://api.anthropic.com/api/oauth/usage
Auth: OAuth-Access-Token aus ~/.claude/.credentials.json (wird nur im
Speicher gehalten, nie ausgegeben).
"""

from __future__ import annotations

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
from PIL import Image, ImageDraw

APP_ID = "claude-limits-tray"
SNI_SERVICE = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
CONTROL_NAME = "org.bene.ClaudeLimitsTray"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CACHE_FILE = Path.home() / ".cache" / "claude-limits-tray" / "usage.json"

BG_REFRESH_SECONDS = 300  # Hintergrund-Aktualisierung
UI_REFRESH_SECONDS = 60   # Aktualisierung bei offenem Fenster

# Reihenfolge und Beschriftung der angezeigten Limits
LIMIT_KEYS = [
    ("five_hour", "5-Stunden-Limit"),
    ("seven_day", "Wochen-Limit"),
    ("seven_day_sonnet", "Woche · Sonnet"),
    ("seven_day_opus", "Woche · Opus"),
    ("seven_day_oauth_apps", "Woche · Apps"),
    ("seven_day_cowork", "Woche · Cowork"),
]


# ---------------------------------------------------------------- Datenmodell


@dataclass
class Limit:
    key: str
    label: str
    percent: float
    resets_at: datetime | None


class AuthError(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    """HTTP 429 - mit Wartezeit aus dem Retry-After-Header."""

    def __init__(self, message, retry_after=180):
        super().__init__(message)
        self.retry_after = retry_after


def _find_token(node, depth: int = 0):
    """Sucht rekursiv den ersten *Token-String ohne ihn zu loggen."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and "token" in k.lower() and "refresh" not in k.lower():
                return v
        for v in node.values():
            found = _find_token(v, depth + 1)
            if found:
                return found
    elif isinstance(node, (list, tuple)):
        for v in node:
            found = _find_token(v, depth + 1)
            if found:
                return found
    return None


def load_token() -> str:
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env
    try:
        data = json.loads(CREDENTIALS.read_text())
    except FileNotFoundError:
        raise AuthError("Keine Claude-Anmeldung gefunden (~/.claude/.credentials.json fehlt).")
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"Anmeldedaten nicht lesbar: {type(exc).__name__}")
    # Bevorzugt der Claude-Code-Login; sonst irgendein Access-Token (z. B. MCP)
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str):
        return oauth["accessToken"]
    token = _find_token(data)
    if not token:
        raise AuthError("Kein Access-Token in den Anmeldedaten gefunden.")
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
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
        CACHE_FILE.write_text(json.dumps(payload))
    except OSError:
        pass


def _parse_reset(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_usage(timeout: float = 15.0) -> list[Limit]:
    token = load_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-limits-tray/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError(
                "Token abgelaufen oder ungültig. Claude Code einmal starten "
                "(oder /login ausführen), dann erneut versuchen."
            ) from exc
        if exc.code == 429:
            try:
                wait = int(exc.headers.get("retry-after") or 180)
            except (TypeError, ValueError):
                wait = 180
            raise RateLimitError(
                "Zu viele Anfragen (HTTP 429)", max(30, min(wait, 1800))
            ) from exc
        raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Keine Verbindung: {exc.reason}") from exc

    # Nutzlast kann direkt die Limits enthalten oder unter "usage" liegen
    data = payload.get("usage", payload) if isinstance(payload, dict) else {}

    raw: dict[str, dict] = {}
    for key, _ in LIMIT_KEYS:
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("utilization") is not None:
            raw[key] = entry

    # utilization kommt je nach Antwort als 0..1 oder 0..100
    values = [float(v.get("utilization") or 0) for v in raw.values()]
    as_percent = any(v > 1.0 for v in values)

    limits = []
    for key, label in LIMIT_KEYS:
        entry = raw.get(key)
        if not entry:
            continue
        val = float(entry.get("utilization") or 0)
        pct = val if as_percent else val * 100.0
        limits.append(Limit(key, label, max(0.0, pct), _parse_reset(entry.get("resets_at"))))
    if not limits:
        raise RuntimeError("Antwort enthielt keine Limit-Daten.")
    return limits


# ------------------------------------------------------------------- Darstellung


def _color_for(percent: float) -> tuple[int, int, int]:
    if percent >= 100:
        return (229, 72, 77)
    if percent >= 80:
        return (240, 133, 45)
    if percent >= 50:
        return (240, 190, 60)
    return (86, 182, 110)


def _countdown(dt: datetime | None) -> str:
    if not dt:
        return "unbekannt"
    now = datetime.now(timezone.utc)
    secs = int((dt - now).total_seconds())
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


def render_icon(percent: float, size: int = 32) -> Image.Image:
    """Ringdiagramm: gefüllter Teil = verbrauchtes Limit."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 10)
    width = max(3, size // 5)
    box = (pad, pad, size - pad - 1, size - pad - 1)
    draw.arc(box, 0, 360, fill=(130, 130, 130, 110), width=width)
    sweep = min(percent, 100.0) / 100.0 * 360.0
    if sweep > 0.5:
        draw.arc(box, -90, -90 + sweep, fill=_color_for(percent) + (255,), width=width)
    return img


def icon_pixmaps(percent: float):
    """ARGB32-Pixmaps im SNI-Format (a(iiay))."""
    out = []
    for size in (22, 32, 48):
        img = render_icon(percent, size)
        r, g, b, a = img.split()
        argb = Image.merge("RGBA", (a, r, g, b)).tobytes()  # A,R,G,B
        out.append(dbus.Struct((size, size, dbus.ByteArray(argb)), signature="iiay"))
    return dbus.Array(out, signature="(iiay)")


# ------------------------------------------------------------------ Tray (SNI)


class StatusNotifierItem(dbus.service.Object):
    def __init__(self, bus, app: "LimitsApp"):
        super().__init__(bus, SNI_PATH)
        self.app = app

    # --- Methoden -------------------------------------------------------
    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii", out_signature="")
    def Activate(self, x, y):
        self.app.open_window()

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        self.app.force_refresh()

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        pass  # Menü wird vom Host über Menu-Objekt geöffnet

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    # --- Properties -----------------------------------------------------
    def _props(self) -> dict:
        percent = self.app.max_percent()
        tooltip = self.app.tooltip_lines()
        tt = dbus.Struct(
            (
                "",
                icon_pixmaps(percent),
                dbus.String(tooltip[0]),
                dbus.String(tooltip[1] if len(tooltip) > 1 else ""),
            ),
            signature="sa(iiay)ss",
        )
        return {
            "Category": dbus.String("ApplicationStatus"),
            "Id": dbus.String(APP_ID),
            "Title": dbus.String("Claude Limits"),
            "Status": dbus.String("Active"),
            "WindowId": dbus.Int32(0),
            "IconName": dbus.String(""),
            "IconPixmap": icon_pixmaps(percent),
            "OverlayIconName": dbus.String(""),
            "OverlayIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionIconName": dbus.String(""),
            "AttentionIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionMovieName": dbus.String(""),
            "ToolTip": tt,
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

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface_name, property_name, value):
        pass

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface_name, changed, invalidated):
        pass

    @dbus.service.signal("org.kde.StatusNotifierItem", signature="")
    def NewIcon(self):
        pass

    @dbus.service.signal("org.kde.StatusNotifierItem", signature="")
    def NewToolTip(self):
        pass

    def refresh_icon(self):
        self.NewIcon()
        self.NewToolTip()
        self.PropertiesChanged(
            "org.kde.StatusNotifierItem",
            dbus.Dictionary(
                {
                    "IconPixmap": icon_pixmaps(self.app.max_percent()),
                    "ToolTip": self._props()["ToolTip"],
                },
                signature="sv",
            ),
            dbus.Array([], signature="s"),
        )


# ------------------------------------------------------------------- dbusmenu


class Menu(dbus.service.Object):
    ACTIONS = {1: "open", 2: "refresh", 4: "quit"}

    def __init__(self, bus, app: "LimitsApp"):
        super().__init__(bus, MENU_PATH)
        self.app = app
        self.revision = 1

    def _items(self):
        return {
            0: {"label": "", "visible": True, "enabled": True, "type": "root"},
            1: {"label": "Fenster öffnen", "enabled": True, "visible": True},
            2: {"label": "Aktualisieren", "enabled": True, "visible": True},
            3: {"label": "", "enabled": False, "visible": True, "type": "separator"},
            4: {"label": "Beenden", "enabled": True, "visible": True},
        }

    def _layout(self, item_id, depth):
        items = self._items()
        meta = items[item_id]
        props = dbus.Dictionary({}, signature="sv")
        if meta.get("type") == "separator":
            props["type"] = dbus.String("separator")
        elif meta.get("type") == "root":
            props["children-display"] = dbus.String("submenu")
        else:
            props["label"] = dbus.String(meta["label"])
        props["enabled"] = dbus.Boolean(meta.get("enabled", True))
        props["visible"] = dbus.Boolean(meta.get("visible", True))

        children = dbus.Array([], signature="v")
        if depth != 0:
            child_ids = {0: [1, 2, 3, 4]}.get(item_id, [])
            children = dbus.Array(
                [self._layout(c, depth - 1 if depth > 0 else -1) for c in child_ids],
                signature="v",
            )
        return dbus.Struct(
            (dbus.Int32(item_id), props, children), signature="ia{sv}av"
        )

    @dbus.service.method(
        "com.canonical.dbusmenu",
        in_signature="iias",
        out_signature="u(ia{sv}av)",
    )
    def GetLayout(self, parent_id, recursion_depth, property_names):
        return dbus.UInt32(self.revision), self._layout(int(parent_id), int(recursion_depth))

    @dbus.service.method(
        "com.canonical.dbusmenu", in_signature="aias", out_signature="a(ia{sv})"
    )
    def GetGroupProperties(self, ids, property_names):
        items = self._items()
        out = []
        for i in ids:
            meta = items.get(int(i))
            if not meta:
                continue
            props = dbus.Dictionary({}, signature="sv")
            props["label"] = dbus.String(meta.get("label", ""))
            props["enabled"] = dbus.Boolean(meta.get("enabled", True))
            props["visible"] = dbus.Boolean(meta.get("visible", True))
            out.append(dbus.Struct((dbus.Int32(int(i)), props), signature="ia{sv}"))
        return dbus.Array(out, signature="(ia{sv})")

    @dbus.service.method(
        "com.canonical.dbusmenu", in_signature="isvu", out_signature=""
    )
    def Event(self, item_id, event_id, data, timestamp):
        if event_id != "clicked":
            return
        action = self.ACTIONS.get(int(item_id))
        if action == "open":
            self.app.open_window()
        elif action == "refresh":
            self.app.force_refresh()
        elif action == "quit":
            self.app.quit()

    @dbus.service.method("com.canonical.dbusmenu", in_signature="i", out_signature="b")
    def AboutToShow(self, item_id):
        return False

    @dbus.service.signal("com.canonical.dbusmenu", signature="ui")
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
progressbar.ok progress { background-color: #56b66e; }
progressbar.warn progress { background-color: #f0be3c; }
progressbar.hot progress { background-color: #f0852d; }
progressbar.crit progress { background-color: #e5484d; }
"""


class LimitsApp:
    def __init__(self, bus, demo=False):
        self.demo = demo
        self.bus = bus
        self.limits: list[Limit] = []
        self.error: str | None = None
        self.last_update: float | None = None
        self.refreshing = False
        self.demo_shift = 0.0
        self._ever_active = False
        self._focus_in_at = 0.0
        self.retry_after = 0.0
        self.limits, cached_ts = load_cache()
        self.last_update = cached_ts or None

        self._build_window()
        self.sni = StatusNotifierItem(bus, self)
        self.menu = Menu(bus, self)

        GLib.timeout_add_seconds(5, self._periodic)
        if self.limits:
            self.render()
        self.refresh(initial=True)

    # --- Daten ----------------------------------------------------------
    @staticmethod
    def _demo_limits(shift=0.0):
        now = datetime.now(timezone.utc)
        return [
            Limit("five_hour", "5-Stunden-Limit", 34 + shift, now + timedelta(hours=2, minutes=17)),
            Limit("seven_day", "Wochen-Limit", 61 + shift, now + timedelta(days=3, hours=4)),
            Limit("seven_day_sonnet", "Woche · Sonnet", 22 + shift, now + timedelta(days=3, hours=4)),
        ]

    def refresh(self, initial=False, from_timer=False):
        if self.refreshing:
            return
        self.refreshing = True
        if not initial:
            self.set_footer("Aktualisiere …")

        def work():
            try:
                data = self._demo_limits(self.demo_shift) if self.demo else fetch_usage()
                GLib.idle_add(self._apply, data, None)
            except RateLimitError as exc:
                GLib.idle_add(self._apply, None, str(exc), exc.retry_after)
            except AuthError as exc:
                GLib.idle_add(self._apply, None, str(exc), 300)
            except Exception as exc:  # noqa: BLE001 - Fehler sollen sichtbar werden
                GLib.idle_add(self._apply, None, str(exc), 180)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, data, error, wait=None):
        self.refreshing = False
        self.error = error
        if data is not None:
            self.limits = data
            self.last_update = time.time()
            self.retry_after = 0.0
            if not self.demo:
                save_cache(data)
            self.render()
        else:
            # Bei Fehlern (z. B. 429) nicht im Minutentakt weiter anfragen
            self.retry_after = time.time() + (wait or 180)
            self.render_error()
        try:
            self.sni.refresh_icon()
        except Exception:  # Tray darf nie die App killen
            pass
        return False

    def max_percent(self) -> float:
        return max((limit.percent for limit in self.limits), default=0.0)

    def tooltip_lines(self):
        if self.error:
            return ["Claude Limits", self.error[:110]]
        if not self.limits:
            return ["Claude Limits", "Lade …"]
        parts = [f"{l.label.split(' · ')[0]}: {l.percent:.0f} %" for l in self.limits[:2]]
        return ["Claude Limits", " · ".join(parts)]

    # --- UI -------------------------------------------------------------
    def _build_window(self):
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("Claude Limits")
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_position(Gtk.WindowPosition.NONE)
        self.win.set_border_width(0)
        self.win.set_decorated(False)
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
        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.refresh_button.set_relief(Gtk.ReliefStyle.NONE)
        self.refresh_button.set_tooltip_text("Aktualisieren")
        self.refresh_button.connect("clicked", lambda *_: self.force_refresh())
        header.pack_end(self.refresh_button, False, False, 0)
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_tooltip_text("Schließen")
        close_button.connect("clicked", lambda *_: self.win.hide())
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
        self.win.show_all()
        self.error_label.hide()
        self.win.hide()
        self.rows: list[dict] = []

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
        _apply_bar_color(bar, limit.percent)

        reset = Gtk.Label(
            label=f"Zurückgesetzt in {_countdown(limit.resets_at)} · {_local_time(limit.resets_at)}"
        )
        reset.get_style_context().add_class("limit-reset")
        reset.set_xalign(0)

        box.pack_start(top, False, False, 0)
        box.pack_start(bar, False, False, 0)
        box.pack_start(reset, False, False, 0)
        self.rows_box.pack_start(box, False, False, 0)
        self.rows.append({"limit": limit, "value": value, "bar": bar, "reset": reset})

    def render(self):
        self._clear_rows()
        for limit in self.limits:
            self._add_row(limit)
        self.error_label.hide()
        self.rows_box.show_all()
        self.set_footer(f"Aktualisiert {datetime.now().strftime('%H:%M:%S')}")
        if self.win.get_visible():
            self._fit(place=False)

    def render_error(self):
        text = self.error or "Unbekannter Fehler"
        if "429" in text:
            text = "Anfrage-Limit erreicht (HTTP 429) – Pause bis zum nächsten Versuch"
        self.error_label.set_text(text)
        self.error_label.show()
        if not self.limits:
            self._clear_rows()
        self.set_footer(f"Letzter Versuch {datetime.now().strftime('%H:%M:%S')}")

    def set_footer(self, text):
        self.footer.set_text(text)

    def _tick(self):
        """Laufende Countdowns und Statuszeile aktualisieren."""
        if not self.win.get_visible():
            return False
        for row in self.rows:
            limit: Limit = row["limit"]
            row["value"].set_text(f"{limit.percent:.0f} %")
            row["reset"].set_text(
                f"Zurückgesetzt in {_countdown(limit.resets_at)} · {_local_time(limit.resets_at)}"
            )
        if self.error and self.retry_after > time.time():
            wait = int(self.retry_after - time.time())
            if self.last_update:
                age = int(time.time() - self.last_update)
                self.set_footer(f"Nächster Versuch in {wait} s · Werte von vor {age} s")
            else:
                self.set_footer(f"Nächster Versuch in {wait} s")
        elif self.last_update and not self.refreshing:
            age = int(time.time() - self.last_update)
            self.set_footer(f"Aktualisiert vor {age} s · Auto alle {UI_REFRESH_SECONDS} s")
        return False

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
        if display is None:
            return
        seat = display.get_default_seat()
        if seat is None:
            return
        ok, x, y = seat.get_pointer().get_position()
        if not ok:
            return
        monitor = display.get_monitor_at_point(x, y)
        area = monitor.get_workarea()
        w, h = getattr(self, "_win_size", (340, 260))
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
        self.win.present()
        self._tick()

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
        # Schliessen erst, wenn der Fokus stabil lag; verhindert, dass das
        # Fenster beim Oeffnen ueber Tray/Menue sofort wieder verschwindet.
        focused_for = time.time() - getattr(self, "_focus_in_at", 0.0)
        if getattr(self, "_ever_active", False) and focused_for > 0.4 and not self.win.is_active():
            self.win.hide()
        return False

    def _on_draw(self, widget, cr):
        """Abgerundeter Hintergrund (Fenster ist app-paintable fuer Ecken)."""
        alloc = widget.get_allocation()
        w, h, r = alloc.width, alloc.height, 12.0
        cr.new_sub_path()
        cr.arc(w - r, r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        ctx = widget.get_style_context()
        ok, color = ctx.lookup_color("theme_bg_color")
        if not ok:
            color = Gdk.RGBA()
            color.parse("#1f1f22")
        Gdk.cairo_set_source_rgba(cr, color)
        cr.fill_preserve()
        border = Gdk.RGBA()
        border.parse("rgba(255,255,255,0.16)")
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
        self.refresh()

    def _periodic(self):
        interval = UI_REFRESH_SECONDS if self.win.get_visible() else BG_REFRESH_SECONDS
        now = time.time()
        due = now >= self.retry_after and (
            self.last_update is None or now - self.last_update >= interval
        )
        if due:
            self.refresh()
        self._tick()
        GLib.timeout_add_seconds(1, self._periodic)
        return False

    def quit(self):
        try:
            self.bus.release_name(SNI_SERVICE)
        except dbus.DBusException:
            pass
        Gtk.main_quit()


def _apply_bar_color(bar: Gtk.ProgressBar, percent: float):
    ctx = bar.get_style_context()
    for name in ("ok", "warn", "hot", "crit"):
        ctx.remove_class(name)
    if percent >= 100:
        cls = "crit"
    elif percent >= 80:
        cls = "hot"
    elif percent >= 50:
        cls = "warn"
    else:
        cls = "ok"
    ctx.add_class(cls)


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


def main():
    if "--print" in sys.argv:
        sys.exit(_cli_print())
    demo = "--demo" in sys.argv

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    # Einzelinstanz: laeuft bereits eine, nur deren Fenster oeffnen
    try:
        control = dbus.service.BusName(CONTROL_NAME, bus, do_not_queue=True)
    except dbus.exceptions.NameExistsException:
        try:
            existing = bus.get_object(CONTROL_NAME, SNI_PATH)
            existing.Activate(0, 0, dbus_interface="org.kde.StatusNotifierItem")
        except dbus.DBusException:
            pass
        return

    name = dbus.service.BusName(SNI_SERVICE, bus)
    app = LimitsApp(bus, demo=demo)

    # Beim StatusNotifierWatcher (Cinnamon-Panel) registrieren, ggf. mit Wiederholung
    def register_sni(remaining=6):
        try:
            watcher = bus.get_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
            watcher.RegisterStatusNotifierItem(
                SNI_SERVICE, dbus_interface="org.kde.StatusNotifierWatcher"
            )
            return False
        except dbus.DBusException as exc:
            if remaining > 1:
                GLib.timeout_add_seconds(2, register_sni, remaining - 1)
            else:
                print(f"Konnte Tray-Icon nicht registrieren: {exc}", file=sys.stderr)
            return False

    register_sni()

    try:
        Gtk.main()
    except KeyboardInterrupt:
        app.quit()
    bus.release_name(SNI_SERVICE)


if __name__ == "__main__":
    main()
