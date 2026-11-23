#!/usr/bin/env python3
"""
NBA ATS Mean-Reversion Model  (2026-27 season)
------------------------------------------------
Run once a day:   python3 nba_ats_model.py
It will:
  1. Pull final scores + DraftKings spreads from ESPN's free public API
     (no key needed) for every day not already saved, plus today's slate.
  2. Save everything to nba_ats_data.json next to this script (so later runs
     only fetch new games).
  3. Rebuild every team's running ATS metrics using ONLY games played before
     each day (no look-ahead), size bets, grade past bets, and write
     nba_ats_model.html, then open it in your browser.

Optional: put nba_spreads.csv (the season spreadsheet export) next to this
script. It seeds history so the first run is fast and fills any game ESPN
has no line for.

Flags:
  --offline        don't hit the network; rebuild the page from saved data
  --refetch        re-pull every day from ESPN (ignores what's saved)
  --no-open        don't open the browser when done
  --date YYYY-MM-DD   treat this date as "today"
Only the Python standard library is used (Python 3.8+).
"""
import csv, json, math, os, sys, time, statistics, webbrowser, urllib.request
from datetime import date, datetime, timedelta, timezone

# ============================== CONFIG ==================================
SEASON_START = date(2026, 10, 20)
BOOK = "DraftKings"            # preferred line provider in ESPN pickcenter
JUICE = -110                   # price assumed on every bet (for units/ROI)

MIN_GAMES = 6        # a team needs this many graded games before it can drive a bet
ATS_SD = None        # spread of single-game ATS margins (pts); None = measure from data
Z_OUTLIER = 2.0      # |Vegas-error z| at/above this may mean the market truly misjudges the team...
LAST5_CONFIRM = 3.0  # ...and if its last-5 avg error is this big in the same direction -> RIDE it
FADE_WEIGHT = 1.0    # strength of mean-reversion fade on normal over/under-performers
RIDE_WEIGHT = 0.5    # strength of following a flagged "misjudged" outlier
UNIT_TIERS = [(2.5, 3.0), (1.75, 2.0), (1.0, 1.0)]  # (|signal| at least, units). Below lowest = pass
# ========================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "nba_ats_data.json")
SEED_CSV = os.path.join(HERE, "nba_spreads.csv")
OUT_HTML = os.path.join(HERE, "nba_ats_model.html")
TEMPLATE = os.path.join(HERE, "nba_ats_template.html")

TEAMS = ["ATL","BOS","BKN","CHA","CHI","CLE","DAL","DEN","DET","GSW","HOU","IND","LAC","LAL","MEM",
         "MIA","MIL","MIN","NOP","NYK","OKC","ORL","PHI","PHX","POR","SAC","SAS","TOR","UTA","WAS"]
ESPN_ABBR = {"GS":"GSW","NY":"NYK","SA":"SAS","NO":"NOP","UTAH":"UTA","WSH":"WAS","PHO":"PHX","BRK":"BKN","NOR":"NOP"}
SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}&limit=100"
SUM = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))
WIN_MULT = 100 / abs(JUICE) if JUICE < 0 else JUICE / 100


def norm(abbr):
    a = (abbr or "").upper()
    return ESPN_ABBR.get(a, a)


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nba-ats-model)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == tries - 1:
                print(f"   ! fetch failed: {url} ({e})")
                return None
            time.sleep(1.5 * (i + 1))


def to_num(x):
    try:
        s = str(x).strip().upper()
        if s in ("PK", "EVEN", "PICK"):
            return 0.0
        return float(s.replace("+", ""))
    except Exception:
        return None


