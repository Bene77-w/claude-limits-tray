"""Datenquellen der Tray-App: Claude- und Codex-Abos.

Ohne GTK/D-Bus, damit Abruf, Aktivitätserkennung und Auswahl testbar bleiben.
Tokens werden nur im Speicher gehalten und nie ausgegeben.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CLAUDE_CREDENTIALS = HOME / ".claude" / ".credentials.json"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CACHE_FILE = HOME / ".cache" / "claude-limits-tray" / "usage.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = "claude-limits-tray/1.2"

ACTIVE_SECONDS = 600            # so lange gilt ein Abo nach der letzten Nutzung als "in Benutzung"
NETWORK_RETRY_SECONDS = 60      # offline, DNS, Timeout
ERROR_RETRY_SECONDS = 180       # Serverfehler, kaputte Antwort
AUTH_RETRY_SECONDS = 300        # Token fehlt/abgelaufen (Dateiänderung löst früher aus)
RATE_LIMIT_MAX_SECONDS = 1800   # Obergrenze des Backoffs bei wiederholtem 429
TAIL_BYTES = 256 * 1024         # so viel vom Ende einer Sitzungsdatei wird gelesen

CLAUDE_EXPIRED_MSG = (
    "Token abgelaufen. Claude Code einmal starten (oder /login ausführen); "
    "die Anzeige aktualisiert sich danach von selbst."
)

# Reihenfolge und Beschriftung der Claude-Limits
CLAUDE_LIMIT_KEYS = [
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


def parse_reset(value):
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


def window_label(seconds: float | None) -> str:
    """Beschriftung eines Limit-Fensters aus seiner Länge."""
    if not seconds:
        return "Limit"
    hours = round(seconds / 3600)
    if hours == 168:
        return "Wochen-Limit"
    if hours < 48:
        return f"{hours}-Stunden-Limit"
    return f"{round(hours / 24)}-Tage-Limit"


def short_label(label: str) -> str:
    """Kurzform für den Tooltip: "5-Stunden-Limit" -> "5 h"."""
    label = label.split(" · ")[-1]
    return (
        label.replace("-Stunden-Limit", " h")
        .replace("-Tage-Limit", " T")
        .replace("Wochen-Limit", "Woche")
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen nicht folgen: urllib gäbe den Authorization-Header
    sonst auch an einen fremden Host weiter. Ein 3xx endet als HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urlopen(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def _get_json(url: str, headers: dict, timeout: float, expired_msg: str, forbidden_msg: str):
    req = urllib.request.Request(
        url, headers={**headers, "User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with _urlopen(req, timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError(expired_msg) from exc
        if exc.code == 403:
            raise AuthError(forbidden_msg) from exc
        if exc.code == 429:
            try:
                wait = int(float((exc.headers or {}).get("retry-after") or ERROR_RETRY_SECONDS))
            except (TypeError, ValueError):
                wait = ERROR_RETRY_SECONDS  # z. B. Retry-After als HTTP-Datum
            raise RateLimitError("Zu viele Anfragen (HTTP 429)", max(0, min(wait, RATE_LIMIT_MAX_SECONDS))) from exc
        raise RuntimeError(f"Serverfehler (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"Keine Verbindung: {exc.reason}") from exc
    except (OSError, http.client.HTTPException) as exc:  # Timeout, abgebrochene Antwort u. Ä.
        raise NetworkError(f"Keine Verbindung: {type(exc).__name__}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise RuntimeError("Antwort des Servers war kein gültiges JSON.") from None


def _read_json(path: Path, missing_msg: str):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise AuthError(missing_msg) from None
    except ValueError:
        # Der Client schreibt die Datei vermutlich gerade neu
        raise RuntimeError("Anmeldedaten gerade nicht lesbar, neuer Versuch folgt.") from None
    except OSError as exc:
        raise AuthError(f"Anmeldedaten nicht lesbar: {type(exc).__name__}") from None


def _tail(path: Path, size: int = TAIL_BYTES) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - size))
        return fh.read()


def _last_match(path: Path, regex: re.Pattern, skip=(), limit: int = 16 * TAIL_BYTES):
    """Letzter Treffer von `regex` in der Datei, rückwärts in Blöcken gelesen.

    Große Tool-Ausgaben am Sitzungsende verdrängen die letzte Modellzeile oft
    aus einem einzelnen Block; darum bis `limit` Bytes weiterlesen.
    """
    with open(path, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        start = end
        while start > 0 and end - start < limit:
            start = max(0, start - TAIL_BYTES)
            fh.seek(start)
            # Überlappung, damit ein Treffer an der Blockgrenze nicht verloren geht
            data = fh.read(min(end, start + TAIL_BYTES + 512) - start)
            hits = [m for m in regex.findall(data) if m not in skip]
            if hits:
                return hits[-1]
            end = start
    return None


def _newest(paths) -> tuple[float, Path] | None:
    best = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return best


def _mtime(path: Path | None) -> float | None:
    try:
        return path.stat().st_mtime if path else None
    except OSError:
        return None


# --------------------------------------------------------------------- Konten


class Account:
    """Ein Abo mit seinen zuletzt bekannten Limits und dem Abrufzustand."""

    def __init__(self, account_id: str, name: str, short: str):
        self.id = account_id
        self.name = name
        self.short = short  # Buchstabe im Tray-Symbol
        self.plan: str | None = None
        self.limits: list[Limit] = []
        self.last_update: float | None = None
        self.activity: float | None = None  # Zeitpunkt der letzten Nutzung
        self.error: str | None = None
        self.rate_limited = False
        self.rate_limit_streak = 0
        self.auth_failed = False
        self.cred_mtime: float | None = None
        self.retry_after = 0.0
        self.refreshing = False

    # --- von Unterklassen bereitgestellt -------------------------------
    def fetch(self) -> tuple[list[Limit], str | None]:
        raise NotImplementedError

    def credentials_path(self) -> Path | None:
        return None

    def scan_activity(self) -> float | None:
        return None

    def live_limits(self):
        """(Zeitpunkt, Limits, Plan) aus lokalen Protokollen, falls vorhanden."""
        return None

    # --- Zustand --------------------------------------------------------
    @property
    def title(self) -> str:
        return f"{self.name} · {self.plan.capitalize()}" if self.plan else self.name

    def max_percent(self) -> float | None:
        return max((limit.percent for limit in self.limits), default=None)

    def is_stale(self) -> bool:
        return bool(self.error and self.limits)

    def is_active(self, now: float) -> bool:
        return self.activity is not None and now - self.activity <= ACTIVE_SECONDS

    def apply_success(self, limits, now: float, plan: str | None = None):
        self.limits = limits
        self.plan = plan or self.plan
        self.last_update = now
        self.error = None
        self.rate_limited = self.auth_failed = False
        self.rate_limit_streak = 0
        self.retry_after = 0.0

    def apply_live(self, live, now: float) -> bool:
        """Lokal protokollierte Limits übernehmen, wenn sie neu und frisch sind.

        Alte Protokolle (z. B. von vor Tagen) zeigen längst zurückgesetzte
        Werte - die kommen dann nur noch über den Abruf.
        """
        if not live:
            return False
        ts, limits, plan = live
        if now - ts > ACTIVE_SECONDS or (self.last_update is not None and ts <= self.last_update):
            return False
        # Nur die Werte übernehmen: eine 429-Pause oder ein Auth-Fehler gilt
        # weiter für den Abruf, die Anzeige ist aber nicht mehr veraltet.
        self.limits = limits
        self.plan = plan or self.plan
        self.last_update = ts
        self.error = None
        return True

    def apply_error(self, exc: Exception, now: float):
        self.error = str(exc) or type(exc).__name__
        self.rate_limited = isinstance(exc, RateLimitError)
        self.rate_limit_streak = self.rate_limit_streak + 1 if self.rate_limited else 0
        self.auth_failed = isinstance(exc, AuthError)
        self.cred_mtime = _mtime(self.credentials_path())
        # Bei Fehlern (z. B. 429) nicht im Minutentakt weiter anfragen
        self.retry_after = now + retry_delay(exc, self.rate_limit_streak)

    def refresh_due(self, now: float, interval: float) -> bool:
        if self.auth_failed and _mtime(self.credentials_path()) != self.cred_mtime:
            return True  # Client hat den Token vermutlich erneuert
        if now < self.retry_after:
            return False
        if self.error or self.retry_after:
            return True  # Wartezeit nach dem Fehler ist abgelaufen
        return self.last_update is None or now - self.last_update >= interval

    def manual_refresh_allowed(self, now: float) -> bool:
        # Bei 429 würde jede weitere Anfrage die Sperre nur verlängern
        return not (self.rate_limited and now < self.retry_after)


# --- Claude ----------------------------------------------------------------


def load_claude_auth(path: Path = CLAUDE_CREDENTIALS) -> tuple[str, str | None]:
    """Nur der Claude-Code-Login zählt - andere Tokens (z. B. MCP) nie senden."""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env, None
    data = _read_json(path, "Keine Claude-Anmeldung gefunden (~/.claude/.credentials.json fehlt).")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise AuthError("Kein Claude-Login gefunden. Claude Code starten und /login ausführen.")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and _epoch_seconds(expires) < time.time():
        raise AuthError(CLAUDE_EXPIRED_MSG)
    plan = oauth.get("subscriptionType")
    return token, plan if isinstance(plan, str) else None


def parse_claude_usage(payload) -> list[Limit]:
    """`utilization` ist ein Prozentwert (0..100, über 100 möglich): 1.0 heißt 1 %."""
    data = payload.get("usage", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Unerwartete Antwort vom Server.")
    limits = []
    for key, label in CLAUDE_LIMIT_KEYS:
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            pct = float(entry["utilization"])
        except (KeyError, TypeError, ValueError):
            continue
        limits.append(Limit(key, label, max(0.0, pct), parse_reset(entry.get("resets_at"))))
    if not limits:
        raise RuntimeError("Antwort enthielt keine Limit-Daten.")
    return limits


_MODEL_RE = re.compile(rb'"model":\s*"([^"]+)"')


class ClaudeAccount(Account):
    def __init__(self, projects: Path = CLAUDE_PROJECTS, credentials: Path = CLAUDE_CREDENTIALS):
        super().__init__("claude", "Claude", "C")
        self.projects = projects
        self.credentials = credentials
        self._model_cache: dict[Path, tuple[float, str | None]] = {}

    def fetch(self, timeout: float = 15.0):
        token, plan = load_claude_auth(self.credentials)
        payload = _get_json(
            CLAUDE_USAGE_URL,
            {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
            timeout,
            CLAUDE_EXPIRED_MSG,
            "Zugriff verweigert (HTTP 403): Token ungültig oder ohne Berechtigung "
            "für Nutzungsdaten. In Claude Code neu anmelden (/login).",
        )
        return parse_claude_usage(payload), plan

    def credentials_path(self):
        return self.credentials

    def _last_model(self, path: Path, mtime: float) -> str | None:
        cached = self._model_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            found = _last_match(path, _MODEL_RE, skip=(b"<synthetic>",))
        except OSError:
            found = None
        model = found.decode(errors="replace") if found else None
        self._model_cache[path] = (mtime, model)
        return model

    def scan_activity(self, check: int = 50):
        """Letzte Claude-Code-Nutzung mit Claude-Modell.

        Sitzungen mit anderen Modellen (z. B. claude-dsh mit DeepSeek) zählen
        nicht, obwohl sie im selben Ordner liegen. Geprüft werden die neuesten
        `check` Sitzungen; das Ergebnis je Datei wird bis zur nächsten
        Änderung zwischengespeichert.
        """
        files = []
        for path in self.projects.glob("*/*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
        files.sort(reverse=True)
        for mtime, path in files[:check]:
            model = self._last_model(path, mtime)
            if model and model.startswith("claude"):
                return mtime
        return None


# --- Codex -----------------------------------------------------------------


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except (IndexError, ValueError, TypeError):
        return None


def load_codex_auth(home: Path, expired_msg: str) -> tuple[str, str]:
    data = _read_json(home / "auth.json", "Kein Codex-Login in diesem Profil.")
    tokens = data.get("tokens") if isinstance(data, dict) else None
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    account = tokens.get("account_id") if isinstance(tokens, dict) else None
    if not isinstance(access, str) or not access or not isinstance(account, str):
        raise AuthError("Kein ChatGPT-Login in diesem Profil (nur API-Key?).")
    exp = _jwt_exp(access)
    if exp is not None and exp < time.time():
        raise AuthError(expired_msg)
    return access, account


def _codex_limit(key: str, window, now: float, seconds_key: str, factor: float, reset_key: str):
    if not isinstance(window, dict):
        return None
    try:
        pct = float(window["used_percent"])
    except (KeyError, TypeError, ValueError):
        return None
    seconds = window.get(seconds_key)
    seconds = seconds * factor if isinstance(seconds, (int, float)) else None
    reset = parse_reset(window.get(reset_key))
    if reset is None and isinstance(window.get("reset_after_seconds"), (int, float)):
        reset = datetime.fromtimestamp(now + window["reset_after_seconds"], tz=timezone.utc)
    return Limit(key, window_label(seconds), max(0.0, pct), reset)


def parse_codex_usage(payload, now: float | None = None) -> tuple[list[Limit], str | None]:
    """Antwort von /wham/usage (wie `/status` in Codex)."""
    now = time.time() if now is None else now
    rate = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate, dict):
        raise RuntimeError("Antwort enthielt keine Limit-Daten.")
    limits = [
        limit
        for key in ("primary", "secondary")
        if (limit := _codex_limit(key, rate.get(f"{key}_window"), now, "limit_window_seconds", 1, "reset_at"))
    ]
    if not limits:
        raise RuntimeError("Antwort enthielt keine Limit-Daten.")
    plan = payload.get("plan_type")
    return limits, plan if isinstance(plan, str) else None


def parse_codex_rollout(line: bytes):
    """(Zeitpunkt, Limits, Plan) aus einer token_count-Zeile eines Codex-Rollouts."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    payload = entry.get("payload") if isinstance(entry, dict) else None
    rate = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(rate, dict) or rate.get("limit_id") not in (None, "codex"):
        return None
    ts = parse_reset(entry.get("timestamp"))
    if ts is None:
        return None
    limits = [
        limit
        for key in ("primary", "secondary")
        if (limit := _codex_limit(key, rate.get(key), ts.timestamp(), "window_minutes", 60, "resets_at"))
    ]
    if not limits:
        return None
    plan = rate.get("plan_type")
    return ts.timestamp(), limits, plan if isinstance(plan, str) else None


