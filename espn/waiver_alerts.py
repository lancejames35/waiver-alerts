"""
Waiver alerts (ESPN). Runs every 30 minutes on GitHub Actions.

DIGESTS (one per league)
  - 8am, noon, 6pm CT: only what's new or changed since the last digest; skipped if nothing changed
  - Sunday 11am ("Game day") and the evening before your league's waivers process ("Waivers tonight"):
    always full, since those are decision points
  - Sections: lineup problems first, then pickups this week, rest of season, trending

IMMEDIATE PUSHES (one per league per run, held 11pm-7am and sent at 7am)
  Your roster, every player:
    - injury designation changes (Questionable, Doubtful, Out, IR, back to healthy)
    - weekly projection moves 30%+ and 3+ pts, up or down
    - rest-of-season projection moves 15%+ and 20+ pts, up or down
  Available players:
    - weekly projection up 4+ pts and now 8+ projected
    - rest-of-season projection up 20+ pts
    - a dropped player clears waivers to free agent, if he beats your roster or is 50%+ rostered on ESPN

BASELINES
  - Status changes compare to the previous run.
  - Projection moves compare to the value at that player's last alert (or his first reading this week),
    so slow drifts trigger and further moves re-trigger. Projection baselines reset when the week changes.
  - Once a player's game kicks off he's frozen until next week.

Env vars:
  ESPN_S2, ESPN_SWID   ESPN cookies (required)
  NTFY_TOPIC           your private ntfy topic (required unless DRY_RUN=1)
  DRY_RUN=1            print messages instead of sending
  FORCE_DIGEST=1       send a full digest now (for testing)
"""

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ================= config =================
SEASON = 2026
LEAGUES = [
    {"id": 464624, "team_id": 12},   # Orange All In
    {"id": 4199856, "team_id": 11},  # Number 2.
]
POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"]  # also the display order
TZ = ZoneInfo("America/Chicago")

# digests
DAILY_DIGEST_HOURS = [8, 12, 18]
SUNDAY_FULL_HOUR = 11
PREWAIVER_HOUR = 20                  # evening before each waiver process day
DEFAULT_WAIVER_DAYS = ["WEDNESDAY"]  # used if the league setting can't be read
QUIET_START, QUIET_END = 23, 7       # immediate pushes held in this window, sent at 7am
DIGEST_PER_POSITION = 2
DIGEST_CHANGE_MIN = 1.0              # a repeated pickup row only reappears if its number moved this much

# pickup sections
WEEKLY_MARGIN = 1.5                  # beats your worst at position this week by this many points
SEASON_MARGIN = 10.0                 # beats your worst at position rest of season by this many points
TRENDING_MIN_ADDS = 1000             # Sleeper adds in last 24h
TRENDING_MIN_ROS_PCT = 0.8           # trending player's ROS >= this share of your worst at position (0 = off)
FA_POOL_SIZE = 200
LINEUP_MARGIN = 2.0                  # bench player must out-project a starter by this much to flag

# immediate pushes
ROSTER_WK_PCT, ROSTER_WK_PTS = 0.30, 3.0
ROSTER_ROS_PCT, ROSTER_ROS_PTS = 0.15, 20.0
AVAIL_WK_UP, AVAIL_WK_FLOOR = 4.0, 8.0
AVAIL_ROS_UP = 20.0
NOTABLE_OWN_PCT = 50.0               # newly free agent counts as notable at this % rostered on ESPN
# ==========================================

READ_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
LEAGUE_URL = READ_BASE + "/segments/0/leagues/{lid}"
FA_PAGE = "https://fantasy.espn.com/football/players/add?leagueId={lid}"
TEAM_PAGE = "https://fantasy.espn.com/football/team?leagueId={lid}&teamId={tid}&seasonId={season}"

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOT_FILTER = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "K": 17, "D/ST": 16}
BENCH, IR = 20, 21
STATUS = {None: "Healthy", "ACTIVE": "Healthy", "NORMAL": "Healthy", "QUESTIONABLE": "Questionable",
          "DAY_TO_DAY": "Questionable", "DOUBTFUL": "Doubtful", "OUT": "Out",
          "INJURY_RESERVE": "IR", "SUSPENSION": "Suspended"}
