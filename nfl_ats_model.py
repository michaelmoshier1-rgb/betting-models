#!/usr/bin/env python3
"""
NFL ATS Mean-Reversion Model  (2026 season)
-------------------------------------------
Run it any day:   python3 nfl_ats_model.py
It will:
  1. Pull final scores + DraftKings spreads from ESPN's free public API (no key)
     for every week not already saved, plus the current and next week's lines.
  2. Save everything to nfl_ats_data.json next to this script.
  3. Rebuild each team's running ATS metrics using ONLY games from earlier weeks
     (no look-ahead), size bets, grade past bets, write nfl_ats_model.html and
     open it in your browser.

nfl_spreads.csv (included) seeds Weeks 1-10 so the first run is fast.

Flags:  --offline   rebuild the page from saved data, no network
        --refetch   re-pull every week from ESPN
        --no-open   don't open the browser
        --date YYYY-MM-DD   treat this date as "today"
Only the Python standard library is used (Python 3.8+).
"""
import csv, json, math, os, sys, time, statistics, webbrowser, urllib.request
from datetime import date, datetime, timedelta, timezone

# ============================== CONFIG ==================================
SEASON_YEAR = 2026
WEEK1_START = date(2026, 9, 9)   # NFL weeks here run Wednesday -> Tuesday
REG_SEASON_WEEKS = 18
BOOK = "DraftKings"
JUICE = -110

MIN_GAMES = 4        # graded games a team needs before it can drive a bet
ATS_SD = None        # spread of single-game ATS margins (pts); None = measure from data
Z_OUTLIER = 2.0      # |Vegas-error z| at/above this may mean the market truly misjudges the team...
RECENT_N = 3         # ...if its last-N-games avg error agrees...
RECENT_CONFIRM = 3.0 # ...by at least this many points -> RIDE it instead of fading
FADE_WEIGHT = 1.0
RIDE_WEIGHT = 0.5
UNIT_TIERS = [(2.5, 3.0), (1.75, 2.0), (1.0, 1.0)]  # (|signal| at least, units)
# ========================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "nfl_ats_data.json")
SEED_CSV = os.path.join(HERE, "nfl_spreads.csv")
OUT_HTML = os.path.join(HERE, "nfl_ats_model.html")
TEMPLATE = os.path.join(HERE, "nfl_ats_template.html")

TEAMS = ["ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN","DET","GB","HOU","IND","JAX","KC",
         "LV","LAC","LAR","MIA","MIN","NE","NO","NYG","NYJ","PHI","PIT","SF","SEA","TB","TEN","WSH"]
ESPN_ABBR = {"WAS":"WSH","LA":"LAR","JAC":"JAX","OAK":"LV","SD":"LAC","GNB":"GB","KAN":"KC","NWE":"NE","NOR":"NO","SFO":"SF","TAM":"TB"}
SB = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week={}&dates=" + str(SEASON_YEAR)
SUM = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={}"
WIN_MULT = 100 / abs(JUICE) if JUICE < 0 else JUICE / 100
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))


def norm(a):
    a = (a or "").upper()
    return ESPN_ABBR.get(a, a)


def week_of(d):
    return (d - WEEK1_START).days // 7 + 1


def week_range(w):
    s = WEEK1_START + timedelta(days=7 * (w - 1))
    return s, s + timedelta(days=6)


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nfl-ats-model)"})
            with urllib.request.urlopen(req, timeout=25) as r:
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
    """(home line: close if available else current, home open line, provider) from ESPN pickcenter/odds."""
    if not pc_list:
        return None, None, None
    pc = next((p for p in pc_list if BOOK.lower() in str((p.get("provider") or {}).get("name", "")).lower()), pc_list[0])
    prov = (pc.get("provider") or {}).get("name")
    h = (pc.get("pointSpread") or {}).get("home") or {}
    pick = lambda k: to_num((h.get(k) or {}).get("line")) if isinstance(h.get(k), dict) else None
    close, cur, opn = pick("close"), pick("current"), pick("open")
    line = close if close is not None else cur
    if line is None:
        det = str(pc.get("details") or "").strip()
        parts = det.split()
        if len(parts) == 2:
            t, v = norm(parts[0]), to_num(parts[1])
            if v is not None:
                line = v if t == home else -v if t == away else None
        elif det.upper() in ("EVEN", "PK"):
            line = 0.0
    if line is None and pc.get("spread") is not None:
        line = to_num(pc.get("spread"))
    return line, opn, prov


def gkey(d, a, b):
    t = sorted([a, b])
    return f"{d}_{t[0]}_{t[1]}"


def load_store():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"games": {}, "done_weeks": []}


def save_store(st):
    with open(DATA_FILE, "w") as f:
        json.dump(st, f, indent=1)


