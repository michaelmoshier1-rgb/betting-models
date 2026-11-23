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
which sport had a problem. Model settings live at the top of each sport file.

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
        bets.append(dict(sport=key, date=g["date"], period=period,
                         period_label=(labels.get(str(g["week"]), "") if cadence == "week" else g["date"]),
                         matchup=f"{g['away']} @ {g['home']}", away=g["away"], home=g["home"], pick=pick, line=line,
                         units=g["units"], result=g.get("result"), pl=g.get("pl") or 0.0, status=g["status"],
                         time=g.get("time") or "", signal=g.get("signal"), final=final,
                         current=(id(g) in cur_ids), away_read=g.get("away_stance", ""), home_read=g.get("home_stance", "")))
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
                bets=bets, ratings=ratings)


def main():
    args = sys.argv[1:]
    sub = [a for a in args if a in ("--offline", "--refetch")] + ["--no-open"]
    if "--date" in args:
        sub += ["--date", args[args.index("--date") + 1]]
    only = None
    if "--only" in args:
        only = set(args[args.index("--only") + 1].lower().split(","))
    sports, errors = [], []
    for key, label, fname, page, cadence, bettype in SPORTS:
        if only and key not in only:
            continue
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            errors.append(dict(sport=label, msg=f"{fname} not found in this folder"))
            continue
        print(f"\n===== {label} =====")
        try:
            payload = load(path).main(sub)
            s = normalize(key, label, cadence, bettype, payload)
            s["page"] = page
            sports.append(s)
        except Exception as e:
            traceback.print_exc()
            errors.append(dict(sport=label, msg=f"{type(e).__name__}: {e}"))
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