def parse_line(pc_list, home, away):
    """Return (home_close_or_current_line, home_open_line, provider) from an ESPN pickcenter list."""
    if not pc_list:
        return None, None, None
    pc = next((p for p in pc_list if BOOK.lower() in str((p.get("provider") or {}).get("name", "")).lower()), pc_list[0])
    prov = (pc.get("provider") or {}).get("name")
    ps = pc.get("pointSpread") or {}
    h = ps.get("home") or {}
    close = to_num((h.get("close") or {}).get("line")) if isinstance(h.get("close"), dict) else None
    cur = to_num((h.get("current") or {}).get("line")) if isinstance(h.get("current"), dict) else None
    opn = to_num((h.get("open") or {}).get("line")) if isinstance(h.get("open"), dict) else None
    line = close if close is not None else cur
    if line is None:  # "details" like "MIA -4.5" is unambiguous about who's favored
        det = str(pc.get("details") or "").strip()
        parts = det.split()
        if len(parts) == 2:
            team, val = norm(parts[0]), to_num(parts[1])
            if val is not None:
                line = val if team == home else -val if team == away else None
        elif det.upper() in ("EVEN", "PK"):
            line = 0.0
    if line is None and pc.get("spread") is not None:  # ESPN 'spread' is the home team's number
        line = to_num(pc.get("spread"))
    return line, opn, prov


def load_store():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"games": {}, "done_dates": []}


def save_store(st):
    with open(DATA_FILE, "w") as f:
        json.dump(st, f, indent=1)


def seed_from_csv(st):
    if not os.path.exists(SEED_CSV) or st["games"]:
        return
    with open(SEED_CSV) as f:
        rows = list(csv.DictReader(f))
    dates = set()
    for r in rows:
        d = r["Date"][:10]
        key = f"{d}_{r['Away']}_{r['Home']}"
        hm = float(r["Home Margin"])
        st["games"][key] = dict(date=d, away=r["Away"], home=r["Home"], status="final",
                                home_line=float(r["Home Line"]), open_line=None, home_margin=hm,
                                away_score=None, home_score=None, time=None,
                                neutral=r.get("Neutral", "") == "Y", src=r.get("Line Source", "seed"))
        dates.add(d)
    st["done_dates"] = sorted(dates)
    print(f"Seeded {len(rows)} games from {os.path.basename(SEED_CSV)}")


def fetch_day(st, d, today):
    data = get_json(SB.format(d.strftime("%Y%m%d")))
    if data is None:
        return False
    all_final = True
    for ev in data.get("events", []):
        if ((ev.get("season") or {}).get("type")) == 1:  # preseason
            continue
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        home = next((norm(c["team"]["abbreviation"]) for c in cs if c.get("homeAway") == "home"), None)
        away = next((norm(c["team"]["abbreviation"]) for c in cs if c.get("homeAway") == "away"), None)
        if home not in TEAMS or away not in TEAMS:
            continue  # All-Star etc.
        hs = next((to_num(c.get("score")) for c in cs if c.get("homeAway") == "home"), None)
        as_ = next((to_num(c.get("score")) for c in cs if c.get("homeAway") == "away"), None)
        stype = (comp.get("status") or ev.get("status") or {}).get("type") or {}
        state = stype.get("state", "pre")
        status = "final" if stype.get("completed") else ("live" if state == "in" else "pre")
        if stype.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            status = "ppd"
        if status != "final" and status != "ppd":
            all_final = False
        key = f"{d.isoformat()}_{away}_{home}"
        old = st["games"].get(key, {})
        tip = None
        try:
            tip = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(ET).strftime("%-I:%M %p")
        except Exception:
            pass
        # line: completed games need the close once; today's/upcoming games refresh every run
        line, opn, prov = old.get("home_line"), old.get("open_line"), old.get("src")
        if line is None or status != "final" or old.get("status") != "final" or prov in (None, "seed"):
            summ = get_json(SUM.format(ev["id"]))
            l2, o2, p2 = parse_line((summ or {}).get("pickcenter"), home, away)
            if l2 is None:  # fall back to the scoreboard's odds block
                l2, o2, p2 = parse_line(comp.get("odds"), home, away)
            if l2 is not None:
                line, opn, prov = l2, o2, f"ESPN/{p2 or BOOK}"
            time.sleep(0.15)
        st["games"][key] = dict(date=d.isoformat(), away=away, home=home, status=status,
                                home_line=line, open_line=opn,
                                home_margin=(hs - as_) if status == "final" and hs is not None and as_ is not None else None,
                                away_score=as_, home_score=hs, time=tip, neutral=bool(comp.get("neutralSite")),
                                src=prov or old.get("src"), espn_id=ev.get("id"), tip_iso=ev.get("date"))
    if all_final and d < today:
        st["done_dates"] = sorted(set(st["done_dates"]) | {d.isoformat()})
    return True


