#!/usr/bin/env python3
"""
NHL Moneyline Mean-Reversion Model  (2026-27 season)
----------------------------------------------------
Run once a day:   python3 nhl_ml_model.py

Same idea as the spread models, but on moneylines:
  * "Vegas error" for a team = how often it actually wins minus how often the
    closing moneyline said it should (no-vig implied probability).
    A -150 favourite (~58%) that wins 7 of 10 beat its price by about +12 pts.
  * "ML units" = profit from betting 1 unit on the team every game at its price.
  * Teams that have beaten (or missed) their prices are faded (or bought) on
    the expectation of reversion, unless the miss is extreme and still showing
    up recently, in which case the model rides it ("misjudged").
Bets are moneyline bets. Units = amount RISKED; a win pays at the listed price.

It pulls final scores + DraftKings moneylines from ESPN's free public API (no
key), saves them to nhl_ml_data.json, uses only games played before each day
for that day's ratings, writes nhl_ml_model.html and opens it.
The first run pulls the whole season so far (a couple of minutes).

Flags:  --offline  --refetch  --no-open  --date YYYY-MM-DD
Only the Python standard library is used (Python 3.8+).
"""
import json, math, os, sys, time, webbrowser, urllib.request
from datetime import date, datetime, timedelta, timezone

# ============================== CONFIG ==================================
SEASON_START = date(2026, 10, 1)   # preseason games are skipped automatically
BOOK = "DraftKings"

MIN_GAMES = 8          # games with a moneyline a team needs before it can drive a bet
Z_OUTLIER = 2.0        # |Vegas-error z| at/above this may mean the market truly misjudges the team...
RECENT_N = 8           # ...if its last-N-games error agrees...
RECENT_CONFIRM = 0.10  # ...by at least this much (0.10 = 10 pts of win probability) -> RIDE it
FADE_WEIGHT = 1.0
RIDE_WEIGHT = 0.5
UNIT_TIERS = [(2.5, 3.0), (1.75, 2.0), (1.0, 1.0)]  # (|signal| at least, units risked)
MAX_FAV = -250         # never bet a favourite shorter than this (too much risk for the edge)
WORKERS = 8
# ========================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "nhl_ml_data.json")
OUT_HTML = os.path.join(HERE, "nhl_ml_model.html")
TEMPLATE = os.path.join(HERE, "nhl_ml_template.html")
SB = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={}&limit=100"
SUM = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={}"
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nhl-ml-model)"})
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
        if s in ("EVEN", "EV"):
            return 100.0
        return float(s.replace("+", ""))
    except Exception:
        return None


def dec(ml):            # American -> decimal odds
    return 1 + (ml / 100 if ml > 0 else 100 / abs(ml))


def implied(ml):        # American -> raw implied probability (with vig)
    return 100 / (ml + 100) if ml > 0 else abs(ml) / (abs(ml) + 100)


def parse_ml(pc_list):
    """(home_ml, away_ml, home_open, away_open, provider) from an ESPN pickcenter/odds list."""
    if not pc_list:
        return None, None, None, None, None
    pc = next((p for p in pc_list if BOOK.lower() in str((p.get("provider") or {}).get("name", "")).lower()), pc_list[0])
    prov = (pc.get("provider") or {}).get("name")
    mlo = pc.get("moneyline") or {}

    def side(k):
        s = mlo.get(k) or {}
        c = to_num((s.get("close") or {}).get("odds")) if isinstance(s.get("close"), dict) else None
        cur = to_num((s.get("current") or {}).get("odds")) if isinstance(s.get("current"), dict) else None
        o = to_num((s.get("open") or {}).get("odds")) if isinstance(s.get("open"), dict) else None
        return (c if c is not None else cur), o
    h, ho = side("home")
    a, ao = side("away")
    if h is None:
        h = to_num((pc.get("homeTeamOdds") or {}).get("moneyLine"))
    if a is None:
        a = to_num((pc.get("awayTeamOdds") or {}).get("moneyLine"))
    if h is None or a is None or h == 0 or a == 0:
        return None, None, None, None, prov
    return h, a, ho, ao, prov


def load_store():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"games": {}, "done_dates": []}


def save_store(st):
    with open(DATA_FILE, "w") as f:
        json.dump(st, f)


def fetch_lines(jobs):
    from concurrent.futures import ThreadPoolExecutor

    def one(job):
        key, eid, odds = job
        r = parse_ml((get_json(SUM.format(eid)) or {}).get("pickcenter"))
        if r[0] is None:
            r = parse_ml(odds)
        return key, r
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return dict(ex.map(one, jobs))


