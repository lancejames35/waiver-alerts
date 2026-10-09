"""
Waiver alerts (ESPN). Designed to run every 30 minutes on a schedule.

Each run:
  - Digest: at 8am, noon, 6pm CT daily, plus Sunday 11am, sends one ntfy push per league
  - Watch: every run, sends an immediate push per league only on big moves:
      * one of your players' weekly projection drops 40%+ or to zero
      * an available player's weekly projection jumps 5+ points and now beats your worst at his position

Env vars:
  ESPN_S2, ESPN_SWID   ESPN cookies (required)
  NTFY_TOPIC           your private ntfy topic (required unless DRY_RUN=1)
  DRY_RUN=1            print messages instead of sending
  FORCE_DIGEST=1       send the digest now regardless of time (for testing)
"""

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ---------------- config ----------------
SEASON = 2026
LEAGUES = [
    {"id": 464624, "team_id": 12},   # Orange All In
    {"id": 4199856, "team_id": 11},  # Number 2.
]
POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"]  # also the display order

WEEKLY_MARGIN = 1.5         # FA weekly proj must beat your worst at position by this many points
SEASON_MARGIN = 10.0        # FA rest-of-season proj must beat your worst at position by this many points
TRENDING_MIN_ADDS = 1000    # Sleeper adds in last 24h
TRENDING_MIN_ROS_PCT = 0.8  # trending player's ROS must be >= this share of your worst at position (0 = off)
FA_POOL_SIZE = 200
DIGEST_PER_POSITION = 2     # rows per position per section in the phone digest

TZ = ZoneInfo("America/Chicago")
DIGEST_SLOTS = [(None, 8), (None, 12), (None, 18), (6, 11)]  # (weekday Mon=0..Sun=6 or None for daily, hour)

DROP_PCT = 0.40             # your player's weekly proj falls by this share (or to zero)
DROP_MIN_PROJ = 5.0         # ...and was projected at least this much before (ignores bench filler)
JUMP_POINTS = 5.0           # available player's weekly proj rises by at least this many points
# ----------------------------------------

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{lid}"
FA_PAGE = "https://fantasy.espn.com/football/players/add?leagueId={lid}"
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOT_FILTER = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "K": 17, "D/ST": 16}
IR_SLOT = 21

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SLEEPER_FILE = os.path.join(STATE_DIR, "sleeper_ids.json")

DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_DIGEST = os.environ.get("FORCE_DIGEST") == "1"


# ---------- helpers ----------

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def stat(player, source, split, period):
    """source 0=actual 1=projected; split 0=season 1=week. Season projection is rest-of-season."""
    for s in player.get("stats") or []:
        if (s.get("statSourceId") == source and s.get("statSplitTypeId") == split
                and s.get("scoringPeriodId") == period and s.get("seasonId") == SEASON):
            return float(s.get("appliedTotal") or 0)
    return 0.0


def short(p):
    """'Jacoby Brissett' -> 'J. Brissett', D/ST names unchanged, [W] if on waivers."""
    parts = p["name"].split()
    name = p["name"] if p["pos"] == "D/ST" or len(parts) < 2 else f"{parts[0][0]}. {' '.join(parts[1:])}"
    return name + (" [W]" if p.get("tag") == "W" else "")


def worst(mine, pos, key, skip_zero=False):
    c = [m for m in mine if m["pos"] == pos and (m[key] > 0 or not skip_zero)]
    return min(c, key=lambda m: m[key]) if c else None


def notify(title, message, click=None, priority=3, tags=None):
    if DRY_RUN:
        print(f"\n----- [{title}] (priority {priority}) -----\n{message}\n(click: {click})")
        return
    r = requests.post("https://ntfy.sh/", json={
        "topic": os.environ["NTFY_TOPIC"], "title": title, "message": message,
        "click": click, "priority": priority, "tags": tags or [],
    }, timeout=30)
    r.raise_for_status()


# ---------- data ----------