BAD_FOR_LINEUP = {"Doubtful", "Out", "IR", "Suspended"}
DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
SUFFIXES = {"Jr.", "Sr.", "II", "III", "IV", "V"}

EMOJI = {"injury": ("🚑", "ambulance"), "healthy": ("✅", "white_check_mark"),
         "down": ("📉", "chart_with_downwards_trend"), "up": ("📈", "chart_with_upwards_trend"),
         "rise": ("📈", "chart_with_upwards_trend"), "fa": ("🆓", "free"), "lineup": ("⚠️", "warning")}

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SLEEPER_FILE = os.path.join(STATE_DIR, "sleeper_ids.json")
STATE_VERSION = 2

DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_DIGEST = os.environ.get("FORCE_DIGEST") == "1"


# ================= helpers =================

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
    """'Jacoby Brissett' -> 'J. Brissett'; D/ST unchanged; [W] if on waivers."""
    parts = p["name"].split()
    name = p["name"] if p["pos"] == "D/ST" or len(parts) < 2 else f"{parts[0][0]}. {' '.join(parts[1:])}"
    return name + (" [W]" if p.get("tag") == "W" else "")


def last_name(p):
    parts = p["name"].split()
    if p["pos"] == "D/ST" or len(parts) < 2:
        return p["name"]
    return parts[-2] if parts[-1] in SUFFIXES and len(parts) > 2 else parts[-1]


def worst(mine, pos, key, skip_zero=False):
    c = [m for m in mine if m["pos"] == pos and m["slot"] != IR and (m[key] > 0 or not skip_zero)]
    return min(c, key=lambda m: m[key]) if c else None


def notify(title, message, priority=3, tags=None, actions=None, click=None):
    if DRY_RUN:
        print(f"\n===== {title}  (priority {priority}, tags {tags}) =====\n{message}")
        for a in actions or []:
            print(f"  [{a['label']}] {a['url']}")
        return
    r = requests.post("https://ntfy.sh/", json={
        "topic": os.environ["NTFY_TOPIC"], "title": title, "message": message,
        "priority": priority, "tags": tags or [], "actions": actions or [], "click": click,
    }, timeout=30)
    r.raise_for_status()


def buttons(d):
    return [{"action": "view", "label": "Free agents", "url": d["fa_url"]},
            {"action": "view", "label": "My team", "url": d["team_url"]}]


# ================= data =================

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


def schedule(state, week, cookies):
    """Kickoff time per NFL team and bye teams for the week. Refreshed every 6 hours (flexed games)."""
    c = state.get("schedule") or {}
    if c.get("week") == week and time.time() - c.get("fetched", 0) < 6 * 3600:
        return {int(k): v for k, v in c["kick"].items()}, set(c["bye"])
    try:
        r = requests.get(READ_BASE.format(season=SEASON), params={"view": "proTeamSchedules_wl"},
                         cookies=cookies, timeout=30)
        r.raise_for_status()
        kick, bye = {}, set()
        for t in r.json()["settings"]["proTeams"]:
            if t.get("byeWeek") == week:
                bye.add(t["id"])
            for g in (t.get("proGamesByScoringPeriod") or {}).get(str(week), []):
                kick[t["id"]] = g["date"] / 1000
        state["schedule"] = {"week": week, "fetched": time.time(), "kick": kick, "bye": sorted(bye)}
        return kick, bye
    except Exception as ex:
        print(f"Schedule unavailable ({ex}); nothing frozen this run.")
        return {}, set()


def to_player(p, week):
    return {"id": str(p["id"]), "name": p["fullName"], "pos": POS.get(p.get("defaultPositionId")),
            "team": p.get("proTeamId"), "status": STATUS.get(p.get("injuryStatus"), str(p.get("injuryStatus")).title()),
            "wk": stat(p, 1, 1, week), "ros": stat(p, 1, 0, 0), "elig": p.get("eligibleSlots") or []}


