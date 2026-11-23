#!/usr/bin/env python3
"""
College Football (FBS) ATS Mean-Reversion Model  (2026 season)
--------------------------------------------------------------
Run it any day:   python3 cfb_ats_model.py
It will:
  1. Pull final scores + DraftKings spreads from ESPN's free public API
     (no key needed) for every day not already saved, plus today's slate.
  2. Save everything to nba_ats_data.json next to this script (so later runs
     only fetch new games).
  3. Rebuild every team's running ATS metrics using ONLY games played before
     each day (no look-ahead), size bets, grade past bets, and write
     nba_ats_model.html, then open it in your browser.

Works week by week (ESPN's college football weeks, Mon-Sun, plus bowls).
The first run pulls the whole season so far (FBS games, a few minutes);
after that only open weeks are fetched. Games with no posted spread are
kept in the log but not rated or bet.

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
SEASON_YEAR = 2026
REG_WEEKS = 16      # ESPN regular-season weeks to scan (bowls are added after)
BOOK = "DraftKings"            # preferred line provider in ESPN pickcenter
JUICE = -110                   # price assumed on every bet (for units/ROI)

MIN_GAMES = 4        # a team needs this many graded games before it can drive a bet
ATS_SD = None        # spread of single-game ATS margins (pts); None = measure from data
Z_OUTLIER = 2.0      # |Vegas-error z| at/above this may mean the market truly misjudges the team...
RECENT_N = 3         # ...and if its last-N-games avg error...
LAST5_CONFIRM = 3.0  # ...is this big in the same direction -> RIDE it
FADE_WEIGHT = 1.0    # strength of mean-reversion fade on normal over/under-performers
RIDE_WEIGHT = 0.5    # strength of following a flagged "misjudged" outlier
UNIT_TIERS = [(2.5, 3.0), (1.75, 2.0), (1.0, 1.0)]  # (|signal| at least, units). Below lowest = pass
# ========================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "cfb_ats_data.json")
OUT_HTML = os.path.join(HERE, "cfb_ats_model.html")
TEMPLATE = os.path.join(HERE, "cfb_ats_template.html")
WORKERS = 8           # parallel downloads for game lines

SB = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?seasontype={}&week={}&dates=" + str(SEASON_YEAR) + "&groups=80&limit=500"
SUM = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={}"
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))
WIN_MULT = 100 / abs(JUICE) if JUICE < 0 else JUICE / 100


def norm(abbr):
    return (abbr or "").upper()


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cfb-ats-model)"})
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
    return {"games": {}, "teams": {}, "done_weeks": []}


def save_store(st):
    with open(DATA_FILE, "w") as f:
        json.dump(st, f)


def fetch_lines(jobs):
    """jobs: list of (key, event_id, home_abbr, away_abbr, comp_odds). Returns {key: (line, open, provider)}."""
    from concurrent.futures import ThreadPoolExecutor

    def one(job):
        key, eid, ha, aa, odds = job
        summ = get_json(SUM.format(eid))
        l, o, p = parse_line((summ or {}).get("pickcenter"), ha, aa)
        if l is None:
            l, o, p = parse_line(odds, ha, aa)
        return key, (l, o, p)

    out = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for key, res in ex.map(one, jobs):
            out[key] = res
    return out


def week_key(stype, wk):
    return "B" if stype == 3 else f"{wk:02d}"


def fetch_week(st, stype, wk, today, horizon):
    """Returns (ok, earliest_game_date or None)."""
    data = get_json(SB.format(stype, wk))
    if data is None:
        return False, None
    all_final, jobs, staged = True, [], {}
    wkey = week_key(stype, wk)
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        H = next((c for c in cs if c.get("homeAway") == "home"), None)
        A = next((c for c in cs if c.get("homeAway") == "away"), None)
        if not H or not A:
            continue
        for c in (H, A):
            t = c.get("team") or {}
            st["teams"][str(t.get("id"))] = dict(abbr=norm(t.get("abbreviation")),
                                                 name=t.get("shortDisplayName") or t.get("location") or t.get("abbreviation"))
        hid, aid = str(H["team"]["id"]), str(A["team"]["id"])
        ha, aa = norm(H["team"].get("abbreviation")), norm(A["team"].get("abbreviation"))
        hs, as_ = to_num(H.get("score")), to_num(A.get("score"))
        stype = (comp.get("status") or ev.get("status") or {}).get("type") or {}
        status = "final" if stype.get("completed") else ("live" if stype.get("state") == "in" else "pre")
        if stype.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            status = "ppd"
        if status not in ("final", "ppd"):
            all_final = False
        key = str(ev.get("id"))
        old = st["games"].get(key, {})
        try:
            kt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        tip = kt.strftime("%a %-I:%M %p")
        staged[key] = dict(date=kt.date().isoformat(), week=wkey, away=aid, home=hid, status=status,
                           home_line=old.get("home_line"), open_line=old.get("open_line"),
                           home_margin=(hs - as_) if status == "final" and hs is not None and as_ is not None else None,
                           away_score=as_, home_score=hs, time=tip, tip_iso=ev.get("date"),
                           neutral=bool(comp.get("neutralSite")), src=old.get("src"), line_done=old.get("line_done", False))
        # completed games: look the closing line up once; upcoming/live games: refresh every run
        if not (status == "final" and old.get("status") == "final" and old.get("line_done")):
            jobs.append((key, key, ha, aa, comp.get("odds")))
    dates = sorted(g["date"] for g in staged.values())
    if not dates or dates[0] > horizon:
        return True, (dates[0] if dates else None)  # week not close yet: don't store or fetch lines
    for key, (l, o, p) in fetch_lines(jobs).items():
        g = staged[key]
        if l is not None:
            g.update(home_line=l, open_line=o, src=f"ESPN/{p or BOOK}")
        if g["status"] == "final":
            g["line_done"] = True
    st["games"].update(staged)
    if staged and all_final and dates[-1] < today.isoformat():
        st["done_weeks"] = sorted(set(st["done_weeks"]) | {wkey})
    return True, (dates[0] if dates else None)


def update_data(st, today, refetch):
    if refetch:
        st["done_weeks"] = []
        for g in st["games"].values():
            g["line_done"] = False
    horizon = (today + timedelta(days=8)).isoformat()
    print("Fetching open weeks from ESPN (first run takes a few minutes)...")
    for stype, wk in [(2, w) for w in range(1, REG_WEEKS + 1)] + [(3, 1)]:
        key = week_key(stype, wk)
        if key in st["done_weeks"]:
            continue
        print(f"  {'Bowls' if stype == 3 else 'Week ' + str(wk)}", end="", flush=True)
        ok, first = fetch_week(st, stype, wk, today, horizon)
        if not ok:
            print("  -> failed (kept old data)")
            continue
        if first is None or first > horizon:
            print("  -> not yet")
            continue
        wg = [g for g in st["games"].values() if g.get("week") == key]
        print(f"  -> {len(wg)} games, {sum(g['home_line'] is not None for g in wg)} with spreads")
        save_store(st)


def display_names(st):
    """Unique short display name per team id (ESPN short names occasionally repeat)."""
    names, seen = {}, {}
    for tid, t in st["teams"].items():
        seen.setdefault(t["name"], []).append(tid)
    for nm, ids in seen.items():
        for tid in ids:
            names[tid] = nm if len(ids) == 1 else f"{nm} ({st['teams'][tid]['abbr']})"
    return names


# ------------------------------- MODEL ----------------------------------
def team_metrics(h, sd):
    """h = list of ATS margins from the team's point of view (+ = covered by that many)."""
    n = len(h)
    if n == 0:
        return dict(gp=0, w=0, l=0, p=0, cover=None, err=None, last5=None, zc=None, ze=None, comb=None)
    w = sum(a > 0 for a in h); l = sum(a < 0 for a in h); p = n - w - l
    err = sum(h) / n
    l5 = h[-RECENT_N:]; last5 = sum(l5) / len(l5)
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
    names = display_names(st)
    nm = lambda tid: names.get(tid, tid)
    games = sorted(st["games"].values(), key=lambda g: (g["week"], g.get("tip_iso") or "", g["away"]))
    graded = [g for g in games if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None]
    sd = ATS_SD
    if sd is None:
        ats = [g["home_margin"] + g["home_line"] for g in graded]
        sd = statistics.pstdev(ats + [-a for a in ats]) if len(ats) > 30 else 12.0
    hist = {}
    H = lambda t: hist.setdefault(t, [])
    weeks = sorted({g["week"] for g in games})
    out_games, snapshots = [], {}

    def snap():
        rows = []
        for t, h in hist.items():
            if not h:
                continue
            m = team_metrics(h, sd); s_ = stance(m)
            rows.append(dict(team=nm(t), **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()},
                             stance=s_[0], contrib=round(s_[1], 3)))
        return rows

    for w in weeks:
        day = [g for g in games if g["week"] == w]
        cache = {}

        def read(t):
            if t not in cache:
                m = team_metrics(H(t), sd); cache[t] = (m, stance(m))
            return cache[t]
        snapshots[w] = snap()
        for g in day:
            (mh, sh), (ma, sa) = read(g["home"]), read(g["away"])
            sig = sh[1] - sa[1]
            u = units_for(sig) if g["home_line"] is not None and g["status"] != "ppd" else 0.0
            pick = (g["home"] if sig > 0 else g["away"]) if u else None
            pick_line = (g["home_line"] if pick == g["home"] else -g["home_line"]) if pick else None
            res, pl, cover = None, 0.0, None
            if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None:
                a = g["home_margin"] + g["home_line"]
                cover = nm(g["home"]) if a > 0 else nm(g["away"]) if a < 0 else "PUSH"
                if pick:
                    res = "P" if a == 0 else ("W" if (a > 0) == (pick == g["home"]) else "L")
                    pl = u * WIN_MULT if res == "W" else (-u if res == "L" else 0.0)
            gg = {k: v for k, v in g.items() if k not in ("line_done",)}
            gg.update(home=nm(g["home"]), away=nm(g["away"]), signal=round(sig, 3), units=u,
                      pick=nm(pick) if pick else None, pick_line=pick_line, result=res, pl=round(pl, 3), cover=cover,
                      home_stance=sh[0], away_stance=sa[0], has_line=g["home_line"] is not None)
            out_games.append(gg)
        for g in day:
            if g["status"] == "final" and g["home_line"] is not None and g["home_margin"] is not None:
                a = g["home_margin"] + g["home_line"]
                H(g["home"]).append(a); H(g["away"]).append(-a)
    snapshots["latest"] = snap()
    return out_games, snapshots, sd


