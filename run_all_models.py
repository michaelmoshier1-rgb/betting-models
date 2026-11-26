#!/usr/bin/env python3
"""
All-Sports Betting Models - one run, one dashboard
--------------------------------------------------
Run once a day:   python3 run_all_models.py

Updates and runs every model in this folder:
  NBA (spreads) · NFL (spreads, weekly) · College Basketball (spreads)
  College Football (spreads, weekly) · NHL (moneylines)
then writes all_models.html (the combined dashboard) and opens it.
Each sport's own full dashboard is still written next to it
(nba_ats_model.html, nfl_ats_model.html, ...), linked from the combined page.

If one sport fails (e.g. ESPN hiccup), the others still run and the page says
which sport had a problem.

LOCKED LEDGER (all_ledger.json): every play on the combined dashboard is saved the first
time it appears, before its game starts - side, line, price and units are frozen there.
It is graded once from the final score at the frozen line and never changes afterwards,
even if ESPN later updates a line or the sport models would now pick differently.
The very first run with this file seeds the ledger with the season so far, as shown then. Model settings live at the top of each sport file.

Flags:  --offline   rebuild from saved data, no network
        --no-open   don't open the browser
        --date YYYY-MM-DD   treat this date as "today"
        --only nba,nhl      run just these sports
"""
import importlib.util, json, os, sys, traceback, webbrowser
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_HTML = os.path.join(HERE, "all_models.html")
TEMPLATE = os.path.join(HERE, "all_models_template.html")
LEDGER = os.path.join(HERE, "all_ledger.json")
TRANSIENT = ("status", "final", "time", "current")   # display-only fields refreshed each run until graded

SPORTS = [  # key, label, module file, own page, cadence, bet type
    ("nba", "NBA", "nba_ats_model.py", "nba_ats_model.html", "day", "Spread"),
    ("nfl", "NFL", "nfl_ats_model.py", "nfl_ats_model.html", "week", "Spread"),
    ("cbb", "College Basketball", "cbb_ats_model.py", "cbb_ats_model.html", "day", "Spread"),
    ("cfb", "College Football", "cfb_ats_model.py", "cfb_ats_model.html", "week", "Spread"),
    ("nhl", "NHL", "nhl_ml_model.py", "nhl_ml_model.html", "day", "Moneyline"),
]


def load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fmt_line(x):
    return "PK" if x == 0 else f"{x:+g}"


