#!/usr/bin/env python3
"""Abo-Limits-Tray für Cinnamon/GTK (Claude und Codex).

Das Tray-Symbol zeigt das Abo, das gerade benutzt wird; ein Linksklick öffnet
ein kleines Fenster mit den Limits aller Abos. Datenquellen und Auswahl
liegen in providers.py.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone

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
from PIL import Image, ImageDraw, ImageFont

import providers
from providers import Account, AuthError, Limit

APP_ID = "claude-limits-tray"
APP_TITLE = "Abo-Limits"
SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_SERVICE = f"{SNI_IFACE}-{os.getpid()}-1"
SNI_PATH = "/StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
MENU_IFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
CONTROL_NAME = "org.bene.ClaudeLimitsTray"

BG_REFRESH_SECONDS = 300    # Hintergrund-Aktualisierung je Abo
UI_REFRESH_SECONDS = 60     # Aktualisierung bei offenem Fenster
ACTIVITY_SCAN_SECONDS = 10  # wie oft geprüft wird, welches Abo gerade benutzt wird

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
]

# Farbstufen: Ring im Tray und Balken im Fenster nutzen dieselben Werte
LEVEL_RGB = {
    "ok": (86, 182, 110),
    "warn": (240, 190, 60),
    "hot": (240, 133, 45),
    "crit": (229, 72, 77),
}


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


def _font(px: int):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def render_icon(
    percent: float | None, size: int = 32, stale: bool = False, letter: str = ""
) -> Image.Image:
    """Ringdiagramm: gefüllter Teil = verbrauchtes Limit, Buchstabe = Abo.

    Ohne Daten erscheint ein gepunkteter Ring, bei veralteten Werten (Fehler)
    ein blasser Ring - beides soll nicht wie ein gültiger Wert aussehen.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    width = max(2, size // 7)
    box = (pad, pad, size - pad - 1, size - pad - 1)
    if percent is None:
        for start in range(-90, 270, 30):
            draw.arc(box, start, start + 14, fill=(150, 150, 150, 220), width=width)
        return img
    alpha = 120 if stale else 255
    color = LEVEL_RGB[_level(percent)]
    draw.arc(box, 0, 360, fill=(130, 130, 130, 110), width=width)
    sweep = min(percent, 100.0) / 100.0 * 360.0
    if sweep > 0.5:
        draw.arc(box, -90, -90 + sweep, fill=color + (alpha,), width=width)
    if letter:
        inner = size - 2 * (pad + width)
        draw.text(
            (size / 2, size / 2), letter, font=_font(max(7, int(inner * 0.95))),
            fill=color + (alpha,), anchor="mm",
        )
    return img


def icon_pixmaps(percent: float | None, stale: bool = False, letter: str = ""):
    """ARGB32-Pixmaps im SNI-Format (a(iiay))."""
    out = []
    for size in (22, 32, 48, 64):
        r, g, b, a = render_icon(percent, size, stale, letter).split()
        argb = Image.merge("RGBA", (a, r, g, b)).tobytes()  # A,R,G,B
        out.append(dbus.Struct((size, size, dbus.ByteArray(argb)), signature="iiay"))
    return dbus.Array(out, signature="(iiay)")


def account_summary(account: Account) -> str:
    """Tooltip-Zeile: "Codex B · Plus: 5 h 72 % (↻ 2 Std 16 Min) · Woche 40 % (↻ 3 T 4 Std)"."""
    if not account.limits:
        return f"{account.title}: {account.error[:80] if account.error else 'lädt …'}"
    values = " · ".join(
        f"{providers.short_label(l.label)} {l.percent:.0f} %"
        + (f" (↻ {_countdown(l.resets_at)})" if l.resets_at else "")
        for l in account.limits
    )
    return f"{account.title}: {values}{' (veraltet)' if account.error else ''}"


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
        shown = self.app.shown
        pixmaps = icon_pixmaps(
            shown.max_percent() if shown else None,
            stale=bool(shown and shown.is_stale()),
            letter=shown.short if shown else "",
        )
        title, text = self.app.tooltip_lines()
        tooltip = dbus.Struct(
            ("", pixmaps, dbus.String(title), dbus.String(text)), signature="sa(iiay)ss"
        )
        return {
            "Category": dbus.String("ApplicationStatus"),
            "Id": dbus.String(APP_ID),
            "Title": dbus.String(APP_TITLE),
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
.account-title { font-weight: bold; }
.account-badge { opacity: 0.65; font-size: 0.85em; }
.account-badge.shown { opacity: 1; }
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


def _label(text="", css=None, xalign=0.0) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(xalign)
    if css:
        label.get_style_context().add_class(css)
    return label


class LimitsApp:
    def __init__(self, bus, accounts: list[Account], demo=False):
        self.bus = bus
        self.accounts = accounts
        self.demo = demo
        if not demo:
            providers.restore_cache(accounts)
        self.shown: Account | None = None
        self._last_scan = 0.0
        self._icon_at = 0.0
        self._ever_active = False
        self._focus_in_at = 0.0
        self._win_size = (360, 300)
        self.sections: list[dict] = []

        self._build_window()
        self.sni = StatusNotifierItem(bus, self)
        self.menu = Menu(bus, self)

        self._scan_activity()
        self._update_view()
        GLib.timeout_add_seconds(1, self._periodic)
        for account in accounts:
            self.refresh(account)

    # --- Daten ----------------------------------------------------------
    def refresh(self, account: Account):
        if account.refreshing:
            return
        account.refreshing = True
        self._tick()

        def work():
            try:
                result = account.fetch()
            except Exception as exc:  # noqa: BLE001 - jeder Fehler soll im Fenster sichtbar werden
                GLib.idle_add(self._apply, account, None, exc)
            else:
                GLib.idle_add(self._apply, account, result, None)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, account: Account, result, exc):
        account.refreshing = False
        if exc is None:
            limits, plan = result
            account.apply_success(limits, time.time(), plan)
            if not self.demo:
                providers.save_cache(self.accounts)
        else:
            account.apply_error(exc, time.time())
        self._update_view()
        return False

    def _scan_activity(self):
        """Nutzung erkennen und lokal protokollierte Codex-Limits übernehmen."""
        now = self._last_scan = time.time()
        data_changed = False
        for account in self.accounts:
            account.activity = account.scan_activity()
            data_changed |= account.apply_live(account.live_limits(), now)
        if data_changed:
            self._update_view()
        else:
            self._reselect()  # Aktivität allein: Badges macht _tick, kein Neuaufbau

    def _update_view(self):
        self.shown = providers.choose_account(self.accounts, time.time())
        self.render()
        self._refresh_icon()

    def _reselect(self):
        """Symbol wechseln, wenn ein anderes Abo dran ist (z. B. 10-min-Fenster abgelaufen)."""
        shown = providers.choose_account(self.accounts, time.time())
        if shown is not self.shown:
            self.shown = shown
            self._refresh_icon()
            self._tick()

    def _refresh_icon(self):
        self._icon_at = time.time()
        try:
            self.sni.refresh_icon()
        except dbus.DBusException:
            pass  # Tray darf nie die App killen

    def tooltip_lines(self) -> tuple[str, str]:
        title = f"{APP_TITLE} · {self.shown.title}" if self.shown else APP_TITLE
        lines = [
            f"{'▸ ' if account is self.shown else ''}{account_summary(account)}"
            for account in self.accounts
        ]
        return title, "\n".join(lines)

    # --- UI -------------------------------------------------------------
    def _build_window(self):
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title(APP_TITLE)
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
        outer.set_size_request(360, -1)
        self.outer = outer

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        header.set_margin_start(16)
        header.set_margin_end(10)
        header.set_margin_top(10)
        header.set_margin_bottom(4)
        header.set_size_request(-1, 30)
        header.connect("button-press-event", self._start_drag)
        header.pack_start(_label(APP_TITLE, "limits-title"), True, True, 0)
        refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh_button.set_relief(Gtk.ReliefStyle.NONE)
        refresh_button.set_tooltip_text("Alle aktualisieren")
        refresh_button.connect("clicked", lambda *_: self.force_refresh())
        header.pack_end(refresh_button, False, False, 0)
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_tooltip_text("Schließen")
        close_button.connect("clicked", self._hide_window)
        header.pack_end(close_button, False, False, 0)
        outer.pack_start(header, False, False, 0)

        self.sections_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sections_box.set_margin_start(16)
        self.sections_box.set_margin_end(16)
        outer.pack_start(self.sections_box, False, False, 0)

        self.footer = _label(f"Automatisch alle {UI_REFRESH_SECONDS} s bei offenem Fenster", "footer")
        self.footer.set_margin_start(16)
        self.footer.set_margin_end(16)
        self.footer.set_margin_top(4)
        self.footer.set_margin_bottom(12)
        outer.pack_start(self.footer, False, False, 0)

        self.win.add(outer)

    def _add_section(self, account: Account):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        if self.sections:
            box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.pack_start(_label(account.title, "account-title"), True, True, 0)
        badge = _label(css="account-badge", xalign=1.0)
        head.pack_end(badge, False, False, 0)
        box.pack_start(head, False, False, 0)

        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        resets = []
        for limit in account.limits:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            top.pack_start(_label(limit.label, "limit-name"), True, True, 0)
            top.pack_end(_label(f"{limit.percent:.0f} %", "limit-value", 1.0), False, False, 0)
            bar = Gtk.ProgressBar()
            bar.set_fraction(min(limit.percent, 100.0) / 100.0)
            bar.get_style_context().add_class(_level(limit.percent))
            reset = _label(_reset_text(limit), "limit-reset")
            resets.append((limit, reset))
            for widget in (top, bar, reset):
                row.pack_start(widget, False, False, 0)
            rows.pack_start(row, False, False, 0)
        # Werte bleiben bei Fehlern sichtbar, aber erkennbar veraltet
        rows.set_opacity(0.55 if account.is_stale() else 1.0)
        box.pack_start(rows, False, False, 0)

        if account.error:
            error = _label(self._error_text(account), "hint")
            error.set_line_wrap(True)
            error.set_max_width_chars(44)
            box.pack_start(error, False, False, 0)
        status = _label(css="footer")
        box.pack_start(status, False, False, 0)

        self.sections_box.pack_start(box, False, False, 0)
        self.sections.append({"account": account, "badge": badge, "status": status, "resets": resets})

    @staticmethod
    def _error_text(account: Account) -> str:
        if account.rate_limited:
            return "Anfrage-Limit erreicht (HTTP 429) – automatische Pause bis zum nächsten Versuch."
        return account.error or "Unbekannter Fehler"

    def render(self):
        for child in self.sections_box.get_children():
            self.sections_box.remove(child)
        self.sections = []
        for account in self.accounts:
            self._add_section(account)
        self.sections_box.show_all()
        self._tick()
        if self.win.get_visible():
            self._fit(place=False)

    def _tick(self):
        """Countdowns, Aktivität und Statuszeilen aktualisieren."""
        if not self.win.get_visible():
            return
        now = time.time()
        for section in self.sections:
            account: Account = section["account"]
            for limit, label in section["resets"]:
                label.set_text(_reset_text(limit))
            badge = section["badge"]
            badge_ctx = badge.get_style_context()
            if account is self.shown:
                badge.set_text("● im Tray" + (" · aktiv" if account.is_active(now) else ""))
                badge_ctx.add_class("shown")
            else:
                badge_ctx.remove_class("shown")
                if account.is_active(now):
                    badge.set_text("aktiv")
                elif account.activity:
                    badge.set_text(f"genutzt vor {_duration(now - account.activity)}")
                else:
                    badge.set_text("")
            section["status"].set_text(self._status_text(account, now))

    @staticmethod
    def _status_text(account: Account, now: float) -> str:
        if account.refreshing:
            return "Aktualisiere …"
        parts = []
        if account.error and account.retry_after > now:
            parts.append(f"Nächster Versuch in {_duration(account.retry_after - now)}")
        if account.last_update:
            parts.append(
                f"{'Werte von' if account.error else 'Aktualisiert'} vor {_duration(now - account.last_update)}"
            )
        return " · ".join(parts)

    # --- Verhalten ------------------------------------------------------
    def _fit(self, place=True):
        """Größe vor dem Anzeigen bestimmen, damit kein Zwischenzustand flackert."""
        self.outer.show_all()
        natural = self.outer.get_preferred_size()[1]
        self._win_size = (max(360, natural.width), natural.height)
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
        now = time.time()
        for account in self.accounts:
            if account.manual_refresh_allowed(now):
                self.refresh(account)

    def _periodic(self):
        now = time.time()
        if now - self._last_scan >= ACTIVITY_SCAN_SECONDS:
            self._scan_activity()
        else:
            self._reselect()
        if now - self._icon_at >= 60:
            self._refresh_icon()  # Reset-Countdowns im Tooltip aktuell halten
        interval = UI_REFRESH_SECONDS if self.win.get_visible() else BG_REFRESH_SECONDS
        for account in self.accounts:
            if not account.refreshing and account.refresh_due(now, interval):
                self.refresh(account)
        self._tick()
        return True

    def quit(self):
        Gtk.main_quit()


def _cli_print(accounts: list[Account]) -> int:
    status = 0
    for account in accounts:
        try:
            limits, plan = account.fetch()
        except Exception as exc:  # noqa: BLE001
            kind = "Auth-Fehler" if isinstance(exc, AuthError) else "Fehler"
            print(f"{account.name}: {kind}: {exc}", file=sys.stderr)
            status = 1
            continue
        account.plan = plan
        print(account.title)
        for limit in limits:
            print(f"  {limit.label:22s} {limit.percent:6.1f} %  Reset {_local_time(limit.resets_at)}")
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-limits-tray", description="Zeigt die Limits der Claude- und Codex-Abos im Tray."
    )
    parser.add_argument("--demo", action="store_true", help="Beispielwerte, ohne Netzwerk")
    parser.add_argument(
        "--print", dest="print_only", action="store_true", help="Werte einmal auf der Konsole ausgeben"
    )
    args = parser.parse_args(argv)
    accounts = providers.demo_accounts() if args.demo else providers.discover_accounts()
    if args.print_only:
        return _cli_print(accounts)

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
    app = LimitsApp(bus, accounts, demo=args.demo)

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
