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
#   - a newer .exe can NOT replace the running program, so the app shows an
#     update banner linking to the release page instead.
# Set the repo in update_config.json next to the app: {"repo": "owner/name"}
# or bake it into DEFAULT_UPDATE_REPO at build time. Empty = checks disabled.
# ---------------------------------------------------------------------------

APP_VERSION = "1.2.0"
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
    "message": "Starting…",
    "app_version": APP_VERSION,
    "update": None,           # {version, url, tool_downloaded} when a newer release exists
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

def demo_poller():
    set_state(connected=True, in_champ_select=True,
              message="Demo mode — simulated champ select")
    stage = 0
    while True:
        draft = parse_session(_demo_session(DEMO_STAGES[stage]), DEMO_CHAMPIONS)
        set_state(params=draft_params(draft), draft=draft,
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
                              draft=None, message="Waiting for the League client…")
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
                          draft=draft, message="Live — champ select in progress")
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
                          draft=None, message="Connected — waiting for champ select…")
        except Exception as exc:  # network hiccup, client restart, etc.
            creds = None
            set_state(connected=False, in_champ_select=False, params="", draft=None,
                      message=f"Reconnecting… ({exc.__class__.__name__})")
        time.sleep(2)

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
            # An .exe-only release (no tool page to auto-apply): point at
            # the release page — a running .exe cannot replace itself.
            set_state(update={"version": version,
                              "url": release.get("html_url", ""),
                              "tool_downloaded": False})
            print(f"Update available: v{version} "
                  f"({release.get('html_url', '')})")
        # Otherwise the tool from the latest release is already the one
        # being served — stay silent, no banner.
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
  tel.textContent = 'Loading sharing settings…';
  document.body.appendChild(tel);
  function renderTelemetry(s){
    if(!s.available){
      tel.innerHTML = '<b style="color:#e8ecf8">Anonymous draft sharing</b><br>' +
        'Not configured in this build — nothing is shared.';
      return;
    }
    tel.innerHTML = '<label style="display:flex;gap:7px;align-items:flex-start;cursor:pointer">' +
      '<input type="checkbox" id="cw-tel-check"' + (s.enabled ? ' checked' : '') +
      ' style="margin-top:1px"> <span><b style="color:#e8ecf8">Share anonymous draft data</b><br>' +
      'One record per finished draft: date, role, queue, the champions picked, ' +
      'the Top 3 shown here, and the game\\'s win/loss. No names, no Riot IDs, ' +
      'no timestamps. Off by default; nothing is sent unless you opt in.</span></label>';
    var box = document.getElementById('cw-tel-check');
    if(box) box.addEventListener('change', function(){
      fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({telemetry_enabled: box.checked})}).catch(function(){});
    });
  }
  fetch('/api/settings').then(function(r){ return r.json(); })
    .then(renderTelemetry).catch(function(){ tel.style.display = 'none'; });
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
            elif path == "/api/settings":
                body = json.dumps({
                    "available": telemetry_available(),
                    "enabled": telemetry_enabled()}).encode()
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
                body = json.dumps({
                    "available": telemetry_available(),
                    "enabled": telemetry_enabled()}).encode()
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
    ap = argparse.ArgumentParser(description="CtrlWard (read-only live draft)")
    ap.add_argument("--demo", action="store_true", help="Simulated champ select")
    ap.add_argument("--port", type=int, default=0, help="Fixed port (default: automatic)")
    ap.add_argument("--no-open", action="store_true", help="Server only, no window")
    ap.add_argument("--lockfile", help="Path to the League lockfile")
    args = ap.parse_args()

    _LCU_ARGS["lockfile"] = args.lockfile

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