def update_data(st, today, refetch):
    if refetch:
        st["done_dates"] = []
    d = SEASON_START
    todo = []
    while d <= today:
        if d.isoformat() not in st["done_dates"]:
            todo.append(d)
        d += timedelta(days=1)
    print(f"Fetching {len(todo)} day(s) from ESPN...")
    for d in todo:
        print(f"  {d}", end="", flush=True)
        ok = fetch_day(st, d, today)
        n = sum(1 for g in st["games"].values() if g["date"] == d.isoformat())
        print(f"  -> {n} games" if ok else "  -> failed (kept old data)")
        save_store(st)


# ------------------------------- MODEL ----------------------------------
def team_metrics(h, sd):
    """h = list of ATS margins from the team's point of view (+ = covered by that many)."""
    n = len(h)
    if n == 0:
        return dict(gp=0, w=0, l=0, p=0, cover=None, err=None, last5=None, zc=None, ze=None, comb=None)
    w = sum(a > 0 for a in h); l = sum(a < 0 for a in h); p = n - w - l
    err = sum(h) / n
    l5 = h[-5:]; last5 = sum(l5) / len(l5)
    dec = w + l
    zc = (w - l) / math.sqrt(dec) if dec else 0.0
    ze = err / (sd / math.sqrt(n))
    return dict(gp=n, w=w, l=l, p=p, cover=(w / dec if dec else None), err=err, last5=last5,
                zc=zc, ze=ze, comb=(zc + ze) / 2)


def stance(m):
    if m["gp"] < MIN_GAMES:
        return "Too early", 0.0
    if abs(m["ze"]) >= Z_OUTLIER and m["last5"] * m["err"] > 0 and abs(m["last5"]) >= LAST5_CONFIRM:
        return ("Misjudged (underrated) - RIDE" if m["err"] > 0 else "Misjudged (overrated) - FADE"), RIDE_WEIGHT * m["comb"]
    if abs(m["comb"]) >= 1.0:
        return ("Underrated so far - FADE (reversion)" if m["comb"] > 0 else "Overrated so far - BUY (reversion)"), -FADE_WEIGHT * m["comb"]
    return "Fairly priced", -FADE_WEIGHT * m["comb"]


def units_for(sig):
    for thr, u in UNIT_TIERS:
        if abs(sig) >= thr:
            return u
    return 0.0


def run_model(st, today):
    games = sorted(st["games"].values(), key=lambda g: (g["date"], g.get("tip_iso") or "", g["away"]))
    graded = [g for g in games if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None]
    sd = ATS_SD
    if sd is None:
        ats = [g["home_margin"] + g["home_line"] for g in graded]
        sd = statistics.pstdev(ats + [-a for a in ats]) if len(ats) > 30 else 13.5
    hist = {t: [] for t in TEAMS}
    dates = sorted({g["date"] for g in games if g["date"] <= today.isoformat()})
    out_games, snapshots = [], {}
    for d in dates:
        day = [g for g in games if g["date"] == d]
        mets = {t: team_metrics(hist[t], sd) for t in TEAMS}
        st_ = {t: stance(mets[t]) for t in TEAMS}
        snapshots[d] = [dict(team=t, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in mets[t].items()},
                             stance=st_[t][0], contrib=round(st_[t][1], 3)) for t in TEAMS]
        for g in day:
            sig = st_[g["home"]][1] - st_[g["away"]][1]
            u = units_for(sig) if g["home_line"] is not None and g["status"] != "ppd" else 0.0
            pick = (g["home"] if sig > 0 else g["away"]) if u else None
            pick_line = None
            if pick:
                pick_line = g["home_line"] if pick == g["home"] else -g["home_line"]
            res, pl, cover = None, 0.0, None
            if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None:
                a = g["home_margin"] + g["home_line"]
                cover = g["home"] if a > 0 else g["away"] if a < 0 else "PUSH"
                if pick:
                    if a == 0:
                        res = "P"
                    else:
                        res = "W" if cover == pick else "L"
                        pl = u * WIN_MULT if res == "W" else -u
            out_games.append(dict(g, signal=round(sig, 3), units=u, pick=pick, pick_line=pick_line,
                                  result=res, pl=round(pl, 3), cover=cover,
                                  home_stance=st_[g["home"]][0], away_stance=st_[g["away"]][0],
                                  home_comb=mets[g["home"]]["comb"], away_comb=mets[g["away"]]["comb"]))
        for g in day:  # add today's results to history only after the day's bets were set
            if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None:
                a = g["home_margin"] + g["home_line"]
                hist[g["home"]].append(a); hist[g["away"]].append(-a)
    # snapshot "after last games" for the ratings tab
    mets = {t: team_metrics(hist[t], sd) for t in TEAMS}
    snapshots["latest"] = [dict(team=t, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in mets[t].items()},
                                stance=stance(mets[t])[0], contrib=round(stance(mets[t])[1], 3)) for t in TEAMS]
    return out_games, snapshots, sd


