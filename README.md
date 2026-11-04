# Control Ward

A champion blind-pick and **live draft** companion for League of Legends,
built from the math in MachineLoLing's *"The math behind champion
blindability"* — expected win rate when blind picking, matchup spread, and
counter risk for all 173 champions across all 5 roles, plus a Live Draft mode
that recommends picks in real time as a real champ select unfolds.

## ⬇️ Download

Go to **[Releases](../../releases/latest)** and download **ControlWard.exe**.
That's the whole app — one file, no installer, no Python needed.

1. Keep your League client open and logged in.
2. Double-click `ControlWard.exe`.
   Windows SmartScreen will warn because the app is unsigned:
   **More info → Run anyway**.
3. A window opens. The pill in the top-right shows the connection state;
   in champ select it turns **● LIVE** and every pick/ban flows into the
   tool's Live Draft trays automatically, with Top Draft Options recomputed
   on every pick.

Requirements: Windows, a League of Legends client, internet on first run
(champion art/names are fetched from Riot's Data Dragon CDN, then cached).

## What it does

- **Blindability rankings** for every champion in every role: expected win
  rate, matchup spread, counter risk, worst counter, per-champion bell curve
  and verdict, Jungle first-clear times, personal pool stars.
- **Live Draft**: watches *your* champ select through your own client's local
  API and recommends the top 3 picks for your role, scored on counter
  (your lane counterpart weighted 3×), synergy (damage balance, frontline,
  CC, engage/peel/poke), and blind safety — with plain-language reasons.
- **Read-only, permanently.** The app only *reads* the champ-select session
  your client already displays (`GET /lol-champ-select/v1/session`). It never
  picks, hovers, locks, bans, accepts or dodges. That is a design boundary,
  not a setting.

> Data honesty: built-in matchup numbers are an illustrative archetype model,
> clearly labelled in the tool — Riot publishes no official matchup win
> rates. The tool can import a CSV of real stats (Lolalytics/U.GG/OP.GG
> exports) and recompute everything from those instead.

## Updates

The app checks this repo's latest Release once per launch (when configured
with the repo name). Newer `tool.html` releases download automatically and
apply on the next launch; newer `.exe` releases show a banner linking here.

## Build from source

Prefer not to run a prebuilt .exe? Everything is here:

- `app.py` — the whole app (Python 3.9+ standard library only)
- `tool.html` — the tool's page
- `run_app.bat` / `run_demo.bat` — run without building (demo mode simulates
  a champ select)
- `build.bat` — builds the one-file .exe via PyInstaller

## Privacy

All local. Traffic is only: your League client on `127.0.0.1`, Riot's public
Data Dragon CDN, and this repo's release API if update checks are configured.
No accounts, no API keys, no telemetry.

*Unofficial fan project. Not affiliated with Riot Games. League of Legends
and all champion artwork are trademarks of Riot Games, Inc.*
