#!/usr/bin/env python3
"""
CtrlWard — the blind-pick and live-draft tool as a downloadable Windows app,
with TRUE LIVE DRAFTING.

Double-click the .exe (or run `python app.py`) and it:
  1. Starts a small local server on 127.0.0.1 that serves the tool.
  2. Opens the tool in an app-style window (Edge/Chrome --app mode, else your
     default browser).
  3. Watches your League of Legends champ select through the local LCU API
     (GET /lol-champ-select/v1/session — the same read your own client shows
     you) and pushes every pick/ban into the tool's Live Draft mode the moment
     it happens. No tapping, no pasting, no link hand-offs.

READ-ONLY, permanently: this app never picks, hovers, locks, bans, accepts or
dodges anything. It only reads. Same design boundary as the bridge script.

Optional and off by default: a prototype in-game CS overlay that reads the
local player's OWN creep score from Riot's Live Client Data API on this
machine and shows CS and CS/min in a small always-on-top window while a
game runs. Display only — no other player's live stats, no advice.

No Riot API key. No third-party packages. Python 3.9+ standard library only.

Modes:
    python app.py            normal: connect to your League client
    python app.py --demo     simulate a champ select that fills in over time,
                             so you can watch the tool update live with no game
    python app.py --port N   serve on a fixed port instead of an automatic one
    python app.py --no-open  don't open the window (server only)
    python app.py --lockfile "X:\\path\\lockfile"   unusual League install path
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Paths (works both as a script and as a PyInstaller one-file .exe)
# ---------------------------------------------------------------------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def bundle_dir():
    # PyInstaller unpacks --add-data files to _MEIPASS at runtime.
    return getattr(sys, "_MEIPASS", app_dir())

def find_tool_html():
    for base in (app_dir(), bundle_dir()):
        candidate = os.path.join(base, "tool.html")
        if os.path.exists(candidate):
            return candidate
    return None

def find_icon():
    for base in (app_dir(), bundle_dir()):
        candidate = os.path.join(base, "ctrl_ward.ico")
        if os.path.exists(candidate):
            return candidate
    return None

CACHE_DIR = os.path.join(app_dir(), "asset_cache")

# ---------------------------------------------------------------------------
# Updates via GitHub Releases
#
# The app checks the repo's latest Release once per launch:
#   - a newer tool.html asset is downloaded automatically and placed next to
#     the app (find_tool_html prefers it over the bundled copy), applying on
#     the next launch;
#   - a newer .exe is staged for a self-update: the asset is downloaded
#     into an "update" folder beside the app, verified against the sha256
#     digest GitHub records for it, and swapped in by a small batch
#     helper at the NEXT launch (the old build is kept as
#     CtrlWard.exe.bak). See the self-update section further down.
#     The banner still appears as the fallback (e.g. from source).
# Set the repo in update_config.json next to the app: {"repo": "owner/name"}
# or bake it into DEFAULT_UPDATE_REPO at build time. Empty = checks disabled.
# ---------------------------------------------------------------------------

APP_VERSION = "1.3.2"
DEFAULT_UPDATE_REPO = "lockwood-labs/CtrlWard"

def load_update_repo():
    cfg = os.path.join(app_dir(), "update_config.json")
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            repo = (json.load(f) or {}).get("repo", "").strip()
        if repo:
            return repo
    except Exception:
        pass
    return DEFAULT_UPDATE_REPO

def _ver_tuple(text):
    parts = []
    for piece in str(text).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)

def is_newer(remote, local):
    return _ver_tuple(remote) > _ver_tuple(local)

def current_tool_version():
    """Version of the tool page this install actually serves: the
    tool_version.txt written beside the app when an update was
    downloaded, else the version bundled with this build. The update
    check must judge "newer" against THIS, not APP_VERSION — tool-only
    releases reuse the same .exe, so the release tag beats the .exe's
    version on every launch and the "restart to apply" banner would
    come back forever (the v1.1.4 bug)."""
    try:
        with open(os.path.join(app_dir(), "tool_version.txt"),
                  "r", encoding="utf-8") as f:
            version = f.read().strip()
        if version:
            return version
    except Exception:
        pass
    return APP_VERSION

# ---------------------------------------------------------------------------
# Robust HTTP GET (the Data Dragon CDN sometimes drops Python clients
# mid-transfer; read incrementally and retry)
# ---------------------------------------------------------------------------

def http_get_bytes(url, timeout=20):
    """GET with resume: some networks/CDN edges drop Python clients
    mid-transfer (seen truncating at exactly 16 KB). If a response comes
    back short, keep re-requesting the missing tail with Range headers
    until the body is complete."""
    base_headers = {"User-Agent": "CtrlWard/1.0",
                    "Accept-Encoding": "identity",
                    "Connection": "close"}
    data = b""
    total = None
    ctype = None
    last = None
    for _round in range(10):
        headers = dict(base_headers)
        if data:
            headers["Range"] = f"bytes={len(data)}-"
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                chunks = []
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                fetched = b"".join(chunks)
                ctype = r.headers.get_content_type()
                if r.status == 206:
                    cr = r.headers.get("Content-Range") or ""
                    if "/" in cr:
                        total = int(cr.rsplit("/", 1)[1])
                    data += fetched
                else:  # 200: full body (server may have ignored our Range)
                    cl = r.headers.get("Content-Length")
                    total = int(cl) if cl else None
                    data = fetched
        except Exception as exc:
            last = exc
            time.sleep(0.6)
            continue
        if total is None or len(data) >= total:
            return data, ctype
        if not fetched:
            time.sleep(0.6)
    raise IOError(f"incomplete download: {len(data)} of {total} bytes ({last})")

def http_get_json(url, timeout=20):
    data, _ = http_get_bytes(url, timeout=timeout)
    return json.loads(data.decode("utf-8"))

# ---------------------------------------------------------------------------
# Champion ID -> name, from Riot's Data Dragon (no key required)
# ---------------------------------------------------------------------------

def fetch_champion_names():
    version = http_get_json(
        "https://ddragon.leagueoflegends.com/api/versions.json", timeout=15)[0]
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    data = http_get_json(url, timeout=30)["data"]
    return version, {int(v["key"]): v["name"] for v in data.values()}

# ---------------------------------------------------------------------------
# LCU connection (lockfile -> port + password) — read-only GETs only
# ---------------------------------------------------------------------------

DEFAULT_LOCKFILE_PATHS = [
    r"C:\Riot Games\League of Legends\lockfile",
    r"D:\Riot Games\League of Legends\lockfile",
    r"E:\Riot Games\League of Legends\lockfile",
    r"C:\Program Files\Riot Games\League of Legends\lockfile",
    r"C:\Program Files (x86)\Riot Games\League of Legends\lockfile",
]

def read_lockfile(path=None):
    """Return (protocol, port, password) or None if no client lockfile found."""
    candidates = [path] if path else DEFAULT_LOCKFILE_PATHS
    for candidate in candidates:
        if not candidate:
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                parts = f.read().strip().split(":")
            return parts[4], int(parts[2]), parts[3]
        except (OSError, IndexError, ValueError):
            continue
    return None

def lcu_get(protocol, port, password, path):
    url = f"{protocol}://127.0.0.1:{port}{path}"
    token = base64.b64encode(f"riot:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    ctx = ssl._create_unverified_context()  # LCU uses a self-signed cert on localhost
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.load(r)

# ---------------------------------------------------------------------------
# Champ-select session -> draft -> tool parameters
# ---------------------------------------------------------------------------

POSITION_TO_ROLE = {
    "top": "top", "jungle": "jungle", "middle": "middle",
    "bottom": "adc", "utility": "support", "": None, None: None,
}

# Lobby display roles (Scout tab): LCU assignedPosition -> short label.
POSITION_TO_LOBBY_ROLE = {
    "top": "Top", "jungle": "Jungle", "middle": "Mid",
    "bottom": "ADC", "utility": "Support",
}

def lobby_role(assigned):
    return POSITION_TO_LOBBY_ROLE.get((assigned or "").lower(), "")

def _summoner_identity(obj):
    """Best-effort {name, tag, puuid} from an LCU summoner object."""
    obj = obj or {}
    name = ""
    for field in ("displayName", "gameName", "username", "name",
                  "internalName"):
        if obj.get(field):
            name = obj[field]
            break
    return {"name": name, "tag": obj.get("tagLine") or None,
            "puuid": obj.get("puuid") or None}

# Summoner identities don't change mid-session; cache for the app's life.
_SUMMONER_CACHE = {}
_ME_CACHE = {"ok": False, "value": None}

def resolve_summoner(creds, summoner_id):
    """Resolve one summonerId to {name, tag, puuid} via the LCU,
    cached per summonerId for the app's lifetime."""
    if not summoner_id:
        return {"name": "", "tag": None, "puuid": None}
    if summoner_id in _SUMMONER_CACHE:
        return _SUMMONER_CACHE[summoner_id]
    try:
        obj = lcu_get(creds[0], creds[1], creds[2],
                      f"/lol-summoner/v1/summoners/{summoner_id}")
        ident = _summoner_identity(obj)
        _SUMMONER_CACHE[summoner_id] = ident
        return ident
    except Exception:
        return {"name": "", "tag": None, "puuid": None}