def daily_table(out_games):
    days = {}
    for g in out_games:
        if not g["pick"]:
            continue
        r = days.setdefault(g["date"], dict(date=g["date"], bets=0, w=0, l=0, p=0, pending=0, risked=0.0, pl=0.0))
        r["bets"] += 1
        if g["result"] == "W": r["w"] += 1
        elif g["result"] == "L": r["l"] += 1
        elif g["result"] == "P": r["p"] += 1
        else: r["pending"] += 1
        if g["result"] in ("W", "L"):
            r["risked"] += g["units"]; r["pl"] += g["pl"]
    rows, cum, cum_r = [], 0.0, 0.0
    for d in sorted(days):
        r = days[d]; cum += r["pl"]; cum_r += r["risked"]
        r["cum"] = round(cum, 3); r["cum_roi"] = round(100 * cum / cum_r, 2) if cum_r else None
        r["pl"] = round(r["pl"], 3)
        rows.append(r)
    return rows


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    today = date.today()
    if "--date" in args:
        today = date.fromisoformat(args[args.index("--date") + 1])
    st = load_store()
    seed_from_csv(st)
    save_store(st)
    if "--offline" not in args:
        update_data(st, today, "--refetch" in args)
        save_store(st)
    out_games, snaps, sd = run_model(st, today)
    daily = daily_table(out_games)
    payload = dict(generated=datetime.now().strftime("%a %b %-d, %Y %-I:%M %p"), today=today.isoformat(),
                   games=out_games, snapshots=snaps, daily=daily, sd=round(sd, 2), win_mult=WIN_MULT,
                   config=dict(MIN_GAMES=MIN_GAMES, Z_OUTLIER=Z_OUTLIER, LAST5_CONFIRM=LAST5_CONFIRM,
                               FADE_WEIGHT=FADE_WEIGHT, RIDE_WEIGHT=RIDE_WEIGHT, UNIT_TIERS=UNIT_TIERS, JUICE=JUICE))
    with open(TEMPLATE) as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(payload))
    with open(OUT_HTML, "w") as f:
        f.write(html)
    w = sum(r["w"] for r in daily); l = sum(r["l"] for r in daily)
    pl = sum(r["pl"] for r in daily); risk = sum(r["risked"] for r in daily)
    todays = [g for g in out_games if g["date"] == today.isoformat() and g["pick"]]
    print(f"\nSeason: {w}-{l}, {pl:+.2f}u, ROI {100*pl/risk if risk else 0:.1f}%   |   today: {len(todays)} bet(s)")
    for g in todays:
        print(f"   {g['pick']} {g['pick_line']:+g}  ({g['away']} @ {g['home']})  {g['units']:g}u")
    print(f"Wrote {OUT_HTML}")
    if "--no-open" not in args:
        webbrowser.open("file://" + OUT_HTML)
    return payload


if __name__ == "__main__":
    main()