class CodexAccount(Account):
    def __init__(self, home: Path, short: str):
        super().__init__(f"codex:{home.name}", f"Codex {short}", short)
        self.home = home
        self.expired_msg = (
            f"Token abgelaufen. Codex in diesem Profil einmal starten "
            f"(codex-{short.lower()}); die Anzeige aktualisiert sich danach von selbst."
        )
        self._newest: tuple[float, Path] | None = None
        self._live_seen: tuple[float, Path] | None = None

    def fetch(self, timeout: float = 15.0):
        access, account = load_codex_auth(self.home, self.expired_msg)
        payload = _get_json(
            CODEX_USAGE_URL,
            {"Authorization": f"Bearer {access}", "chatgpt-account-id": account},
            timeout,
            self.expired_msg,
            "Zugriff verweigert (HTTP 403). Codex in diesem Profil neu anmelden.",
        )
        return parse_codex_usage(payload)

    def credentials_path(self):
        return self.home / "auth.json"

    def scan_activity(self):
        # Codex hängt bei jeder Antwort an die Rollout-Datei der Sitzung an
        self._newest = _newest((self.home / "sessions").glob("*/*/*/rollout-*.jsonl"))
        return self._newest[0] if self._newest else None

    def live_limits(self):
        """Neueste Limits aus der zuletzt geschriebenen Sitzung, ohne Netzwerk."""
        newest = self._newest
        if newest is None or newest == self._live_seen:
            return None
        self._live_seen = newest
        try:
            data = _tail(newest[1])
        except OSError:
            return None
        for line in reversed(data.splitlines()):
            if b'"rate_limits"' in line and (found := parse_codex_rollout(line)):
                return found
        return None