def normalize(key, label, cadence, bettype, p):
    """Turn one sport's payload into common records for the combined page."""
    games, today = p["games"], p["today"]
    if cadence == "week":
        cur = p["open_week"]
        labels = {str(k): v for k, v in p["week_labels"].items()}
        current = [g for g in games if g["week"] == cur]
        cur_label = labels.get(str(cur), f"Week {cur}")
    else:
        current = [g for g in games if g["date"] == today]
        cur_label = "Today"
    cur_ids = {id(g) for g in current}
    bets = []
    for g in games:
        if not g.get("pick"):
            continue
        if bettype == "Moneyline":
            pick = f"{g['pick']} {g['price']:+.0f}" if g.get("price") is not None else g["pick"]
            line = (f"{g['away']} {g['away_ml']:+.0f} / {g['home']} {g['home_ml']:+.0f}"
                    if g.get("home_ml") is not None else "")
            final = f"{g['away_score']:g}-{g['home_score']:g}{' OT' if g.get('ot') else ''}" if g["status"] == "final" and g.get("home_score") is not None else ""
        else:
            pick = f"{g['pick']} {fmt_line(g['pick_line'])}"
            hl = g.get("home_line")
            line = "" if hl is None else (f"{g['home']} {fmt_line(hl)}")
            if g["status"] == "final":
                final = (f"{g['away_score']:g}-{g['home_score']:g}" if g.get("home_score") is not None
                         else (f"{g['home']} {g['home_margin']:+g}" if g.get("home_margin") is not None else ""))
            else:
                final = ""
        period = str(g["week"]) if cadence == "week" else g["date"]
        bets.append(dict(k=game_key(key, g), side="home" if g["pick"] == g["home"] else "away",
                         pick_line=g.get("pick_line"), price=g.get("price"),
                         sport=key, date=g["date"], period=period,
                         period_label=(labels.get(str(g["week"]), "") if cadence == "week" else g["date"]),
                         matchup=f"{g['away']} @ {g['home']}", away=g["away"], home=g["home"], pick=pick, line=line,
                         units=g["units"], result=g.get("result"), pl=g.get("pl") or 0.0, status=g["status"],
                         time=g.get("time") or "", signal=g.get("signal"), final=final,
                         current=(id(g) in cur_ids), away_read=g.get("away_stance", ""), home_read=g.get("home_stance", "")))
    gidx = {}
    for g in games:
        if g.get("home_score") is not None and g.get("away_score") is not None:
            margin = g["home_score"] - g["away_score"]
        else:
            margin = g.get("home_margin")
        fin = ""
        if g["status"] == "final":
            if g.get("home_score") is not None:
                fin = f"{g['away_score']:g}-{g['home_score']:g}" + (" OT" if bettype == "Moneyline" and g.get("ot") else "")
            elif g.get("home_margin") is not None:
                fin = f"{g['home']} {g['home_margin']:+g}"
        gidx[game_key(key, g)] = dict(status=g["status"], margin=margin, final=fin, time=g.get("time") or "",
                                      current=(id(g) in cur_ids))
    snap = p["snapshots"].get("latest", [])
    ratings = []
    for t in snap:
        if bettype == "Moneyline":
            ratings.append(dict(team=t["team"], gp=t["gp"], rec=f"{t['w']}-{t['l']}", err=t["err"], recent=t["recent"],
                                comb=t["comb"], stance=t["stance"], unit="pct"))
        else:
            ratings.append(dict(team=t["team"], gp=t["gp"], rec=f"{t['w']}-{t['l']}" + (f"-{t['p']}" if t.get("p") else ""),
                                err=t["err"], recent=t.get("recent", t.get("last5")), comb=t["comb"], stance=t["stance"], unit="pts"))
    return dict(key=key, label=label, cadence=cadence, bettype=bettype, cur_label=cur_label,
                slate_games=len(current), slate_lines=sum(1 for g in current if g.get("has_line", g.get("home_line") is not None)),
                bets=bets, ratings=ratings, gidx=gidx)


def game_key(sport, g):
    return f"{sport}|{g['date']}|{g['away']}|{g['home']}"


def win_mult(units, juice=-110):
    return units * (100 / abs(juice) if juice < 0 else juice / 100)


def dec(ml):
    return 1 + (ml / 100 if ml > 0 else 100 / abs(ml))


def grade(b, gi, juice):
    """Grade a frozen bet once, from the final score at the FROZEN line/price."""
    m = gi["margin"]
    if m is None:
        return False
    mine = m if b["side"] == "home" else -m
    if b["bettype"] == "Moneyline":
        res = "W" if mine > 0 else ("L" if mine < 0 else "P")
        pl = b["units"] * (dec(b["price"]) - 1) if res == "W" else (-b["units"] if res == "L" else 0.0)
    else:
        a = mine + (b["pick_line"] or 0.0)
        res = "W" if a > 0 else ("L" if a < 0 else "P")
        pl = win_mult(b["units"], juice) if res == "W" else (-b["units"] if res == "L" else 0.0)
    b.update(result=res, pl=round(pl, 3), status="final", final=gi["final"], graded_at=datetime.now().isoformat(timespec="minutes"))
    return True