def fetch_current_summoner(creds):
    """The local player's own {name, tag, puuid}, or None while the
    client is unreachable. Cached once it succeeds; retried on later
    polls until then."""
    if _ME_CACHE["ok"]:
        return _ME_CACHE["value"]
    try:
        obj = lcu_get(creds[0], creds[1], creds[2],
                      "/lol-summoner/v1/current-summoner")
        ident = _summoner_identity(obj)
        if ident["name"] or ident["puuid"]:
            _ME_CACHE.update({"ok": True, "value": ident})
            return ident
    except Exception:
        pass
    return None

def build_lobby(session, champ_names, creds):
    """Scout-tab lobby state from a champ-select session: the local
    player's team with resolved identities, plus 'me'."""
    local_cell = session.get("localPlayerCellId")
    teammates = []
    for member in session.get("myTeam", []) or []:
        ident = resolve_summoner(creds, member.get("summonerId"))
        champ_id = (member.get("championId")
                    or member.get("championPickIntent") or 0)
        teammates.append({
            "name": ident["name"],
            "tag": ident["tag"],
            "puuid": ident["puuid"],
            "role": lobby_role(member.get("assignedPosition")),
            "champion": champ_names.get(champ_id, "") if champ_id else "",
            "isSelf": member.get("cellId") == local_cell,
        })
    return {"inChampSelect": True, "teammates": teammates,
            "me": fetch_current_summoner(creds)}

def empty_lobby(me=None):
    return {"inChampSelect": False, "teammates": [], "me": me}

def _names(ids, champ_names):
    return [champ_names[i] for i in ids if i and i in champ_names]

QUEUE_LABELS = {420: "ranked", 440: "ranked", 400: "normal", 430: "normal",
                450: "aram"}

def queue_label(session):
    """Best-effort short queue label. The champ-select session rarely
    carries a queueId; 'custom' is knowable, the rest stays 'unknown'
    rather than guessed."""
    if session.get("isCustomGame"):
        return "custom"
    qid = session.get("queueId")
    if qid is None and isinstance(session.get("gameConfig"), dict):
        qid = session["gameConfig"].get("queueId")
    try:
        return QUEUE_LABELS.get(int(qid), "unknown")
    except (TypeError, ValueError):
        return "unknown"

def parse_session(session, champ_names):
    local_cell = session.get("localPlayerCellId")
    my_team = session.get("myTeam", [])
    their_team = session.get("theirTeam", [])

    role = None
    my_champ_id = 0
    for member in my_team:
        if member.get("cellId") == local_cell:
            role = POSITION_TO_ROLE.get((member.get("assignedPosition") or "").lower())
            my_champ_id = member.get("championId", 0) or 0

    # "Locked" means the local player's pick action completed. The cell's
    # championId alone also reflects hovers mid-selection, so prefer the
    # actions record when the session carries one (real sessions do;
    # simulated ones may not — there the cell value is the only signal).
    actions = session.get("actions") or []
    if actions:
        locked_id = 0
        for group in actions:
            for action in group:
                if (action.get("type") == "pick"
                        and action.get("actorCellId") == local_cell
                        and action.get("completed")
                        and action.get("championId")):
                    locked_id = action["championId"]
        my_champ_id = locked_id
    my_champ = champ_names.get(my_champ_id) if my_champ_id else None

    ally = _names([m.get("championId", 0) for m in my_team
                   if m.get("cellId") != local_cell], champ_names)
    enemy = _names([m.get("championId", 0) for m in their_team], champ_names)

    bans_obj = session.get("bans") or {}
    ban_ids = list(bans_obj.get("myTeamBans", [])) + list(bans_obj.get("theirTeamBans", []))
    if not ban_ids:
        for group in actions:
            for action in group:
                if action.get("type") == "ban" and action.get("completed"):
                    ban_ids.append(action.get("championId", 0))
    bans = _names(ban_ids, champ_names)

    return {"role": role, "ally": ally, "enemy": enemy, "bans": bans,
            "myChamp": my_champ, "myChampId": my_champ_id,
            "queue": queue_label(session)}

def draft_params(draft):
    q = ["live=1"]
    if draft.get("role"):
        q.append("role=" + draft["role"])
    for key in ("ally", "enemy", "bans"):
        if draft.get(key):
            joined = ",".join(urllib.parse.quote(n, safe="") for n in draft[key])
            q.append(f"{key}={joined}")
    return "&".join(q)

# ---------------------------------------------------------------------------
# Shared live state (poller thread writes, HTTP handlers read)
# ---------------------------------------------------------------------------

STATE = {
    "connected": False,       # League client lockfile found / LCU reachable
    "in_champ_select": False,
    "params": "",             # tool-format draft params, "" when no draft
    "signature": "",          # hash of params, changes when the draft changes
    "draft": None,
    "lobby": {"inChampSelect": False, "teammates": [], "me": None},
    "message": "Starting…",
    "overlay": {"enabled": False, "inGame": False, "cs": None,
                "csPerMin": None, "gameTimeSec": None},
    "app_version": APP_VERSION,
    "update": None,           # {version, url, tool_downloaded} when a newer release exists
    "appUpdate": None,        # {available: v} / {pendingRestart: v} — exe self-update state
}
STATE_LOCK = threading.Lock()

def set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)
        if "params" in kw:
            STATE["signature"] = hashlib.sha1(kw["params"].encode()).hexdigest()[:12]

def get_state():
    with STATE_LOCK:
        return dict(STATE)

# ---------------------------------------------------------------------------
# Anonymous draft telemetry (opt-in, OFF by default)
#
# If report_config.json sits beside the app ({"endpoint", "report_key"})
# AND the user ticks "Share anonymous draft data" in the app window, the
# app sends ONE record per completed draft — date, role, queue, champion
# names, the Top 3 it showed, and later the game's result — to that
# endpoint. No summoner names, no Riot IDs, no match IDs, and no
# timestamps finer than the day ever leave this PC. The player's PUUID is
# used only locally, to find their own finished game in their own
# client's match history when filling in the win/loss. With no
# report_config.json the feature does not exist; with one, it is still
# off until the user opts in.
# ---------------------------------------------------------------------------

SETTINGS_PATH = os.path.join(app_dir(), "ctrlward_settings.json")
PENDING_PATH = os.path.join(app_dir(), "ctrlward_pending.json")
_FILE_LOCK = threading.Lock()
_LCU_ARGS = {"lockfile": None}  # filled in by main()

def load_report_config():
    for base in (app_dir(), bundle_dir()):
        path = os.path.join(base, "report_config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            endpoint = (cfg.get("endpoint") or "").strip()
            key = (cfg.get("report_key") or "").strip()
            if endpoint and key:
                return {"endpoint": endpoint.rstrip("/"), "report_key": key}
        except Exception:
            continue
    return None

REPORT_CONFIG = load_report_config()

def _read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

def load_settings():
    settings = _read_json_file(SETTINGS_PATH, {})
    if not isinstance(settings, dict):
        settings = {}
    settings.setdefault("telemetry_enabled", False)
    settings.setdefault("overlay_enabled", False)
    if not settings.get("install_id"):
        settings["install_id"] = str(uuid.uuid4())
        _write_json_file(SETTINGS_PATH, settings)
    return settings

SETTINGS = load_settings()

def telemetry_available():
    return REPORT_CONFIG is not None

def telemetry_enabled():
    return bool(REPORT_CONFIG) and bool(SETTINGS.get("telemetry_enabled"))

def set_telemetry_enabled(on):
    SETTINGS["telemetry_enabled"] = bool(on)
    _write_json_file(SETTINGS_PATH, SETTINGS)

def overlay_enabled():
    return bool(SETTINGS.get("overlay_enabled"))

def set_overlay_enabled(on):
    SETTINGS["overlay_enabled"] = bool(on)
    _write_json_file(SETTINGS_PATH, SETTINGS)

def telemetry_post(path, payload):
    cfg = REPORT_CONFIG
    if not cfg:
        return False
    try:
        req = urllib.request.Request(
            cfg["endpoint"] + path,
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Report-Key": cfg["report_key"],
                     "User-Agent": "CtrlWard/" + APP_VERSION})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:
        return False