# ------------------------------------------------------------ Kontenauswahl


def discover_accounts(home: Path = HOME) -> list[Account]:
    """Claude plus jedes Codex-Profil (~/.codex = A, ~/.codex-b = B, ...)."""
    accounts: list[Account] = [ClaudeAccount()]
    used = set()
    for path in sorted(home.glob(".codex*")):
        if path.name == ".codex":
            short = "A"
        elif path.name.startswith(".codex-") and len(path.name) == 8 and path.name[-1].isalpha():
            short = path.name[-1].upper()  # ~/.codex-b = B; Backups wie .codex-old zählen nicht
        else:
            continue
        if short in used or not (path.is_dir() and (path / "auth.json").is_file()):
            continue
        used.add(short)
        accounts.append(CodexAccount(path, short))
    return accounts


def choose_account(accounts: list[Account], now: float) -> Account | None:
    """Welches Abo das Tray-Symbol zeigt.

    In Benutzung (Aktivität in den letzten ACTIVE_SECONDS) hat Vorrang; bei
    mehreren gleichzeitig das mit dem höchsten Verbrauch. Ist keins aktiv,
    das zuletzt genutzte.
    """
    with_data = [a for a in accounts if a.limits]
    active = [a for a in with_data if a.is_active(now)]
    if active:
        return max(active, key=lambda a: a.max_percent() or 0.0)
    used = [a for a in with_data if a.activity is not None]
    if used:
        return max(used, key=lambda a: a.activity)
    return with_data[0] if with_data else None