def fetch_league(lg, cookies, trending):
    url = LEAGUE_URL.format(season=SEASON, lid=lg["id"])

    s = requests.get(url, params={"view": "mSettings"}, cookies=cookies, timeout=30)
    s.raise_for_status()
    sd = s.json()
    week = sd["scoringPeriodId"]
    acq = sd.get("settings", {}).get("acquisitionSettings", {}) or {}
    waiver_days = [x for x in (acq.get("waiverProcessDays") or []) if x in DAYS] or DEFAULT_WAIVER_DAYS

    r = requests.get(url, params={"view": "mRoster", "scoringPeriodId": week}, cookies=cookies, timeout=30)
    r.raise_for_status()
    teams = r.json()["teams"]
    team = next((t for t in teams if t["id"] == lg["team_id"]), None)
    if not team:
        raise RuntimeError(f"team {lg['team_id']} not found")
    rostered = {str(e["playerId"]) for t in teams for e in t.get("roster", {}).get("entries", [])}

    mine = []
    for e in team["roster"]["entries"]:
        pl = to_player(e["playerPoolEntry"]["player"], week)
        if pl["pos"] in POSITIONS:
            pl["slot"] = e.get("lineupSlotId")
            mine.append(pl)

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
        pl = to_player(entry["player"], week)
        if pl["pos"] not in POSITIONS:
            continue
        pl["tag"] = "W" if entry.get("status") == "WAIVERS" else "FA"
        pl["own"] = entry["player"].get("ownership", {}).get("percentOwned", 0)
        pl["adds"] = trending.get(pl["id"], 0)
        avail.append(pl)

    return {"lid": str(lg["id"]), "name": sd["settings"]["name"].strip(), "week": week, "mine": mine,
            "avail": avail, "rostered": rostered, "waiver_days": waiver_days,
            "fa_url": FA_PAGE.format(lid=lg["id"]),
            "team_url": TEAM_PAGE.format(lid=lg["id"], tid=lg["team_id"], season=SEASON)}


# ================= lineup =================

def lineup_issues(d, is_locked, byes):
    """[(key, text)] for starters who are out/bye/zero or out-projected by an eligible bench player."""
    starters = [m for m in d["mine"] if m["slot"] not in (BENCH, IR) and not is_locked(m)]
    bench = [m for m in d["mine"] if m["slot"] == BENCH and not is_locked(m)
             and m["status"] not in BAD_FOR_LINEUP and m["team"] not in byes and m["wk"] > 0]
    out, used = [], set()
    for s in sorted(starters, key=lambda m: POSITIONS.index(m["pos"])):
        reason = (s["status"] if s["status"] in BAD_FOR_LINEUP else
                  "on bye" if s["team"] in byes else "projected 0" if s["wk"] == 0 else None)
        cands = [b for b in bench if s["slot"] in b["elig"] and b["id"] not in used]
        best = max(cands, key=lambda b: b["wk"], default=None)
        if reason:
            if best:
                out.append((f"lineup:{s['id']}:{best['id']}",
                            f"Start {short(best)} {best['wk']:.1f} over {short(s)} ({reason})"))
                used.add(best["id"])
            else:
                out.append((f"lineup:{s['id']}:none", f"{short(s)} is {reason}, no bench option"))
        elif best and best["wk"] >= s["wk"] + LINEUP_MARGIN:
            out.append((f"lineup:{s['id']}:{best['id']}",
                        f"Start {short(best)} {best['wk']:.1f} over {short(s)} {s['wk']:.1f}"))
            used.add(best["id"])
    return out


def bench_option(d, starter, is_locked, byes):
    cands = [b for b in d["mine"] if b["slot"] == BENCH and starter["slot"] in b["elig"] and not is_locked(b)
             and b["status"] not in BAD_FOR_LINEUP and b["team"] not in byes and b["wk"] > 0]
    return max(cands, key=lambda b: b["wk"], default=None)


# ================= digest =================