def apply_ledger(sports, juices):
    """Freeze new plays, grade open ones once, and show ONLY ledger plays on the dashboard."""
    seeding = not os.path.exists(LEDGER)
    led = {"bets": {}}
    if not seeding:
        with open(LEDGER) as f:
            led = json.load(f)
    now = datetime.now().isoformat(timespec="minutes")
    added = graded = 0
    for s in sports:
        for b in s["bets"]:
            if b["k"] in led["bets"]:
                continue
            if seeding or b["status"] == "pre":          # only lock plays made BEFORE the game starts
                fb = {k: v for k, v in b.items()}
                fb.update(bettype=s["bettype"], locked_at=("seeded " if seeding else "") + now)
                if not seeding:
                    fb.update(result=None, pl=0.0)
                led["bets"][b["k"]] = fb
                added += 1
        gidx = s.pop("gidx")
        mine = [b for b in led["bets"].values() if b["sport"] == s["key"]]
        for b in mine:
            gi = gidx.get(b["k"])
            if b.get("result") or not gi:
                continue                                 # graded = frozen forever
            b.update({k: gi[k] for k in TRANSIENT})
            if gi["status"] == "final" and grade(b, gi, juices.get(s["key"], -110)):
                graded += 1
            elif gi["status"] == "ppd":
                b.update(result="P", pl=0.0, status="ppd", graded_at=now)
        for b in mine:
            if b.get("result"):
                b["current"] = gidx.get(b["k"], {}).get("current", False)
        s["bets"] = sorted(mine, key=lambda b: (b["date"], b.get("time") or "", b["matchup"]))
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=0)
    os.replace(tmp, LEDGER)
    print(f"Ledger: {'seeded with' if seeding else 'added'} {added} play(s), graded {graded}, {len(led['bets'])} total")


def main():
    args = sys.argv[1:]
    sub = [a for a in args if a in ("--offline", "--refetch")] + ["--no-open"]
    if "--date" in args:
        sub += ["--date", args[args.index("--date") + 1]]
    only = None
    if "--only" in args:
        only = set(args[args.index("--only") + 1].lower().split(","))
    sports, errors, juices = [], [], {}
    for key, label, fname, page, cadence, bettype in SPORTS:
        if only and key not in only:
            continue
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            errors.append(dict(sport=label, msg=f"{fname} not found in this folder"))
            continue
        print(f"\n===== {label} =====")
        try:
            mod = load(path)
            payload = mod.main(sub)
            juices[key] = getattr(mod, "JUICE", -110)
            s = normalize(key, label, cadence, bettype, payload)
            s["page"] = page
            sports.append(s)
        except Exception as e:
            traceback.print_exc()
            errors.append(dict(sport=label, msg=f"{type(e).__name__}: {e}"))
    if only:
        print("(--only run: ledger not updated)")
        for s in sports:
            s.pop("gidx", None)
    else:
        apply_ledger(sports, juices)
    today = date.today().isoformat()
    if "--date" in args:
        today = args[args.index("--date") + 1]
    data = dict(generated=datetime.now().strftime("%a %b %-d, %Y %-I:%M %p"), today=today, sports=sports, errors=errors)
    with open(TEMPLATE) as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(data))
    with open(OUT_HTML, "w") as f:
        f.write(html)
    allb = [b for s in sports for b in s["bets"]]
    w = sum(b["result"] == "W" for b in allb); l = sum(b["result"] == "L" for b in allb)
    pl = sum(b["pl"] for b in allb); risk = sum(b["units"] for b in allb if b["result"] in ("W", "L"))
    print("\n================ ALL SPORTS ================")
    print(f"Overall: {w}-{l}, {pl:+.2f}u, ROI {100*pl/risk if risk else 0:.1f}%")
    for s in sports:
        cur = [b for b in s["bets"] if b["current"] and not b["result"]]
        print(f"  {s['label']:<19} {s['cur_label']}: {len(cur)} open bet(s)")
    for e in errors:
        print(f"  !! {e['sport']}: {e['msg']}")
    print(f"Wrote {OUT_HTML}")
    if "--no-open" not in args:
        webbrowser.open("file://" + OUT_HTML)


if __name__ == "__main__":
    main()