# -------------------------------------------------------------------- Cache


def save_cache(accounts: list[Account], path: Path = CACHE_FILE):
    payload = {
        "accounts": {
            a.id: {
                "ts": a.last_update,
                "plan": a.plan,
                "limits": [
                    {
                        "key": l.key,
                        "label": l.label,
                        "percent": l.percent,
                        "resets_at": l.resets_at.isoformat() if l.resets_at else None,
                    }
                    for l in a.limits
                ],
            }
            for a in accounts
            if a.limits
        }
    }
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)  # atomar: nie eine halb geschriebene Datei
    except OSError:
        pass


def restore_cache(accounts: list[Account], path: Path = CACHE_FILE):
    """Zuletzt geladene Werte übernehmen (überlebt Neustarts)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    entries = data.get("accounts")
    if entries is None and "limits" in data:
        entries = {"claude": data}  # Format bis Version 1.1 (nur Claude)
    if not isinstance(entries, dict):
        return
    for account in accounts:
        entry = entries.get(account.id)
        try:
            account.limits = [
                Limit(e["key"], e["label"], float(e["percent"]), parse_reset(e.get("resets_at")))
                for e in entry["limits"]
            ]
            account.last_update = float(entry.get("ts") or 0.0) or None
            account.plan = entry.get("plan")
        except (KeyError, TypeError, ValueError):
            continue


# --------------------------------------------------------------------- Demo


class DemoAccount(Account):
    def __init__(self, account_id, name, short, plan, percents, active_ago):
        super().__init__(account_id, name, short)
        self._plan = plan
        self._percents = percents
        self._active_ago = active_ago

    def fetch(self):
        now = datetime.now(timezone.utc)
        resets = [timedelta(hours=2, minutes=17), timedelta(days=3, hours=4)]
        labels = ["5-Stunden-Limit", "Wochen-Limit"]
        return [
            Limit(f"demo{i}", labels[i], pct, now + resets[i]) for i, pct in enumerate(self._percents)
        ], self._plan

    def scan_activity(self):
        return None if self._active_ago is None else time.time() - self._active_ago


def demo_accounts() -> list[Account]:
    return [
        DemoAccount("claude", "Claude", "C", "pro", [34, 61], 120),
        DemoAccount("codex:.codex", "Codex A", "A", "free", [12], None),
        DemoAccount("codex:.codex-b", "Codex B", "B", "plus", [72, 40], 30),
    ]