def pickup_rows(d, is_locked):
    """{section: [(pos, sortval, key, value, text)]}"""
    out = {"week": [], "ros": [], "hot": []}
    for a in d["avail"]:
        w = worst(d["mine"], a["pos"], "wk", skip_zero=True)
        if w and not is_locked(a) and a["wk"] >= w["wk"] + WEEKLY_MARGIN:
            diff = a["wk"] - w["wk"]
            out["week"].append((a["pos"], diff, f"wk:{a['id']}", round(a["wk"], 1),
                                f"{short(a)} {a['wk']:.1f} (+{diff:.1f} vs {short(w)})"))
        r = worst(d["mine"], a["pos"], "ros")
        if r and a["ros"] >= r["ros"] + SEASON_MARGIN:
            diff = a["ros"] - r["ros"]
            out["ros"].append((a["pos"], diff, f"ros:{a['id']}", round(a["ros"]),
                               f"{short(a)} {a['ros']:.0f} (+{diff:.0f} vs {short(r)})"))
    listed = {row[2].split(":", 1)[1] for sec in ("week", "ros") for row in out[sec]}
    for a in d["avail"]:
        r = worst(d["mine"], a["pos"], "ros")
        relevant = not r or a["ros"] >= TRENDING_MIN_ROS_PCT * r["ros"]
        if a["adds"] >= TRENDING_MIN_ADDS and relevant and a["id"] not in listed:
            out["hot"].append((a["pos"], a["adds"], f"hot:{a['id']}", None,
                               f"{short(a)} {a['adds'] / 1000:.1f}k adds, wk {a['wk']:.1f}"))
    return out


def build_digest(d, lineup, rows, last_items, full, label):
    """Returns (title, body, current_items) or None if a routine digest has nothing new."""
    current = {k: None for k, _ in lineup}
    for sec in rows.values():
        for _, _, key, val, _ in sec:
            current[key] = val

    def is_new(key, val):
        if full or key not in last_items:
            return True
        old = last_items[key]
        return val is not None and old is not None and abs(val - old) >= DIGEST_CHANGE_MIN

    lines, new_pickups, hidden = [], 0, 0
    if lineup:
        lines.append("⚠️ LINEUP")
        lines += [f"  {text}" for _, text in lineup]
        lines.append("")

    for title, sec in (("📋 THIS WEEK", "week"), ("📅 REST OF SEASON", "ros"), ("🔥 TRENDING", "hot")):
        shown = []
        for pos in POSITIONS:
            group = sorted((r for r in rows[sec] if r[0] == pos), key=lambda r: r[1], reverse=True)[:DIGEST_PER_POSITION]
            for _, _, key, val, text in group:
                if is_new(key, val):
                    shown.append(f"  {pos}  {text}")
                else:
                    hidden += 1
        if shown:
            lines.append(title)
            lines += shown
            lines.append("")
            new_pickups += len(shown)

    if not full and not lineup and new_pickups == 0:
        return None
    if hidden:
        lines.append(f"({hidden} unchanged since last digest)")
    if not lines:
        lines = ["Lineup is set and nothing on the wire beats your roster."]

    parts = [f"{d['name']} · Wk {d['week']}"]
    if label:
        parts.append(label)
    counts = []
    if lineup:
        counts.append(f"{len(lineup)} lineup")
    if new_pickups:
        counts.append(f"{new_pickups} {'pickups' if full else 'new'}")
    if counts:
        parts.append(", ".join(counts))
    return " · ".join(parts), "\n".join(lines).strip(), current


def digest_due(now, waiver_days):
    """(kind, full, label) or None."""
    if now.weekday() == 6 and now.hour == SUNDAY_FULL_HOUR:
        return "sunday", True, "Game day"
    for day in waiver_days:
        if now.weekday() == (DAYS.index(day) - 1) % 7 and now.hour == PREWAIVER_HOUR:
            return "waivers", True, "Waivers tonight"
    if now.hour in DAILY_DIGEST_HOURS:
        return "daily", False, None
    return None


# ================= immediate events =================

def ev(kind, title, text, prio):
    return {"kind": kind, "title": title, "text": text, "prio": prio}