def sleeper_trending():
    """{espn_id: adds_24h}. Sleeper's full player dump is only pulled once a day."""
    cache = load_json(SLEEPER_FILE, {})
    if not cache.get("map") or time.time() - cache.get("fetched", 0) > 86400:
        r = requests.get("https://api.sleeper.app/v1/players/nfl", timeout=60)
        r.raise_for_status()
        cache = {"fetched": time.time(),
                 "map": {sid: str(p["espn_id"]) for sid, p in r.json().items() if p.get("espn_id")}}
        save_json(SLEEPER_FILE, cache)
    to_espn = cache["map"]
    r = requests.get("https://api.sleeper.app/v1/players/nfl/trending/add",
                     params={"lookback_hours": 24, "limit": 300}, timeout=30)
    r.raise_for_status()
    return {to_espn[t["player_id"]]: t["count"] for t in r.json() if t["player_id"] in to_espn}


def fetch_league(lg, cookies, trending):
    url = BASE.format(season=SEASON, lid=lg["id"])

    s = requests.get(url, params={"view": "mSettings"}, cookies=cookies, timeout=30)
    s.raise_for_status()
    sd = s.json()
    week = sd["scoringPeriodId"]

    r = requests.get(url, params={"view": "mRoster", "scoringPeriodId": week}, cookies=cookies, timeout=30)
    r.raise_for_status()
    team = next((t for t in r.json()["teams"] if t["id"] == lg["team_id"]), None)
    if not team:
        raise RuntimeError(f"team {lg['team_id']} not found")

    mine = []
    for e in team["roster"]["entries"]:
        if e.get("lineupSlotId") == IR_SLOT:
            continue
        p = e["playerPoolEntry"]["player"]
        pos = POS.get(p.get("defaultPositionId"))
        if pos in POSITIONS:
            mine.append({"id": str(p["id"]), "name": p["fullName"], "pos": pos,
                         "wk": stat(p, 1, 1, week), "ros": stat(p, 1, 0, 0)})

    filt = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "filterSlotIds": {"value": [SLOT_FILTER[p] for p in POSITIONS]},
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "limit": FA_POOL_SIZE,
    }}
    fa = requests.get(url, params={"view": "kona_player_info", "scoringPeriodId": week},
                      headers={"X-Fantasy-Filter": json.dumps(filt)}, cookies=cookies, timeout=30)
    fa.raise_for_status()

    avail = []
    for entry in fa.json().get("players", []):
        p = entry["player"]
        pos = POS.get(p.get("defaultPositionId"))
        if pos not in POSITIONS:
            continue
        avail.append({"id": str(p["id"]), "name": p["fullName"], "pos": pos,
                      "tag": "W" if entry.get("status") == "WAIVERS" else "FA",
                      "wk": stat(p, 1, 1, week), "ros": stat(p, 1, 0, 0),
                      "own": p.get("ownership", {}).get("percentOwned", 0),
                      "adds": trending.get(str(p["id"]), 0)})

    return {"name": sd["settings"]["name"].strip(), "week": week, "mine": mine, "avail": avail,
            "click": FA_PAGE.format(lid=lg["id"])}


# ---------- logic ----------

def sections(d):
    out = {"week": [], "ros": [], "hot": []}
    for a in d["avail"]:
        w = worst(d["mine"], a["pos"], "wk", skip_zero=True)
        if w and a["wk"] >= w["wk"] + WEEKLY_MARGIN:
            diff = a["wk"] - w["wk"]
            out["week"].append((a["pos"], diff, f"{short(a)} {a['wk']:.1f} (+{diff:.1f} vs {short(w)})"))

        r = worst(d["mine"], a["pos"], "ros")
        if r and a["ros"] >= r["ros"] + SEASON_MARGIN:
            diff = a["ros"] - r["ros"]
            out["ros"].append((a["pos"], diff, f"{short(a)} {a['ros']:.0f} (+{diff:.0f} vs {short(r)})"))

        relevant = not r or a["ros"] >= TRENDING_MIN_ROS_PCT * r["ros"]
        if a["adds"] >= TRENDING_MIN_ADDS and relevant:
            out["hot"].append((a["pos"], a["adds"], f"{short(a)} {a['adds'] / 1000:.1f}k adds, wk {a['wk']:.1f}"))
    return out