def seed_from_csv(st):
    if not os.path.exists(SEED_CSV) or st["games"]:
        return
    with open(SEED_CSV) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        d = r["Date"][:10]
        st["games"][gkey(d, r["Away"], r["Home"])] = dict(
            date=d, week=week_of(date.fromisoformat(d)), away=r["Away"], home=r["Home"], status="final",
            home_line=float(r["Home Line"]), open_line=None, home_margin=float(r["Home Margin"]),
            away_score=None, home_score=None, time=None, neutral=r.get("Neutral") == "Y", src=r.get("Line Source", "seed"))
    # weeks where every team is accounted for (played or bye) are complete in the seed
    st["done_weeks"] = list(range(1, 11))
    print(f"Seeded {len(rows)} games from {os.path.basename(SEED_CSV)}")


def fetch_week(st, w, today):
    data = get_json(SB.format(w))
    if data is None:
        return False
    all_final = True
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        home = next((norm(c["team"]["abbreviation"]) for c in cs if c.get("homeAway") == "home"), None)
        away = next((norm(c["team"]["abbreviation"]) for c in cs if c.get("homeAway") == "away"), None)
        if home not in TEAMS or away not in TEAMS:
            continue
        hs = next((to_num(c.get("score")) for c in cs if c.get("homeAway") == "home"), None)
        as_ = next((to_num(c.get("score")) for c in cs if c.get("homeAway") == "away"), None)
        stype = (comp.get("status") or ev.get("status") or {}).get("type") or {}
        status = "final" if stype.get("completed") else ("live" if stype.get("state") == "in" else "pre")
        if stype.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            status = "ppd"
        if status not in ("final", "ppd"):
            all_final = False
        try:
            kt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        d = kt.date().isoformat()
        key = gkey(d, away, home)
        old = st["games"].get(key, {})
        old_line = old.get("home_line")
        if old_line is not None and old.get("home") and old.get("home") != home:
            old_line = -old_line  # seed had the neutral-site teams the other way round
        line, opn, prov = old_line, old.get("open_line"), old.get("src")
        if line is None or status != "final" or old.get("status") != "final":
            summ = get_json(SUM.format(ev["id"]))
            l2, o2, p2 = parse_line((summ or {}).get("pickcenter"), home, away)
            if l2 is None:
                l2, o2, p2 = parse_line(comp.get("odds"), home, away)
            if l2 is not None:
                line, opn, prov = l2, o2, f"ESPN/{p2 or BOOK}"
            time.sleep(0.15)
        st["games"][key] = dict(date=d, week=w, away=away, home=home, status=status, home_line=line, open_line=opn,
                                home_margin=(hs - as_) if status == "final" and hs is not None and as_ is not None else None,
                                away_score=as_, home_score=hs, time=kt.strftime("%a %-I:%M %p"), tip_iso=ev.get("date"),
                                neutral=bool(comp.get("neutralSite")), src=prov or old.get("src"), espn_id=ev.get("id"))
    if all_final and week_range(w)[1] < today:
        st["done_weeks"] = sorted(set(st["done_weeks"]) | {w})
    return True


def update_data(st, today, refetch):
    if refetch:
        st["done_weeks"] = []
    last = min(REG_SEASON_WEEKS, max(1, week_of(today) + 1))  # current week + next week's early lines
    todo = [w for w in range(1, last + 1) if w not in st["done_weeks"]]
    print(f"Fetching {len(todo)} week(s) from ESPN...")
    for w in todo:
        print(f"  Week {w}", end="", flush=True)
        ok = fetch_week(st, w, today)
        n = sum(1 for g in st["games"].values() if g["week"] == w)
        print(f"  -> {n} games" if ok else "  -> failed (kept old data)")
        save_store(st)


# ------------------------------- MODEL ----------------------------------
def team_metrics(h, sd):
    n = len(h)
    if n == 0:
        return dict(gp=0, w=0, l=0, p=0, cover=None, err=None, recent=None, zc=None, ze=None, comb=None)
    w = sum(a > 0 for a in h); l = sum(a < 0 for a in h); p = n - w - l
    err = sum(h) / n
    rc = h[-RECENT_N:]; recent = sum(rc) / len(rc)
    dec = w + l
    zc = (w - l) / math.sqrt(dec) if dec else 0.0
    ze = err / (sd / math.sqrt(n))
    return dict(gp=n, w=w, l=l, p=p, cover=(w / dec if dec else None), err=err, recent=recent, zc=zc, ze=ze, comb=(zc + ze) / 2)


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


def snap(mets):
    out = []
    for t in TEAMS:
        s = stance(mets[t])
        out.append(dict(team=t, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in mets[t].items()},
                        stance=s[0], contrib=round(s[1], 3)))
    return out