def detect_events(d, ls, is_locked, byes):
    """Compares this run to league state; returns events and mutates ls baselines/status maps."""
    new_week = ls.get("week") != d["week"]
    first_run = "status" not in ls
    if new_week:
        ls["base_wk"], ls["base_ros"] = {}, {}
    base_wk, base_ros = ls.setdefault("base_wk", {}), ls.setdefault("base_ros", {})
    prev_status = ls.get("status", {})
    prev_tag = ls.get("avail_tag", {})
    prev_rostered = set(ls.get("rostered", []))
    events = []

    for m in d["mine"]:
        pid = m["id"]
        if is_locked(m):
            continue
        old = prev_status.get(pid)
        status_changed = not first_run and old is not None and old != m["status"]
        if status_changed:
            text = f"{m['pos']} {short(m)}: {old} → {m['status']}"
            starter = m["slot"] not in (BENCH, IR)
            if starter and m["status"] in BAD_FOR_LINEUP:
                alt = bench_option(d, m, is_locked, byes)
                text += f". Start {short(alt)} {alt['wk']:.1f}" if alt else ". No bench option"
            kind = "healthy" if m["status"] == "Healthy" else "injury"
            events.append(ev(kind, f"{last_name(m)} {m['status']}", text, 4 if m["status"] in BAD_FOR_LINEUP else 3))

        bw = base_wk.get(pid)
        if bw is None:
            base_wk[pid] = m["wk"]
        else:
            diff = m["wk"] - bw
            if abs(diff) >= ROSTER_WK_PTS and (bw == 0 or abs(diff) >= ROSTER_WK_PCT * bw):
                if not status_changed:  # the injury alert already explains this move
                    kind = "up" if diff > 0 else "down"
                    events.append(ev(kind, f"{last_name(m)} {'up' if diff > 0 else 'down'} this week",
                                     f"{m['pos']} {short(m)}: this week {bw:.1f} → {m['wk']:.1f}", 3))
                base_wk[pid] = m["wk"]

        br = base_ros.get(pid)
        if br is None:
            base_ros[pid] = m["ros"]
        else:
            diff = m["ros"] - br
            if abs(diff) >= ROSTER_ROS_PTS and (br == 0 or abs(diff) >= ROSTER_ROS_PCT * br):
                kind = "up" if diff > 0 else "down"
                events.append(ev(kind, f"{last_name(m)} ROS {'up' if diff > 0 else 'down'}",
                                 f"{m['pos']} {short(m)}: rest of season {br:.0f} → {m['ros']:.0f}", 3))
                base_ros[pid] = m["ros"]

    for a in d["avail"]:
        pid = a["id"]
        if is_locked(a):
            continue
        bw = base_wk.get(pid)
        if bw is None:
            base_wk[pid] = a["wk"]
        elif a["wk"] - bw >= AVAIL_WK_UP and a["wk"] >= AVAIL_WK_FLOOR:
            w = worst(d["mine"], a["pos"], "wk", skip_zero=True)
            ctx = f" (your worst {a['pos']}: {short(w)} {w['wk']:.1f})" if w else ""
            events.append(ev("rise", f"{last_name(a)} rising",
                             f"{a['pos']} {short(a)}: this week {bw:.1f} → {a['wk']:.1f}{ctx}", 3))
            base_wk[pid] = a["wk"]

        br = base_ros.get(pid)
        if br is None:
            base_ros[pid] = a["ros"]
        elif a["ros"] - br >= AVAIL_ROS_UP:
            r = worst(d["mine"], a["pos"], "ros")
            ctx = f" (your worst {a['pos']}: {short(r)} {r['ros']:.0f})" if r else ""
            events.append(ev("rise", f"{last_name(a)} ROS rising",
                             f"{a['pos']} {short(a)}: rest of season {br:.0f} → {a['ros']:.0f}{ctx}", 3))
            base_ros[pid] = a["ros"]

        was_held = prev_tag.get(pid) != "FA" and (pid in prev_rostered or prev_tag.get(pid) == "W")
        if not first_run and a["tag"] == "FA" and was_held:
            w = worst(d["mine"], a["pos"], "wk", skip_zero=True)
            r = worst(d["mine"], a["pos"], "ros")
            beats = (w and a["wk"] >= w["wk"] + WEEKLY_MARGIN) or (r and a["ros"] >= r["ros"] + SEASON_MARGIN)
            if beats or a["own"] >= NOTABLE_OWN_PCT:
                how = "cleared waivers" if prev_tag.get(pid) == "W" else "was dropped"
                events.append(ev("fa", f"{last_name(a)} free agent",
                                 f"{a['pos']} {short(a)} {how}, free agent now. "
                                 f"Wk {a['wk']:.1f}, ROS {a['ros']:.0f}, {a['own']:.0f}% rostered", 4))

    ls["week"] = d["week"]
    ls["status"] = {m["id"]: m["status"] for m in d["mine"]}
    ls["avail_tag"] = {a["id"]: a["tag"] for a in d["avail"]}
    ls["rostered"] = sorted(d["rostered"])
    # keep baselines only for players still in view
    seen = {p["id"] for p in d["mine"] + d["avail"]}
    ls["base_wk"] = {k: v for k, v in base_wk.items() if k in seen}
    ls["base_ros"] = {k: v for k, v in base_ros.items() if k in seen}
    return events