def fetch_day(st, d, today):
    data = get_json(SB.format(d.strftime("%Y%m%d")))
    if data is None:
        return False
    all_final, jobs, staged = True, [], {}
    for ev in data.get("events", []):
        if ((ev.get("season") or {}).get("type")) not in (2, 3):
            continue  # preseason / All-Star
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        H = next((c for c in cs if c.get("homeAway") == "home"), None)
        A = next((c for c in cs if c.get("homeAway") == "away"), None)
        if not H or not A:
            continue
        home, away = H["team"]["abbreviation"].upper(), A["team"]["abbreviation"].upper()
        hs, as_ = to_num(H.get("score")), to_num(A.get("score"))
        stype = (comp.get("status") or ev.get("status") or {}).get("type") or {}
        status = "final" if stype.get("completed") else ("live" if stype.get("state") == "in" else "pre")
        if stype.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            status = "ppd"
        if status not in ("final", "ppd"):
            all_final = False
        key = str(ev["id"])
        old = st["games"].get(key, {})
        try:
            tip = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(ET).strftime("%-I:%M %p")
        except Exception:
            tip = None
        detail = stype.get("shortDetail") or ""
        staged[key] = dict(date=d.isoformat(), away=away, home=home, status=status,
                           home_ml=old.get("home_ml"), away_ml=old.get("away_ml"),
                           home_open=old.get("home_open"), away_open=old.get("away_open"),
                           home_score=hs, away_score=as_, ot=("OT" in detail or "SO" in detail) if status == "final" else False,
                           time=tip, tip_iso=ev.get("date"), src=old.get("src"), line_done=old.get("line_done", False))
        if not (status == "final" and old.get("status") == "final" and old.get("line_done")):
            jobs.append((key, key, comp.get("odds")))
    for key, (h, a, ho, ao, p) in fetch_lines(jobs).items():
        g = staged[key]
        if h is not None:
            g.update(home_ml=h, away_ml=a, home_open=ho, away_open=ao, src=f"ESPN/{p or BOOK}")
        if g["status"] == "final":
            g["line_done"] = True
    st["games"].update(staged)
    if all_final and d < today:
        st["done_dates"] = sorted(set(st["done_dates"]) | {d.isoformat()})
    return True


def update_data(st, today, refetch):
    if refetch:
        st["done_dates"] = []
        for g in st["games"].values():
            g["line_done"] = False
    d, todo = SEASON_START, []
    while d <= today:
        if d.isoformat() not in st["done_dates"]:
            todo.append(d)
        d += timedelta(days=1)
    print(f"Fetching {len(todo)} day(s) from ESPN (first run takes a couple of minutes)...")
    for d in todo:
        print(f"  {d}", end="", flush=True)
        ok = fetch_day(st, d, today)
        day = [g for g in st["games"].values() if g["date"] == d.isoformat()]
        print(f"  -> {len(day)} games, {sum(g['home_ml'] is not None for g in day)} with moneylines" if ok else "  -> failed (kept old data)")
        save_store(st)


# ------------------------------- MODEL ----------------------------------
def novig(g):
    ih, ia = implied(g["home_ml"]), implied(g["away_ml"])
    return ih / (ih + ia)


def team_metrics(h):
    """h = list of (won 0/1, fair win prob, decimal odds) from the team's side."""
    n = len(h)
    if n == 0:
        return dict(gp=0, w=0, l=0, winp=None, imp=None, err=None, recent=None, units=None, ze=None, zu=None, comb=None)
    w = sum(x[0] for x in h)
    resid = [x[0] - x[1] for x in h]
    err = sum(resid) / n
    rc = resid[-RECENT_N:]
    recent = sum(rc) / len(rc)
    var_p = sum(x[1] * (1 - x[1]) for x in h)
    ze = sum(resid) / math.sqrt(var_p) if var_p > 0 else 0.0
    pl = [(x[2] - 1) if x[0] else -1.0 for x in h]
    var_u = sum(x[1] * (x[2] - 1) ** 2 + (1 - x[1]) - (x[1] * (x[2] - 1) - (1 - x[1])) ** 2 for x in h)
    zu = sum(pl) / math.sqrt(var_u) if var_u > 0 else 0.0
    return dict(gp=n, w=w, l=n - w, winp=w / n, imp=sum(x[1] for x in h) / n, err=err, recent=recent,
                units=sum(pl), ze=ze, zu=zu, comb=(ze + zu) / 2)