def run_model(st, today):
    games = sorted(st["games"].values(), key=lambda g: (g["week"], g.get("tip_iso") or g["date"], g["away"]))
    graded = [g for g in games if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None]
    sd = ATS_SD
    if sd is None:
        ats = [g["home_margin"] + g["home_line"] for g in graded]
        sd = statistics.pstdev(ats + [-a for a in ats]) if len(ats) > 30 else 13.0
    hist = {t: [] for t in TEAMS}
    out_games, snapshots = [], {}
    for w in sorted({g["week"] for g in games}):
        wk = [g for g in games if g["week"] == w]
        mets = {t: team_metrics(hist[t], sd) for t in TEAMS}
        st_ = {t: stance(mets[t]) for t in TEAMS}
        snapshots[str(w)] = snap(mets)
        for g in wk:
            sig = st_[g["home"]][1] - st_[g["away"]][1]
            u = units_for(sig) if g["home_line"] is not None and g["status"] != "ppd" else 0.0
            pick = (g["home"] if sig > 0 else g["away"]) if u else None
            pick_line = (g["home_line"] if pick == g["home"] else -g["home_line"]) if pick else None
            res, pl, cover = None, 0.0, None
            if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None:
                a = g["home_margin"] + g["home_line"]
                cover = g["home"] if a > 0 else g["away"] if a < 0 else "PUSH"
                if pick:
                    res = "P" if a == 0 else ("W" if cover == pick else "L")
                    pl = u * WIN_MULT if res == "W" else (-u if res == "L" else 0.0)
            out_games.append(dict(g, signal=round(sig, 3), units=u, pick=pick, pick_line=pick_line, result=res,
                                  pl=round(pl, 3), cover=cover, home_stance=st_[g["home"]][0], away_stance=st_[g["away"]][0]))
        for g in wk:
            if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None:
                a = g["home_margin"] + g["home_line"]
                hist[g["home"]].append(a); hist[g["away"]].append(-a)
    snapshots["latest"] = snap({t: team_metrics(hist[t], sd) for t in TEAMS})
    return out_games, snapshots, sd


def weekly_table(out_games):
    wks = {}
    for g in out_games:
        if not g["pick"]:
            continue
        r = wks.setdefault(g["week"], dict(week=g["week"], bets=0, w=0, l=0, p=0, pending=0, risked=0.0, pl=0.0))
        r["bets"] += 1
        k = {"W": "w", "L": "l", "P": "p"}.get(g["result"], "pending")
        r[k] += 1
        if g["result"] in ("W", "L"):
            r["risked"] += g["units"]; r["pl"] += g["pl"]
    rows, cum, cum_r = [], 0.0, 0.0
    for w in sorted(wks):
        r = wks[w]; cum += r["pl"]; cum_r += r["risked"]
        r.update(cum=round(cum, 3), cum_roi=round(100 * cum / cum_r, 2) if cum_r else None, pl=round(r["pl"], 3))
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
    weekly = weekly_table(out_games)
    weeks = sorted({g["week"] for g in out_games})
    cur = week_of(today)
    # open on the earliest week that still has games to play, else the latest week
    pending = [g["week"] for g in out_games if g["status"] != "final" and g["week"] >= cur]
    open_week = min(pending) if pending else (max(weeks) if weeks else 1)
    labels = {str(w): f"Week {w} · {week_range(w)[0].strftime('%b %-d')}–{week_range(w)[1].strftime('%b %-d')}" for w in weeks}
    payload = dict(generated=datetime.now().strftime("%a %b %-d, %Y %-I:%M %p"), today=today.isoformat(), open_week=open_week,
                   week_labels=labels, games=out_games, snapshots=snaps, weekly=weekly, sd=round(sd, 2), win_mult=WIN_MULT,
                   config=dict(MIN_GAMES=MIN_GAMES, Z_OUTLIER=Z_OUTLIER, RECENT_N=RECENT_N, RECENT_CONFIRM=RECENT_CONFIRM,
                               FADE_WEIGHT=FADE_WEIGHT, RIDE_WEIGHT=RIDE_WEIGHT, UNIT_TIERS=UNIT_TIERS, JUICE=JUICE))
    with open(TEMPLATE) as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(payload))
    with open(OUT_HTML, "w") as f:
        f.write(html)
    w = sum(r["w"] for r in weekly); l = sum(r["l"] for r in weekly)
    pl = sum(r["pl"] for r in weekly); risk = sum(r["risked"] for r in weekly)
    print(f"\nSeason: {w}-{l}, {pl:+.2f}u, ROI {100*pl/risk if risk else 0:.1f}%")
    for g in [g for g in out_games if g["week"] in (open_week, open_week + 1) and g["pick"] and not g["result"]]:
        print(f"   Week {g['week']}: {g['pick']} {g['pick_line']:+g}  ({g['away']} @ {g['home']}, {g.get('time') or g['date']})  {g['units']:g}u"
              )
    print(f"Wrote {OUT_HTML}")
    if "--no-open" not in args:
        webbrowser.open("file://" + OUT_HTML)
    return payload


if __name__ == "__main__":
    main()