def weekly_table(out_games):
    wks = {}
    for g in out_games:
        if not g["pick"]:
            continue
        r = wks.setdefault(g["week"], dict(week=g["week"], bets=0, w=0, l=0, p=0, pending=0, risked=0.0, pl=0.0))
        r["bets"] += 1
        r[{"W": "w", "L": "l", "P": "p"}.get(g["result"], "pending")] += 1
        if g["result"] in ("W", "L"):
            r["risked"] += g["units"]; r["pl"] += g["pl"]
    rows, cum, cum_r = [], 0.0, 0.0
    for w in sorted(wks):
        r = wks[w]; cum += r["pl"]; cum_r += r["risked"]
        r.update(cum=round(cum, 3), cum_roi=round(100 * cum / cum_r, 2) if cum_r else None, pl=round(r["pl"], 3))
        rows.append(r)
    return rows


def week_label(w, gs):
    ds = sorted(g["date"] for g in gs)
    f = lambda x: date.fromisoformat(x).strftime("%b %-d")
    name = "Bowls & CFP" if w == "B" else f"Week {int(w)}"
    if not ds:
        return name
    return f"{name} · {f(ds[0])}" if ds[0] == ds[-1] else f"{name} · {f(ds[0])}–{f(ds[-1])}"


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    today = date.today()
    if "--date" in args:
        today = date.fromisoformat(args[args.index("--date") + 1])
    st = load_store()
    if "--offline" not in args:
        update_data(st, today, "--refetch" in args)
        save_store(st)
    out_games, snaps, sd = run_model(st, today)
    weekly = weekly_table(out_games)
    weeks = sorted({g["week"] for g in out_games})
    open_wk = [g["week"] for g in out_games if g["status"] != "final"]
    open_week = min(open_wk) if open_wk else (weeks[-1] if weeks else "01")
    labels = {w: week_label(w, [g for g in out_games if g["week"] == w]) for w in weeks}
    payload = dict(generated=datetime.now().strftime("%a %b %-d, %Y %-I:%M %p"), today=today.isoformat(), open_week=open_week,
                   week_labels=labels, games=out_games, snapshots=snaps, weekly=weekly, sd=round(sd, 2), win_mult=WIN_MULT,
                   config=dict(MIN_GAMES=MIN_GAMES, Z_OUTLIER=Z_OUTLIER, LAST5_CONFIRM=LAST5_CONFIRM, RECENT_N=RECENT_N,
                               FADE_WEIGHT=FADE_WEIGHT, RIDE_WEIGHT=RIDE_WEIGHT, UNIT_TIERS=UNIT_TIERS, JUICE=JUICE))
    with open(TEMPLATE) as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(payload))
    with open(OUT_HTML, "w") as f:
        f.write(html)
    w = sum(r["w"] for r in weekly); l = sum(r["l"] for r in weekly)
    pl = sum(r["pl"] for r in weekly); risk = sum(r["risked"] for r in weekly)
    open_bets = [g for g in out_games if g["week"] == open_week and g["pick"] and not g["result"]]
    print(f"\nSeason: {w}-{l}, {pl:+.2f}u, ROI {100*pl/risk if risk else 0:.1f}%   |   {labels.get(open_week, '')}: {len(open_bets)} open bet(s)")
    for g in open_bets:
        print(f"   {g['pick']} {g['pick_line']:+g}  ({g['away']} @ {g['home']}, {g.get('time') or g['date']})  {g['units']:g}u")
    print(f"Wrote {OUT_HTML}")
    if "--no-open" not in args:
        webbrowser.open("file://" + OUT_HTML)
    return payload


if __name__ == "__main__":
    main()