def stance(m):
    if m["gp"] < MIN_GAMES:
        return "Too early", 0.0
    if abs(m["ze"]) >= Z_OUTLIER and m["recent"] * m["err"] > 0 and abs(m["recent"]) >= RECENT_CONFIRM:
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
    hist = {}
    H = lambda t: hist.setdefault(t, [])
    dates = sorted({g["date"] for g in games if g["date"] <= today.isoformat()})
    out, snaps = [], {}

    def snap():
        rows = []
        for t in sorted(hist):
            m = team_metrics(hist[t]); s_ = stance(m)
            rows.append(dict(team=t, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
                             stance=s_[0], contrib=round(s_[1], 3)))
        return rows

    for d in dates:
        day = [g for g in games if g["date"] == d]
        for g in day:
            H(g["home"]); H(g["away"])
        cache = {}

        def read(t):
            if t not in cache:
                m = team_metrics(H(t)); cache[t] = (m, stance(m))
            return cache[t]
        snaps[d] = snap()
        for g in day:
            (mh, sh), (ma, sa) = read(g["home"]), read(g["away"])
            sig = sh[1] - sa[1]
            has = g["home_ml"] is not None and g["away_ml"] is not None
            u = units_for(sig) if has and g["status"] != "ppd" else 0.0
            pick = (g["home"] if sig > 0 else g["away"]) if u else None
            price = (g["home_ml"] if pick == g["home"] else g["away_ml"]) if pick else None
            if pick and price is not None and price < 0 and price < MAX_FAV:
                pick, price, u = None, None, 0.0  # favourite too short to bet
            res, pl, winner = None, 0.0, None
            if g["status"] == "final" and g["home_score"] is not None and g["away_score"] is not None:
                winner = g["home"] if g["home_score"] > g["away_score"] else g["away"]
                if pick:
                    res = "W" if winner == pick else "L"
                    pl = u * (dec(price) - 1) if res == "W" else -u
            out.append(dict({k: v for k, v in g.items() if k != "line_done"}, signal=round(sig, 3), units=u, pick=pick,
                            price=price, result=res, pl=round(pl, 3), winner=winner, has_line=has,
                            fair_home=round(novig(g), 4) if has else None,
                            home_stance=sh[0], away_stance=sa[0]))
        for g in day:
            if g["status"] == "final" and g["home_ml"] is not None and g["away_ml"] is not None and g["home_score"] is not None:
                ph = novig(g)
                hw = 1 if g["home_score"] > g["away_score"] else 0
                H(g["home"]).append((hw, ph, dec(g["home_ml"])))
                H(g["away"]).append((1 - hw, 1 - ph, dec(g["away_ml"])))
    snaps["latest"] = snap()
    return out, snaps


def daily_table(out):
    days = {}
    for g in out:
        if not g["pick"]:
            continue
        r = days.setdefault(g["date"], dict(date=g["date"], bets=0, w=0, l=0, p=0, pending=0, risked=0.0, pl=0.0))
        r["bets"] += 1
        r[{"W": "w", "L": "l"}.get(g["result"], "pending")] += 1
        if g["result"] in ("W", "L"):
            r["risked"] += g["units"]; r["pl"] += g["pl"]
    rows, cum, cr = [], 0.0, 0.0
    for d in sorted(days):
        r = days[d]; cum += r["pl"]; cr += r["risked"]
        r.update(cum=round(cum, 3), cum_roi=round(100 * cum / cr, 2) if cr else None, pl=round(r["pl"], 3))
        rows.append(r)
    return rows


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    today = date.today()
    if "--date" in args:
        today = date.fromisoformat(args[args.index("--date") + 1])
    st = load_store()
    if "--offline" not in args:
        update_data(st, today, "--refetch" in args)
        save_store(st)
    out, snaps = run_model(st, today)
    daily = daily_table(out)
    payload = dict(generated=datetime.now().strftime("%a %b %-d, %Y %-I:%M %p"), today=today.isoformat(),
                   games=out, snapshots=snaps, daily=daily,
                   config=dict(MIN_GAMES=MIN_GAMES, Z_OUTLIER=Z_OUTLIER, RECENT_N=RECENT_N, RECENT_CONFIRM=RECENT_CONFIRM,
                               FADE_WEIGHT=FADE_WEIGHT, RIDE_WEIGHT=RIDE_WEIGHT, UNIT_TIERS=UNIT_TIERS, MAX_FAV=MAX_FAV))
    with open(TEMPLATE) as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(payload))
    with open(OUT_HTML, "w") as f:
        f.write(html)
    w = sum(r["w"] for r in daily); l = sum(r["l"] for r in daily)
    pl = sum(r["pl"] for r in daily); risk = sum(r["risked"] for r in daily)
    todays = [g for g in out if g["date"] == today.isoformat() and g["pick"]]
    print(f"\nSeason: {w}-{l}, {pl:+.2f}u, ROI {100*pl/risk if risk else 0:.1f}%   |   today: {len(todays)} bet(s)")
    for g in todays:
        print(f"   {g['pick']} {g['price']:+g}  ({g['away']} @ {g['home']}, {g.get('time') or ''})  {g['units']:g}u")
    print(f"Wrote {OUT_HTML}")
    if "--no-open" not in args:
        webbrowser.open("file://" + OUT_HTML)
    return payload


if __name__ == "__main__":
    main()