# --- Scout-tab player lookup proxy ---
#
# The page asks THIS engine for a player lookup; the engine relays it
# to the telemetry Worker's GET /lookup route with the report key.
# The key never reaches the page — the page only ever sees the
# Worker's JSON body, forwarded verbatim (error bodies included, so
# the page renders one uniform shape). Demo mode serves no lookups.

DEMO_MODE = {"on": False}  # set by main(); --demo disables lookups

def lookup_proxy(query):
    """Relay a lookup to the Worker. Returns response body bytes."""
    if DEMO_MODE["on"] or not REPORT_CONFIG:
        return json.dumps({"error": "not_configured"}).encode()
    params = urllib.parse.parse_qs(query or "")
    fwd = {}
    for key in ("puuid", "name", "tag"):
        values = params.get(key)
        if values and values[0]:
            fwd[key] = values[0]
    fwd["platform"] = (params.get("platform") or [""])[0] or "na1"
    url = (REPORT_CONFIG["endpoint"] + "/lookup?"
           + urllib.parse.urlencode(fwd))
    req = urllib.request.Request(url, headers={
        "X-Report-Key": REPORT_CONFIG["report_key"],
        "User-Agent": "CtrlWard/" + APP_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
            if body:
                return body
        except Exception:
            pass
        return json.dumps({"error": "lookup_unreachable"}).encode()
    except Exception:
        return json.dumps({"error": "lookup_unreachable"}).encode()

# ---------------------------------------------------------------------------
# In-game CS overlay (prototype, opt-in, OFF by default)
#
# When the user ticks "In-game CS overlay (prototype)" in the app
# window, a background poller reads Riot's Live Client Data API on
# THIS machine (https://127.0.0.1:2999/liveclientdata — the loopback
# interface broadcast tools use; its certificate is self-signed, so
# an unverified SSL context is correct here) once a second and
# publishes ONLY the local player's own numbers: creep score,
# CS/min, and the game clock. No other player's live stats are read
# into state, displayed, or logged — the scoreboard reveals enemy
# numbers only when you look, and this overlay does not change that.
# A tiny always-on-top window (Windows builds) paints the numbers;
# it is display-only: no pace warnings, no verdicts, no colours that
# imply good or bad. The live path has no fallback data: when the
# endpoint is silent the state honestly says "not in game". The one
# simulated feed lives behind --demo (demo_overlay_sample below),
# exactly like the demo champ select.
# ---------------------------------------------------------------------------

LIVE_CLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
OVERLAY_MIN_RATE_SECONDS = 30.0   # a CS/min over the walk to lane is noise

_OVERLAY_LIVE = {"inGame": False, "cs": None, "csPerMin": None,
                 "gameTimeSec": None}
_OVERLAY_LIVE_LOCK = threading.Lock()
_DEMO_OVERLAY_START = {"at": None}

def compute_cs_per_min(creep_score, game_time):
    """CS/min for the overlay: creep score over elapsed game minutes.
    None until the game is at least 30 seconds old."""
    if game_time is None or game_time < OVERLAY_MIN_RATE_SECONDS:
        return None
    return round(creep_score / (game_time / 60.0), 1)

def format_game_clock(game_time):
    total = int(game_time or 0)
    return f"{total // 60}:{total % 60:02d}"

def _identity_full_ids(obj):
    out = set()
    for field in ("riotId", "summonerName"):
        value = (obj or {}).get(field)
        if value:
            out.add(str(value).strip().lower())
    return out

def _identity_game_name(obj):
    value = (obj or {}).get("riotIdGameName")
    return str(value).strip().lower() if value else ""

def match_local_player(all_players, active):
    """The local player's own entry in allPlayers, identified via the
    activeplayer response. Full Riot IDs (riotId / summonerName) are
    matched first; the bare game name (riotIdGameName) is accepted
    only when exactly one entry carries it — game names alone are
    not unique. None when the local player can't be identified with
    confidence."""
    full_ids = _identity_full_ids(active)
    game_name = _identity_game_name(active)
    entries = [e for e in (all_players or []) if isinstance(e, dict)]
    for entry in entries:
        if _identity_full_ids(entry) & full_ids:
            return entry
    if game_name:
        named = [e for e in entries
                 if _identity_game_name(e) == game_name]
        if len(named) == 1:
            return named[0]
    return None

def overlay_sample_from(all_data, active):
    """The overlay sample from Live Client Data payloads, or None
    when the local player can't be identified. Reads ONLY the
    matched player's scores.creepScore — nothing from any other
    player's entry is copied, returned, or stored."""
    if not isinstance(all_data, dict):
        return None
    game_time = (all_data.get("gameData") or {}).get("gameTime")
    if not isinstance(game_time, (int, float)):
        return None
    entry = match_local_player(all_data.get("allPlayers"), active)
    if entry is None:
        return None
    creep = (entry.get("scores") or {}).get("creepScore")
    if not isinstance(creep, (int, float)):
        return None
    creep = int(creep)
    game_time = float(game_time)
    return {"cs": creep,
            "csPerMin": compute_cs_per_min(creep, game_time),
            "gameTimeSec": game_time}

def _lcd_get(path, timeout=1.5):
    req = urllib.request.Request(
        LIVE_CLIENT_BASE + path,
        headers={"User-Agent": "CtrlWard/" + APP_VERSION})
    ctx = ssl._create_unverified_context()  # self-signed loopback cert
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return json.load(r)

def fetch_live_overlay_sample():
    """One live read of the local player's CS state. Raises when the
    endpoint is unreachable (not in a game) — callers treat every
    failure as "not in game", never as an error worth showing."""
    active = _lcd_get("/activeplayer")
    all_data = _lcd_get("/allgamedata")
    return overlay_sample_from(all_data, active)

def demo_overlay_sample(elapsed):
    """DEMO ONLY — the simulated game feed behind --demo: game time
    advancing from zero, CS accruing at a plausible 7.3/min. This is
    the only fabricated game data in the app; it exists so the
    overlay state can be exercised without League, and the live path
    never calls it (the same rule as the demo champ select)."""
    game_time = max(0.0, min(float(elapsed), 3600.0))
    creep = int(7.3 * (game_time / 60.0))
    return {"cs": creep,
            "csPerMin": compute_cs_per_min(creep, game_time),
            "gameTimeSec": game_time}

def _publish_overlay():
    with _OVERLAY_LIVE_LOCK:
        live = dict(_OVERLAY_LIVE)
    set_state(overlay={"enabled": overlay_enabled(),
                       "inGame": live["inGame"], "cs": live["cs"],
                       "csPerMin": live["csPerMin"],
                       "gameTimeSec": live["gameTimeSec"]})

def _set_overlay_live(sample):
    with _OVERLAY_LIVE_LOCK:
        if sample is None:
            _OVERLAY_LIVE.update({"inGame": False, "cs": None,
                                  "csPerMin": None, "gameTimeSec": None})
        else:
            _OVERLAY_LIVE.update({"inGame": True, "cs": sample["cs"],
                                  "csPerMin": sample["csPerMin"],
                                  "gameTimeSec": sample["gameTimeSec"]})
    _publish_overlay()

def _overlay_poll_once():
    """One overlay poll step. With the toggle off there is no network
    traffic and no window — the published state simply says so."""
    if not overlay_enabled():
        _set_overlay_live(None)
        return
    if DEMO_MODE["on"]:
        start = _DEMO_OVERLAY_START["at"]
        elapsed = time.time() - start if start else 0.0
        _set_overlay_live(demo_overlay_sample(elapsed))
        return
    try:
        sample = fetch_live_overlay_sample()
    except Exception:
        sample = None  # endpoint silent = not in a game; not an error
    _set_overlay_live(sample)

def overlay_poller():
    """Background overlay poller (daemon thread). Cheap when idle:
    with the toggle off it only keeps the published flag honest."""
    _DEMO_OVERLAY_START["at"] = time.time()
    if DEMO_MODE["on"]:
        print("[demo] Simulated CS overlay feed running (demo only — "
              "the live overlay reads the real game).")
    while True:
        try:
            _overlay_poll_once()
        except Exception:
            pass
        time.sleep(1.0)

# --- The overlay window: a thin shell over the published overlay
# --- state. Every decision (enabled? in a game? which numbers?) is
# --- made by the poller above; this only paints. Windows only, and
# --- every step is guarded so a missing or broken tkinter can never
# --- take the app down with it.

OVERLAY_CHROMA = "#010203"  # chroma key: this exact colour turns transparent

def _overlay_click_through(root):
    """Best-effort click-through + no-activate via the Win32 extended
    window styles. Any failure just leaves a normal (still tiny,
    still harmless) always-on-top window."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(
            hwnd, -20,
            style | 0x00080000 | 0x00000020 | 0x08000000)
        # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    except Exception:
        pass

def overlay_window_loop():
    """Own the tkinter overlay window on its own thread (every Tk
    call happens here). No-ops off Windows and without tkinter."""
    if sys.platform != "win32":
        return
    try:
        import tkinter as tk
    except Exception:
        return  # no tk in this build — the feature disables itself
    try:
        root = tk.Tk()
        root.title("CtrlWard CS overlay")
        root.overrideredirect(True)
        root.configure(bg=OVERLAY_CHROMA)
        try:
            root.attributes("-transparentcolor", OVERLAY_CHROMA)
        except Exception:
            pass
        root.attributes("-topmost", True)
        main_var = tk.StringVar(value="")
        rate_var = tk.StringVar(value="")
        clock_var = tk.StringVar(value="")
        frame = tk.Frame(root, bg=OVERLAY_CHROMA)
        frame.pack(padx=6, pady=3)
        tk.Label(frame, textvariable=main_var, bg=OVERLAY_CHROMA,
                 fg="#f5eeda",
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(frame, textvariable=rate_var, bg=OVERLAY_CHROMA,
                 fg="#ff4655",
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(root, textvariable=clock_var, bg=OVERLAY_CHROMA,
                 fg="#cbb8c4", font=("Segoe UI", 9)).pack(anchor="e",
                                                         padx=6)
        root.withdraw()
        root.update_idletasks()
        _overlay_click_through(root)
        shown = {"on": False}

        def refresh():
            try:
                snap = get_state().get("overlay") or {}
                if (snap.get("enabled") and snap.get("inGame")
                        and snap.get("cs") is not None):
                    main_var.set(f"CS {snap['cs']}")
                    rate = snap.get("csPerMin")
                    rate_var.set(f" · {rate:.1f}/min"
                                 if rate is not None else "")
                    clock_var.set("@ " + format_game_clock(
                        snap.get("gameTimeSec")))
                    if not shown["on"]:
                        shown["on"] = True
                        root.deiconify()
                        root.update_idletasks()
                        width = root.winfo_reqwidth()
                        root.geometry(
                            f"+{root.winfo_screenwidth() - width - 18}+18")
                elif shown["on"]:
                    shown["on"] = False
                    root.withdraw()
            except Exception:
                pass
            root.after(250, refresh)

        root.after(250, refresh)
        root.mainloop()
    except Exception:
        return

# --- Top 3 snapshots: the page reports its current recommendations to
# --- this local server; they are kept only if they describe the draft
# --- the LCU poller currently sees (signature check below).

TOP3_STATE = {"signature": None, "top3": [], "at": 0.0}
TOP3_LOCK = threading.Lock()

def draft_signature(draft):
    """Canonical draft identity. The page's ally list can include the
    user's own pick (the 5th ally slot doubles as it); the LCU ally list
    excludes the local player — the signature always folds myChamp into
    the ally side so both views of the same draft sign identically."""
    ally = list(draft.get("ally") or [])
    if draft.get("myChamp"):
        ally.append(draft["myChamp"])
    canon = {"role": draft.get("role"), "ally": sorted(ally),
             "enemy": sorted(draft.get("enemy") or []),
             "bans": sorted(draft.get("bans") or [])}
    return hashlib.sha1(
        json.dumps(canon, sort_keys=True).encode()).hexdigest()[:16]

def top3_matches_draft(page, draft):
    """True when the page's trays describe THIS draft: page ally equals
    the LCU ally list, or that list plus the user's own pick; enemy
    lists equal. Stops a stale snapshot from a previous draft (or a
    hand-edited one) being attached to this draft's record."""
    if not draft:
        return False
    ally_page = sorted(page.get("ally") or [])
    ally_lcu = sorted(draft.get("ally") or [])
    ally_full = sorted(ally_lcu + ([draft["myChamp"]]
                                    if draft.get("myChamp") else []))
    if ally_page != ally_lcu and ally_page != ally_full:
        return False
    if sorted(page.get("enemy") or []) != sorted(draft.get("enemy") or []):
        return False
    return True

def store_top3(page):
    draft = get_state().get("draft")
    if not top3_matches_draft(page, draft):
        return False
    names = [n for n in (page.get("top3") or [])
             if isinstance(n, str) and n][:3]
    with TOP3_LOCK:
        TOP3_STATE.update({"signature": draft_signature(draft),
                           "top3": names, "at": time.time()})
    return True

def top3_for_draft(draft):
    with TOP3_LOCK:
        if TOP3_STATE["signature"] != draft_signature(draft):
            return []
        if time.time() - TOP3_STATE["at"] > 1800:
            return []
        return list(TOP3_STATE["top3"])

# --- Completed-draft records + outcome backfill ---

def load_pending():
    data = _read_json_file(PENDING_PATH, [])
    return data if isinstance(data, list) else []

def save_pending(entries):
    _write_json_file(PENDING_PATH, entries)

def flush_pending_reports():
    """POST any records not yet accepted by the endpoint; returns how
    many are still outstanding. Safe to call often."""
    with _FILE_LOCK:
        entries = load_pending()
        changed = False
        for entry in entries:
            if not entry.get("reported"):
                if telemetry_post("/report", entry["record"]):
                    entry["reported"] = True
                    changed = True
        if changed:
            save_pending(entries)
        return sum(1 for e in entries if not e.get("reported"))

def on_draft_ended(draft):
    """A champ-select session just ended. If the user had locked a
    champion and opted in, report the draft and queue its outcome."""
    if not draft or not draft.get("myChamp"):
        return
    if not telemetry_enabled():
        return
    role = {"middle": "mid"}.get(draft.get("role"), draft.get("role"))
    if role not in ("top", "jungle", "mid", "adc", "support"):
        return  # no assigned role (e.g. ARAM shuffle) — nothing to file
    top3 = top3_for_draft(draft)
    followed = None
    if top3:
        followed = 1 if draft["myChamp"] in top3 else 0
    record = {
        "record_id": str(uuid.uuid4()),
        "install_id": SETTINGS.get("install_id"),
        "date": time.strftime("%Y-%m-%d"),
        "role": role,
        "queue": draft.get("queue") or "unknown",
        "my_champ": draft["myChamp"],
        "ally": draft.get("ally") or [],
        "enemy": draft.get("enemy") or [],
        "bans": draft.get("bans") or [],
        "top3": top3,
        "followed": followed,
        "result": None,
    }
    entry = {"record_id": record["record_id"],
             "my_champ_id": draft.get("myChampId") or 0,
             "my_champ": draft["myChamp"],
             "draft_end_ms": int(time.time() * 1000),
             "reported": False,
             "record": record}
    with _FILE_LOCK:
        entries = load_pending()
        entries.append(entry)
        save_pending(entries)
    print("Telemetry: draft recorded for outcome tracking.")
    flush_pending_reports()

def _local_participant(game, puuid):
    wanted = set()
    for ident in game.get("participantIdentities", []) or []:
        player = ident.get("player") or {}
        if player.get("puuid") == puuid:
            wanted.add(ident.get("participantId"))
    for part in game.get("participants", []) or []:
        if part.get("participantId") in wanted:
            return part
    return None

def match_result_for_entry(entry, games, puuid, now_ms, used_game_ids):
    """Resolve one pending draft against match history. The game must
    have been created after the draft ended and within 12 hours, and the
    local player must be on the champion they locked. No matching game
    within 24 hours of the draft means the queue was dodged/abandoned."""
    start = entry["draft_end_ms"]
    end = start + 12 * 3600 * 1000
    candidates = []
    for game in games:
        created = game.get("gameCreation") or 0
        if not (start < created < end):
            continue
        if game.get("gameId") in used_game_ids:
            continue
        part = _local_participant(game, puuid)
        if part and part.get("championId") == entry["my_champ_id"]:
            candidates.append((created, game, part))
    if candidates:
        candidates.sort(key=lambda t: t[0])
        _created, game, part = candidates[0]
        used_game_ids.add(game.get("gameId"))
        if (game.get("gameDuration") or 0) < 240:
            return "remake"
        return "win" if (part.get("stats") or {}).get("win") else "loss"
    if now_ms - start > 24 * 3600 * 1000:
        return "unknown"
    return None

def backfill_once():
    """One outcome-resolution pass: post outstanding reports, then try
    to fill in results for reported drafts from the local match history.
    Every failure leaves the entries pending for a later pass."""
    if not telemetry_enabled():
        return
    flush_pending_reports()
    entries = load_pending()
    if not any(e.get("reported") for e in entries):
        return
    creds = read_lockfile(_LCU_ARGS["lockfile"])
    if not creds:
        return
    try:
        summoner = lcu_get(creds[0], creds[1], creds[2],
                           "/lol-summoner/v1/current-summoner")
        puuid = (summoner or {}).get("puuid")
        if not puuid:
            return
        history = lcu_get(
            creds[0], creds[1], creds[2],
            f"/lol-match-history/v1/products/lol/{puuid}/matches"
            "?begIndex=0&endIndex=20")
    except Exception:
        return
    games_node = (history or {}).get("games")
    games = games_node.get("games") if isinstance(games_node, dict) \
        else (games_node or [])
    now_ms = int(time.time() * 1000)
    used = set()
    keep = []
    for entry in load_pending():  # oldest first
        if not entry.get("reported"):
            if telemetry_post("/report", entry["record"]):
                entry["reported"] = True
            else:
                keep.append(entry)
                continue
        result = match_result_for_entry(entry, games, puuid, now_ms, used)
        if result is None:
            keep.append(entry)
        elif telemetry_post("/result", {"record_id": entry["record_id"],
                                        "result": result}):
            pass  # resolved — drop the entry
        else:
            keep.append(entry)
    with _FILE_LOCK:
        save_pending(keep)

def backfill_loop():
    time.sleep(20)
    while True:
        try:
            backfill_once()
        except Exception:
            pass
        time.sleep(120)

# ---------------------------------------------------------------------------
# Demo mode: a simulated champ select that fills in pick by pick
# ---------------------------------------------------------------------------

DEMO_CHAMPIONS = {266: "Aatrox", 103: "Ahri", 61: "Orianna", 222: "Jinx",
                  62: "Wukong", 421: "Rek'Sai", 145: "Kai'Sa",
                  238: "Zed", 7: "LeBlanc", 141: "Kayn"}

def _demo_session(picks):
    """picks: dict cellId -> championId applied over an empty lobby."""
    base = {
        "localPlayerCellId": 2,
        "myTeam": [
            {"cellId": 0, "championId": 0, "assignedPosition": "top"},
            {"cellId": 1, "championId": 0, "assignedPosition": "middle"},
            {"cellId": 2, "championId": 0, "assignedPosition": "jungle"},
            {"cellId": 3, "championId": 0, "assignedPosition": "bottom"},
            {"cellId": 4, "championId": 0, "assignedPosition": "utility"},
        ],
        "theirTeam": [
            {"cellId": 5, "championId": 0, "assignedPosition": "jungle"},
            {"cellId": 6, "championId": 0, "assignedPosition": "middle"},
            {"cellId": 7, "championId": 0, "assignedPosition": "bottom"},
            {"cellId": 8, "championId": 0, "assignedPosition": "top"},
            {"cellId": 9, "championId": 0, "assignedPosition": "utility"},
        ],
        "bans": {"myTeamBans": [238], "theirTeamBans": [7]},
    }
    for team in ("myTeam", "theirTeam"):
        for member in base[team]:
            if member["cellId"] in picks:
                member["championId"] = picks[member["cellId"]]
    return base

DEMO_STAGES = [
    {},                                   # bans done, nobody picked yet
    {5: 421},                             # enemy locks Rek'Sai (your counterpart)
    {5: 421, 0: 266},                     # ally Aatrox
    {5: 421, 0: 266, 6: 61},              # enemy Orianna
    {5: 421, 0: 266, 6: 61, 1: 103},      # ally Ahri
    {5: 421, 0: 266, 6: 61, 1: 103, 7: 145},   # enemy Kai'Sa
    {5: 421, 0: 266, 6: 61, 1: 103, 7: 145, 3: 222},  # ally Jinx — full picture
]

def _demo_lobby(session):
    """Demo lobby for the Scout tab: the five demo teammates, with the
    local player's cell as 'You' (no identities exist offline)."""
    local_cell = session.get("localPlayerCellId")
    teammates = []
    ally_n = 0
    for member in session.get("myTeam", []):
        is_self = member.get("cellId") == local_cell
        if not is_self:
            ally_n += 1
        champ_id = member.get("championId") or 0
        teammates.append({
            "name": "You" if is_self else f"Ally {ally_n}",
            "tag": None,
            "puuid": None,
            "role": lobby_role(member.get("assignedPosition")),
            "champion": DEMO_CHAMPIONS.get(champ_id, "") if champ_id else "",
            "isSelf": is_self,
        })
    return {"inChampSelect": True, "teammates": teammates,
            "me": {"name": "You", "tag": None, "puuid": None}}

def demo_poller():
    set_state(connected=True, in_champ_select=True,
              message="Demo mode — simulated champ select")
    stage = 0
    while True:
        session = _demo_session(DEMO_STAGES[stage])
        draft = parse_session(session, DEMO_CHAMPIONS)
        set_state(params=draft_params(draft), draft=draft,
                  lobby=_demo_lobby(session),
                  in_champ_select=True, connected=True)
        print(f"[demo] stage {stage + 1}/{len(DEMO_STAGES)}: {json.dumps(draft)}")
        stage = min(stage + 1, len(DEMO_STAGES) - 1)
        time.sleep(6)

# ---------------------------------------------------------------------------
# Real poller: watch the actual client
# ---------------------------------------------------------------------------

def real_poller(lockfile_arg):
    champ_names = None
    version = None
    creds = None
    last_draft = None   # most recent parsed session, for draft-end filing
    absent_polls = 0    # consecutive polls with no session (debounce)
    while True:
        try:
            if champ_names is None:
                version, champ_names = fetch_champion_names()
                print(f"Champion list loaded (patch {version}).")
            if creds is None:
                creds = read_lockfile(lockfile_arg)
                if creds is None:
                    set_state(connected=False, in_champ_select=False, params="",
                              draft=None, lobby=empty_lobby(),
                              message="Waiting for the League client…")
                    time.sleep(3)
                    continue
                print(f"League client found (LCU port {creds[1]}).")
            session = None
            try:
                session = lcu_get(creds[0], creds[1], creds[2],
                                  "/lol-champ-select/v1/session")
            except Exception:
                session = None  # lobby / in game / client restarted
            if session:
                absent_polls = 0
                draft = parse_session(session, champ_names)
                last_draft = draft
                params = draft_params(draft)
                if params != get_state()["params"]:
                    print("Draft update:", json.dumps(draft))
                set_state(connected=True, in_champ_select=True, params=params,
                          draft=draft, lobby=build_lobby(session, champ_names, creds),
                          message="Live — champ select in progress")
            else:
                absent_polls += 1
                if last_draft is not None and absent_polls >= 2:
                    # Champ select is really over (two clean misses, not
                    # one flaky read): file the draft for telemetry.
                    ended, last_draft = last_draft, None
                    on_draft_ended(ended)
                st = get_state()
                if st["in_champ_select"]:
                    print("Champ select ended.")
                set_state(connected=True, in_champ_select=False, params="",
                          draft=None,
                          lobby=empty_lobby(fetch_current_summoner(creds)),
                          message="Connected — waiting for champ select…")
        except Exception as exc:  # network hiccup, client restart, etc.
            creds = None
            set_state(connected=False, in_champ_select=False, params="", draft=None,
                      lobby=empty_lobby(),
                      message=f"Reconnecting… ({exc.__class__.__name__})")
        time.sleep(2)

# ---------------------------------------------------------------------------
# Self-update of the executable (frozen Windows builds only)
#
# A running .exe cannot overwrite itself, so the swap is two-staged:
#   1. check_for_updates (background, once per launch) downloads a
#      newer release's CtrlWard.exe into an "update" folder beside the
#      app — but only when GitHub records a sha256 digest for the
#      asset — verifies the download's size and sha256 against that
#      metadata, and writes update/pending.json.
#   2. At the NEXT launch, before anything else starts,
#      apply_pending_update() re-verifies the staged file and hands
#      the swap to a tiny batch helper: the helper waits for this
#      process to exit, renames the running exe to CtrlWard.exe.bak,
#      moves the new one into place, and relaunches it.
# update/attempted.json is the anti-loop guard: if a version was
# already attempted and this process STILL runs the old build, the
# staged update is dropped rather than retried on every launch (the
# release-page banner remains as the manual fallback).
# A release whose .exe is byte-identical to the running build (a
# re-cut release of the same bits) is skipped before any download:
# stage_exe_update compares the asset's digest with the running
# exe's own sha256 (_running_exe_sha256).
# Nothing here runs unless the app is a frozen Windows build; from
# source, on other platforms, and in --demo-from-source, every entry
# point below is a no-op. Nothing outside the exe's own folder and
# its "update" subfolder is ever touched.
# ---------------------------------------------------------------------------

EXE_ASSET_NAME = "CtrlWard.exe"
UPDATE_FOLDER = "update"

def _is_frozen_windows():
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"

def _update_dir(base=None):
    return os.path.join(base if base is not None else app_dir(),
                        UPDATE_FOLDER)

def _staged_exe(base=None):
    return os.path.join(_update_dir(base), EXE_ASSET_NAME + ".new")

def _pending_file(base=None):
    return os.path.join(_update_dir(base), "pending.json")

def _attempted_file(base=None):
    return os.path.join(_update_dir(base), "attempted.json")

def _remove_quietly(path):
    try:
        os.remove(path)
    except Exception:
        pass

def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _running_exe_sha256():
    """sha256 of the executable file this process is running from
    (sys.executable in a frozen build), or "" when it cannot be
    determined — from source, or if the file cannot be read. Used by
    stage_exe_update to skip staging a release whose .exe is
    byte-identical to the build already running (a re-cut release):
    identical bits are not an update, whatever the tag says."""
    try:
        if not getattr(sys, "frozen", False):
            return ""
        return _sha256_file(sys.executable)
    except Exception:
        return ""

def _asset_sha256(asset):
    """The sha256 GitHub recorded for a release asset (its "digest"
    field has the form "sha256:<hex>"), or "" when the asset carries
    no digest. An asset without a digest is never downloaded."""
    digest = str((asset or {}).get("digest") or "").strip()
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()
    return ""

def verify_staged_file(path, size, sha256):
    """True only when the file at path exists and matches BOTH the
    recorded byte size and the recorded sha256. A missing digest or
    size never verifies — an unverifiable file is never applied."""
    if not sha256 or size is None:
        return False
    try:
        if os.path.getsize(path) != int(size):
            return False
        return _sha256_file(path) == str(sha256).lower()
    except Exception:
        return False

def read_pending(base=None):
    data = _read_json_file(_pending_file(base), None)
    return data if isinstance(data, dict) else None

def _read_attempted(base=None):
    data = _read_json_file(_attempted_file(base), None)
    return data if isinstance(data, dict) else None

def already_attempted(version, base=None):
    attempted = _read_attempted(base)
    return bool(attempted) and attempted.get("version") == version

def should_block_reapply(pending_version, attempted_version,
                         running_version):
    """Anti-loop guard: an apply of pending_version was already
    attempted, yet this process still runs an older build — the swap
    did not take (or the user rolled back to the .bak). Do not try
    again; drop the staged update and let the banner do its job."""
    if not pending_version or attempted_version != pending_version:
        return False
    return is_newer(pending_version, running_version)

def stage_exe_update(version, asset, base=None, log=print):
    """Download a release's CtrlWard.exe asset into the update
    folder, verify it against the asset's size + sha256 digest, and
    record pending.json. Returns True when a verified update is
    staged for the next launch. Any failure deletes the partial
    download and returns False; the only trace is a log line.
    Callers gate on _is_frozen_windows(); this re-checks anyway."""
    if not _is_frozen_windows():
        return False
    sha = _asset_sha256(asset)
    url = (asset or {}).get("browser_download_url")
    size = (asset or {}).get("size")
    if not sha or not url or size is None:
        return False  # no digest (or no size) -> never download an exe
    if already_attempted(version, base):
        log(f"Self-update: v{version} was already attempted once — "
            "not staging it again.")
        return False
    if _running_exe_sha256() == sha:
        log(f"Self-update: v{version}'s release exe is identical to "
            "the running build — nothing to stage.")
        return False
    target = _staged_exe(base)
    try:
        os.makedirs(_update_dir(base), exist_ok=True)
        data, _ctype = http_get_bytes(url, timeout=60)
        with open(target, "wb") as f:
            f.write(data)
        if not verify_staged_file(target, size, sha):
            log("Self-update: downloaded exe failed size/sha256 "
                "verification — discarded.")
            _remove_quietly(target)
            return False
        _write_json_file(_pending_file(base),
                         {"version": version, "sha256": sha,
                          "size": size})
        log(f"Self-update: CtrlWard v{version} downloaded and "
            "verified — it applies on the next launch.")
        return True
    except Exception as exc:
        log(f"Self-update: staging failed ({exc.__class__.__name__}) "
            "— continuing without it.")
        _remove_quietly(target)
        return False

def build_apply_script(exe_path, staged_path, pending_file):
    """The batch helper's text. Steps, in order: wait for the running
    CtrlWard process to exit (the exe stays locked until it does, so
    the rename retries), rename the exe to .bak (replacing any older
    .bak), move the staged file into place, delete pending.json, and
    relaunch. If the move fails, the .bak is renamed back and the old
    build relaunches instead."""
    exe_dir = os.path.dirname(exe_path)
    exe_name = os.path.basename(exe_path)
    bak_name = exe_name + ".bak"
    rel_new = os.path.relpath(staged_path, exe_dir)
    rel_pending = os.path.relpath(pending_file, exe_dir)
    lines = [
        "@echo off",
        "rem CtrlWard self-update helper (generated by app.py; "
        "safe to delete).",
        f'cd /d "{exe_dir}"',
        "rem Step 1: wait for the running CtrlWard to exit.",
        "timeout /t 2 /nobreak >nul",
        "set TRIES=0",
        ":wait_for_exit",
        "set /a TRIES+=1",
        "if %TRIES% GTR 90 goto done",
        f'if exist "{bak_name}" del /f /q "{bak_name}"',
        f'ren "{exe_name}" "{bak_name}" 2>nul',
        f'if exist "{bak_name}" goto swap_in',
        "timeout /t 1 /nobreak >nul",
        "goto wait_for_exit",
        ":swap_in",
        "rem Step 2: the verified new build moves into place.",
        f'move /y "{rel_new}" "{exe_name}" >nul',
        f'if not exist "{exe_name}" goto rollback',
        f'del /f /q "{rel_pending}" 2>nul',
        "rem Step 3: relaunch.",
        f'start "" "{exe_name}"',
        "goto done",
        ":rollback",
        f'ren "{bak_name}" "{exe_name}"',
        f'start "" "{exe_name}"',
        ":done",
        'del /f /q "%~f0"',
        "",
    ]
    return "\r\n".join(lines)

def launch_apply_helper(bat_path, cwd):
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", bat_path], cwd=cwd,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)

def apply_pending_update(base=None, exe_path=None, launcher=None,
                         log=print):
    """Apply a staged exe update, if one is pending. Called at the
    very start of main(), before the server or UI. Returns True when
    the helper was launched and this process must exit immediately
    (the helper relaunches the new build once this one is gone).
    Any problem cleans up the staged files and returns False, so the
    app simply starts normally."""
    if not _is_frozen_windows():
        return False
    base = base if base is not None else app_dir()
    if exe_path is None:
        exe_path = (sys.executable if getattr(sys, "frozen", False)
                    else os.path.join(base, EXE_ASSET_NAME))
    pending = read_pending(base)
    if not pending:
        return False
    version = str(pending.get("version") or "")
    staged = _staged_exe(base)
    try:
        if not version or not is_newer(version, APP_VERSION):
            # Already running this (or a newer) build — the staged
            # files are leftovers; clear them and start normally.
            _remove_quietly(_pending_file(base))
            _remove_quietly(staged)
            return False
        attempted = _read_attempted(base)
        if should_block_reapply(version,
                                (attempted or {}).get("version"),
                                APP_VERSION):
            log(f"Self-update: v{version} was already attempted and "
                f"this is still v{APP_VERSION} — dropping the staged "
                "update instead of looping.")
            _remove_quietly(_pending_file(base))
            _remove_quietly(staged)
            return False
        if not verify_staged_file(staged, pending.get("size"),
                                  pending.get("sha256")):
            log("Self-update: staged exe failed re-verification — "
                "dropping it.")
            _remove_quietly(_pending_file(base))
            _remove_quietly(staged)
            return False
        bat_path = os.path.join(_update_dir(base), "apply_update.bat")
        with open(bat_path, "w", encoding="utf-8", newline="") as f:
            f.write(build_apply_script(exe_path, staged,
                                       _pending_file(base)))
        _write_json_file(_attempted_file(base),
                         {"version": version,
                          "at": int(time.time())})
        (launcher or launch_apply_helper)(
            bat_path, os.path.dirname(exe_path))
        log(f"Self-update: applying v{version} — restarting…")
        return True
    except Exception as exc:
        log(f"Self-update: apply failed ({exc.__class__.__name__}) "
            "— starting normally.")
        return False

# ---------------------------------------------------------------------------
# Update check (background, once per launch, silent on any failure)
# ---------------------------------------------------------------------------

def check_for_updates():
    repo = load_update_repo()
    if not repo:
        return
    try:
        release = http_get_json(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=20)
        tag = (release.get("tag_name") or "").strip()
        if not tag:
            return
        version = tag.lstrip("vV")
        assets = {a.get("name"): a.get("browser_download_url")
                  for a in release.get("assets", [])}
        tool_url = assets.get("tool.html")
        exe_asset = next((a for a in release.get("assets", [])
                          if a.get("name") == EXE_ASSET_NAME), None)
        if tool_url and is_newer(tag, current_tool_version()):
            # A newer tool page than the one this install is running:
            # download it beside the app (find_tool_html prefers it over
            # the bundled copy), so it applies on the next launch.
            tool_downloaded = False
            try:
                data, _ctype = http_get_bytes(tool_url, timeout=30)
                if b"Champion" in data or b"champion" in data:
                    target = os.path.join(app_dir(), "tool.html")
                    with open(target, "wb") as f:
                        f.write(data)
                    with open(os.path.join(app_dir(), "tool_version.txt"),
                              "w", encoding="utf-8") as f:
                        f.write(version)
                    tool_downloaded = True
                    print(f"Update: tool v{version} downloaded — "
                          "it applies on the next launch.")
            except Exception:
                tool_downloaded = False
            set_state(update={"version": version,
                              "url": release.get("html_url", ""),
                              "tool_downloaded": tool_downloaded})
            print(f"Update available: v{version} "
                  f"({release.get('html_url', '')})")
        elif not tool_url and is_newer(tag, APP_VERSION):
            # An .exe-only release (no tool page to auto-apply): point
            # at the release page. (On frozen Windows builds the .exe
            # is ALSO staged for self-update just below; the banner
            # remains the fallback everywhere else.)
            set_state(update={"version": version,
                              "url": release.get("html_url", ""),
                              "tool_downloaded": False})
            print(f"Update available: v{version} "
                  f"({release.get('html_url', '')})")
        # Otherwise the tool from the latest release is already the one
        # being served — stay silent, no banner.
        # Self-update of the .exe itself (frozen Windows builds only):
        # a release newer than the running app that carries a
        # CtrlWard.exe asset is staged in the background; the next
        # launch applies it (see apply_pending_update).
        if (_is_frozen_windows() and exe_asset is not None
                and is_newer(tag, APP_VERSION)):
            set_state(appUpdate={"available": version})
            if stage_exe_update(version, exe_asset):
                set_state(appUpdate={"pendingRestart": version})
            elif (_asset_sha256(exe_asset)
                    and _running_exe_sha256()
                    == _asset_sha256(exe_asset)):
                # The release .exe IS the running build (a re-cut
                # release): nothing is actually available — withdraw
                # the notice stage_exe_update just declined.
                set_state(appUpdate=None)
    except Exception:
        pass  # offline or repo not set up yet — updates are best-effort

# ---------------------------------------------------------------------------
# Local web server: the tool page, its artwork, and the live-state API
# ---------------------------------------------------------------------------

LIVE_SCRIPT = """
<script>
(function(){
  var fav = document.querySelector("link[rel~='icon']") || document.createElement('link');
  fav.rel = 'icon';
  fav.type = 'image/x-icon';
  fav.href = '/favicon.ico';
  document.head.appendChild(fav);
  var pill = document.createElement('div');
  pill.id = 'lcu-pill';
  pill.textContent = '○ Connecting to app…';
  pill.style.cssText = 'position:fixed;top:10px;right:10px;z-index:9999;' +
    'background:#0b0e1a;color:#e8ecf8;border:1px solid #ff4655;' +
    'font:600 12px/1.5 system-ui,sans-serif;padding:6px 10px;border-radius:4px;' +
    'letter-spacing:.04em;pointer-events:none;opacity:.92';
  document.body.appendChild(pill);
  var upd = document.createElement('a');
  upd.id = 'lcu-update';
  upd.target = '_blank';
  upd.style.cssText = 'position:fixed;top:46px;right:10px;z-index:9999;display:none;' +
    'background:#1a1206;color:#ffb74d;border:1px solid #ff9800;' +
    'font:600 12px/1.5 system-ui,sans-serif;padding:6px 10px;border-radius:4px;' +
    'letter-spacing:.04em;text-decoration:none;opacity:.95';
  document.body.appendChild(upd);
  var tel = document.createElement('div');
  tel.id = 'cw-telemetry';
  tel.style.cssText = 'position:fixed;top:82px;right:10px;z-index:9999;width:238px;' +
    'background:#0b0e1a;color:#aeb6cc;border:1px solid #2a3049;' +
    'font:500 11px/1.45 system-ui,sans-serif;padding:7px 10px;border-radius:4px;' +
    'letter-spacing:.02em;opacity:.94';
  tel.textContent = 'Loading settings…';
  document.body.appendChild(tel);
  function renderSettings(s){
    var html = '';
    if(!s.available){
      html += '<b style="color:#e8ecf8">Anonymous draft sharing</b><br>' +
        'Not configured in this build — nothing is shared.';
    } else {
      html += '<label style="display:flex;gap:7px;align-items:flex-start;cursor:pointer">' +
        '<input type="checkbox" id="cw-tel-check"' + (s.enabled ? ' checked' : '') +
        ' style="margin-top:1px"> <span><b style="color:#e8ecf8">Share anonymous draft data</b><br>' +
        'One record per finished draft: date, role, queue, the champions picked, ' +
        'the Top 3 shown here, and the game\\'s win/loss. No names, no Riot IDs, ' +
        'no timestamps. Off by default; nothing is sent unless you opt in.</span></label>';
    }
    html += '<div style="border-top:1px solid #2a3049;margin:8px 0 7px"></div>' +
      '<label style="display:flex;gap:7px;align-items:flex-start;cursor:pointer">' +
      '<input type="checkbox" id="cw-overlay-check"' + (s.overlay_enabled ? ' checked' : '') +
      ' style="margin-top:1px"> <span><b style="color:#e8ecf8">In-game CS overlay (prototype)</b><br>' +
      'A small always-on-top window showing only your own CS and CS/min while ' +
      'you play — display only, no advice, no other players\\' numbers. League ' +
      'must be in Borderless mode. Off by default.</span></label>';
    tel.innerHTML = html;
    var box = document.getElementById('cw-tel-check');
    if(box) box.addEventListener('change', function(){
      fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({telemetry_enabled: box.checked})}).catch(function(){});
    });
    var obox = document.getElementById('cw-overlay-check');
    if(obox) obox.addEventListener('change', function(){
      fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({overlay_enabled: obox.checked})}).catch(function(){});
    });
  }
  fetch('/api/settings').then(function(r){ return r.json(); })
    .then(renderSettings).catch(function(){ tel.style.display = 'none'; });
  function chipNames(id){
    var out = [], els = document.querySelectorAll('#' + id + ' .draft-slot.filled .slot-name');
    for(var i = 0; i < els.length; i++) out.push(els[i].textContent.trim());
    return out;
  }
  function top3Names(){
    var out = [], els = document.querySelectorAll('#draftRecs .draft-card .dn');
    for(var i = 0; i < els.length; i++){
      var t = els[i].textContent.trim().replace(/^#\\d+\\s*/, '');
      if(t) out.push(t);
    }
    return out.slice(0, 3);
  }
  var lastTop3Key = null;
  function tickTop3(){
    var payload = { top3: top3Names(),
                    ally: chipNames('draftAllyChips'),
                    enemy: chipNames('draftEnemyChips') };
    var key = JSON.stringify(payload);
    if(key === lastTop3Key) return;
    lastTop3Key = key;
    fetch('/api/top3', {method:'POST', headers:{'Content-Type':'application/json'},
      body: key}).catch(function(){});
  }
  setInterval(tickTop3, 1500);
  var lastSig = null;
  function tick(){
    fetch('/api/state').then(function(r){ return r.json(); }).then(function(s){
      window.CTRLWARD_STATE = s;
      try { window.dispatchEvent(new CustomEvent('ctrlward-state', {detail: s})); } catch(e) {}
      pill.textContent = s.in_champ_select ? '● LIVE — champ select'
        : (s.connected ? '○ Connected — waiting for champ select' : '○ Waiting for League client…');
      pill.style.borderColor = s.in_champ_select ? '#2ee6a8' : '#ff4655';
      if(s.update && s.update.url){
        upd.href = s.update.url;
        upd.textContent = s.update.tool_downloaded
          ? '⬆ Tool v' + s.update.version + ' downloaded — restart app to apply · release page'
          : '⬆ Update v' + s.update.version + ' available — open release page';
        upd.style.display = 'block';
      }
      if(!s.params || s.signature === lastSig) return;
      lastSig = s.signature;
      var cur = location.search.replace(/^\\?/, '');
      if(cur === s.params) return;
      if(typeof window.parseDraftText === 'function' &&
         typeof window.applyDraftImport === 'function'){
        try {
          var obj = window.parseDraftText(s.params);
          if(obj){ window.applyDraftImport(obj, 'live app'); return; }
        } catch(e) {}
      }
      location.replace('/?' + s.params);
    }).catch(function(){});
  }
  setInterval(tick, 800);
  tick();
})();
</script>
"""

_ddragon_version = {"v": "16.21.1"}

def ddragon_url(kind, champ_id):
    if kind == "champs":
        return (f"https://ddragon.leagueoflegends.com/cdn/"
                f"{_ddragon_version['v']}/img/champion/{champ_id}.png")
    return f"https://ddragon.leagueoflegends.com/cdn/img/champion/loading/{champ_id}_0.jpg"

def fetch_asset(kind, champ_id):
    """Proxy + disk-cache the tool's artwork from Data Dragon."""
    ext = ".png" if kind == "champs" else ".jpg"
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{kind}_{champ_id}{ext}")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            return f.read(), ("image/png" if kind == "champs" else "image/jpeg")
    last = None
    for _attempt in range(3):
        try:
            data, ctype = http_get_bytes(ddragon_url(kind, champ_id), timeout=20)
            with open(path, "wb") as f:
                f.write(data)
            return data, ctype
        except Exception as exc:
            last = exc
            time.sleep(0.8)
    raise last

class Handler(BaseHTTPRequestHandler):
    tool_bytes = b""
    icon_bytes = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html", "/tool.html"):
                self._send(200, self.tool_bytes, "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                if self.icon_bytes:
                    self._send(200, self.icon_bytes, "image/x-icon")
                else:
                    self._send(404, b"no icon", "text/plain")
            elif path == "/api/state":
                body = json.dumps(get_state()).encode()
                self._send(200, body, "application/json")
            elif path == "/api/lookup":
                self._send(200, lookup_proxy(parsed.query),
                           "application/json")
            elif path == "/api/settings":
                body = json.dumps({
                    "available": telemetry_available(),
                    "enabled": telemetry_enabled(),
                    "overlay_enabled": overlay_enabled()}).encode()
                self._send(200, body, "application/json")
            elif path.startswith("/assets/champs/") or path.startswith("/assets/loading/"):
                kind = "champs" if "/champs/" in path else "loading"
                champ_id = os.path.basename(path).rsplit(".", 1)[0]
                champ_id = urllib.parse.unquote(champ_id)
                if not champ_id.replace("'", "").isalnum():
                    self._send(400, b"bad champion id", "text/plain")
                    return
                data, ctype = fetch_asset(kind, champ_id)
                self._send(200, data, ctype)
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            try:
                self._send(502, f"asset error: {exc}".encode(), "text/plain")
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            self._send(400, b"bad request", "text/plain")
            return
        try:
            if parsed.path == "/api/settings":
                if "telemetry_enabled" in data:
                    set_telemetry_enabled(bool(data["telemetry_enabled"]))
                if "overlay_enabled" in data:
                    set_overlay_enabled(bool(data["overlay_enabled"]))
                    _publish_overlay()
                body = json.dumps({
                    "available": telemetry_available(),
                    "enabled": telemetry_enabled(),
                    "overlay_enabled": overlay_enabled()}).encode()
                self._send(200, body, "application/json")
            elif parsed.path == "/api/top3":
                stored = store_top3(data)
                body = json.dumps({"stored": stored}).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception:
            try:
                self._send(500, b"internal error", "text/plain")
            except Exception:
                pass

def build_tool_bytes():
    path = find_tool_html()
    if not path:
        raise SystemExit("tool.html not found next to the app. Keep tool.html "
                         "in the same folder as CtrlWard.exe / app.py.")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    idx = html.lower().rfind("</body>")
    if idx >= 0:
        html = html[:idx] + LIVE_SCRIPT + html[idx:]
    else:
        html += LIVE_SCRIPT
    return html.encode("utf-8")

# ---------------------------------------------------------------------------
# App window: Edge/Chrome --app mode if available, else default browser
# ---------------------------------------------------------------------------

def open_app_window(url):
    candidates = []
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if os.name == "nt":
        for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            if os.path.exists(p):
                candidates.append(p)
    profile = os.path.join(app_dir(), "app_profile")
    for exe in candidates:
        try:
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}",
                              "--window-size=1280,920"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Opened app window via {os.path.basename(exe)}.")
            return
        except Exception:
            continue
    webbrowser.open(url)
    print("Opened in your default browser.")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # A staged self-update applies before anything else starts: the
    # batch helper swaps the .exe and relaunches it, so this process
    # exits immediately. No-op unless a verified update is pending
    # (and only ever on frozen Windows builds).
    if apply_pending_update():
        return 0

    ap = argparse.ArgumentParser(description="CtrlWard (read-only live draft)")
    ap.add_argument("--demo", action="store_true", help="Simulated champ select")
    ap.add_argument("--port", type=int, default=0, help="Fixed port (default: automatic)")
    ap.add_argument("--no-open", action="store_true", help="Server only, no window")
    ap.add_argument("--lockfile", help="Path to the League lockfile")
    args = ap.parse_args()

    _LCU_ARGS["lockfile"] = args.lockfile
    DEMO_MODE["on"] = bool(args.demo)

    Handler.tool_bytes = build_tool_bytes()
    icon_path = find_icon()
    if icon_path:
        with open(icon_path, "rb") as f:
            Handler.icon_bytes = f.read()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    # Learn the current Data Dragon version for artwork URLs, in the
    # background so a slow network never delays the window opening.
    def _refresh_version():
        try:
            _ddragon_version["v"] = http_get_json(
                "https://ddragon.leagueoflegends.com/api/versions.json",
                timeout=15)[0]
        except Exception:
            pass
    threading.Thread(target=_refresh_version, daemon=True).start()

    print(f"CtrlWard running at {url}")
    print("Read-only: it watches champ select and recommends. It never picks or locks.")

    if args.demo:
        poller = threading.Thread(target=demo_poller, daemon=True)
    else:
        poller = threading.Thread(target=real_poller, args=(args.lockfile,),
                                  daemon=True)
    poller.start()

    threading.Thread(target=check_for_updates, daemon=True).start()

    # In-game CS overlay (prototype): a background poller plus, on
    # Windows, the little always-on-top window. Both stay inert
    # unless the user enables the overlay in the settings panel;
    # the window loop also no-ops off Windows / without tkinter.
    _publish_overlay()
    threading.Thread(target=overlay_poller, daemon=True).start()
    threading.Thread(target=overlay_window_loop, daemon=True).start()

    # Outcome backfill for telemetry (no-ops unless the user opted in
    # and a report endpoint is configured).
    threading.Thread(target=backfill_loop, daemon=True).start()

    if not args.no_open:
        open_app_window(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