def send_events(d, events):
    if not events:
        return
    top = max(events, key=lambda e: e["prio"])
    emoji, tag = EMOJI[top["kind"]]
    title = f"{d['name']}: {events[0]['title']}" if len(events) == 1 else f"{d['name']}: {len(events)} updates"
    body = "\n".join(f"{EMOJI[e['kind']][0]} {e['text']}" for e in sorted(events, key=lambda e: -e["prio"]))
    notify(title, body, priority=top["prio"], tags=[tag], actions=buttons(d), click=d["team_url"])


# ================= main =================

def main():
    s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
    if not s2 or not swid:
        sys.exit("Set ESPN_S2 and ESPN_SWID.")
    if not DRY_RUN and not os.environ.get("NTFY_TOPIC"):
        sys.exit("Set NTFY_TOPIC (or DRY_RUN=1).")
    cookies = {"espn_s2": s2, "SWID": swid}

    state = load_json(STATE_FILE, {})
    if state.get("version") != STATE_VERSION:
        state = {"version": STATE_VERSION, "leagues": {}, "schedule": {}, "auth_alert_day": None}
    now = datetime.now(TZ)
    now_ts = time.time()
    quiet = (now.hour >= QUIET_START or now.hour < QUIET_END) and not FORCE_DIGEST

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
                       "Grab fresh espn_s2 and SWID cookies and update the GitHub secrets.",
                       priority=4, tags=["key"])
                state["auth_alert_day"] = f"{now:%Y-%m-%d}"
            continue
        except Exception as ex:
            print(f"League {lid} failed: {ex}")
            continue

        kick, byes = schedule(state, d["week"], cookies)

        def is_locked(p, kick=kick):
            k = kick.get(p["team"])
            return k is not None and k <= now_ts

        ls = state["leagues"].setdefault(lid, {})

        # immediate pushes
        events = detect_events(d, ls, is_locked, byes)
        queue = ls.get("queue", []) + events
        if quiet:
            ls["queue"] = queue
        else:
            send_events(d, queue)
            ls["queue"] = []

        # digest
        due = ("forced", True, "Full check") if FORCE_DIGEST else digest_due(now, d["waiver_days"])
        slot = f"{now:%Y-%m-%d}-{now.hour}"
        if due and (FORCE_DIGEST or slot not in ls.get("digests_sent", [])):
            _, full, label = due
            lineup = lineup_issues(d, is_locked, byes)
            built = build_digest(d, lineup, pickup_rows(d, is_locked), ls.get("last_digest", {}), full, label)
            if built:
                title, body, current = built
                prio = 4 if lineup else 3
                notify(title, body, priority=prio, tags=["warning" if lineup else "football"],
                       actions=buttons(d), click=d["fa_url"])
                ls["last_digest"] = current
            if not FORCE_DIGEST:
                ls["digests_sent"] = (ls.get("digests_sent", []) + [slot])[-30:]

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