def format_digest(d, secs):
    lines = []
    for title, key in (("THIS WEEK", "week"), ("REST OF SEASON", "ros"), ("TRENDING", "hot")):
        rows = secs[key]
        if not rows:
            continue
        lines.append(title)
        for pos in POSITIONS:
            group = sorted((r for r in rows if r[0] == pos), key=lambda r: r[1], reverse=True)[:DIGEST_PER_POSITION]
            for _, _, text in group:
                lines.append(f"{pos}  {text}")
        lines.append("")
    return "\n".join(lines).strip() or "Nothing on the wire beats your roster right now."


def watch(d, prev):
    """Returns (alert lines, alert keys). Only compares within the same week; a new week resets baselines."""
    if not prev or prev.get("week") != d["week"]:
        return [], []
    done = set(prev.get("alerted", []))
    lines, keys = [], []

    for m in d["mine"]:
        before = prev["mine"].get(m["id"])
        key = f"drop:{m['id']}"
        if before and before >= DROP_MIN_PROJ and key not in done and (m["wk"] == 0 or m["wk"] <= before * (1 - DROP_PCT)):
            lines.append(f"YOUR {m['pos']} {short(m)}: {before:.1f} -> {m['wk']:.1f} this week")
            keys.append(key)

    for a in d["avail"]:
        before = prev["avail"].get(a["id"])
        key = f"jump:{a['id']}"
        if before is None or key in done or a["wk"] - before < JUMP_POINTS:
            continue
        w = worst(d["mine"], a["pos"], "wk", skip_zero=True)
        if w and a["wk"] >= w["wk"] + WEEKLY_MARGIN:
            lines.append(f"{a['pos']} {short(a)} jumped {before:.1f} -> {a['wk']:.1f}, beats {short(w)} {w['wk']:.1f}")
            keys.append(key)

    return lines, keys


def digest_slot(now):
    for wd, hour in DIGEST_SLOTS:
        if now.hour == hour and (wd is None or now.weekday() == wd):
            return f"{now:%Y-%m-%d}-{hour}"
    return None


# ---------- main ----------

def main():
    s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
    if not s2 or not swid:
        sys.exit("Set ESPN_S2 and ESPN_SWID.")
    if not DRY_RUN and not os.environ.get("NTFY_TOPIC"):
        sys.exit("Set NTFY_TOPIC (or DRY_RUN=1).")
    cookies = {"espn_s2": s2, "SWID": swid}

    state = load_json(STATE_FILE, {"leagues": {}, "digests_sent": [], "auth_alert_day": None})
    now = datetime.now(TZ)
    slot = digest_slot(now)
    send_digest = FORCE_DIGEST or (slot is not None and slot not in state["digests_sent"])

    try:
        trending = sleeper_trending()
    except Exception as ex:
        print(f"Sleeper unavailable ({ex}); continuing without trending.")
        trending = {}

    for lg in LEAGUES:
        lid = str(lg["id"])
        try:
            d = fetch_league(lg, cookies, trending)
        except requests.HTTPError as ex:
            code = ex.response.status_code if ex.response is not None else None
            print(f"League {lid} failed: {ex}")
            if code in (401, 403) and state.get("auth_alert_day") != f"{now:%Y-%m-%d}":
                notify("ESPN login expired", "Waiver alerts can't read your ESPN leagues. "
                       "Grab fresh espn_s2 and SWID cookies and update the GitHub secrets.", priority=4)
                state["auth_alert_day"] = f"{now:%Y-%m-%d}"
            continue
        except Exception as ex:
            print(f"League {lid} failed: {ex}")
            continue

        prev = state["leagues"].get(lid)
        alert_lines, alert_keys = watch(d, prev)
        if alert_lines:
            notify(f"{d['name']}: heads up", "\n".join(alert_lines), click=d["click"], priority=4, tags=["rotating_light"])

        if send_digest:
            notify(f"{d['name']} - Wk {d['week']}", format_digest(d, sections(d)), click=d["click"], tags=["football"])

        carried = prev.get("alerted", []) if prev and prev.get("week") == d["week"] else []
        state["leagues"][lid] = {
            "week": d["week"],
            "mine": {m["id"]: m["wk"] for m in d["mine"]},
            "avail": {a["id"]: a["wk"] for a in d["avail"]},
            "alerted": carried + alert_keys,
        }

    if send_digest and slot and not FORCE_DIGEST:
        state["digests_sent"] = (state["digests_sent"] + [slot])[-30:]
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
