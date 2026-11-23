"""Weekly grooming analysis: what each run's measured grooming category is, and how grooming follows snowfall.

Reads log.csv (one row per resort, run and day) and the thresholds in analysis.json, and writes:
  per_run.csv     every run: open days, groomed days, groomed share, measured and guessed category
  trends.csv      groomed share of open run-days by days since 2+ in of snow, new snow, weekday or weekend,
                  season stage, pod and difficulty, per resort and overall
  measured.json   map run -> measured category, for runs with enough open days (what the maps read)
  Grooming log.xlsx  the daily log, the per-run summary and the trend tables (needs openpyxl)

Usage: python analyze.py [--xlsx PATH]   (Python 3.7+; openpyxl only for the spreadsheet)
"""

import csv
import json
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS = json.loads((HERE / "analysis.json").read_text(encoding="utf-8"))
ORDER = ["never", "rarely", "rotational", "often", "always"]


def number(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def category(share):
    for name, cut in SETTINGS["categories"]:
        if share >= cut:
            return name
    return SETTINGS["categories"][-1][0]


def since_bin(v):
    if v is None:
        return "unknown"
    v = int(v)
    return "3+ days" if v >= 3 else "%d day%s" % (v, "" if v == 1 else "s")


def snow_bin(v):
    if v is None:
        return "unknown"
    lo, hi = SETTINGS["snow_bins_in"]
    if v < 0.1:
        return "0 in"
    if v < lo:
        return "under %g in" % lo
    if v < hi:
        return "%g-%g in" % (lo, hi)
    return "%g+ in" % hi


def stage_bin(v):
    if v is None:
        return "unknown"
    lo, hi = SETTINGS["base_stage_in"]
    return "early (under %g in base)" % lo if v < lo else "mid (%g-%g in)" % (lo, hi) if v < hi else \
        "late (%g+ in)" % hi


def per_run(rows):
    runs = OrderedDict()
    for r in rows:
        key = (r["resort"], r["run"])
        item = runs.setdefault(key, {"resort": r["resort"], "run": r["run"], "map_names": r["map_names"],
                                     "difficulty": r["difficulty"], "pod": r["pod"], "guessed": r["guessed"],
                                     "open_days": 0, "groomed_days": 0, "first": r["date"], "last": r["date"]})
        item["last"] = r["date"]
        if r["open"] == "Y":
            item["open_days"] += 1
            item["groomed_days"] += r["groomed"] == "Y"
    out = []
    for item in runs.values():
        share = item["groomed_days"] / item["open_days"] if item["open_days"] else None
        enough = item["open_days"] >= SETTINGS["min_open_days"]
        measured = category(share) if share is not None and enough else ""
        guess = item["guessed"]
        if not measured:
            verdict = "not enough open days" if item["open_days"] else "never open"
        elif not guess:
            verdict = "no guess"
        elif measured in guess.split("; "):
            verdict = "matches"
        else:
            gap = ORDER.index(measured) - max(ORDER.index(g) for g in guess.split("; ") if g in ORDER)
            verdict = "groomed more than guessed" if gap > 0 else "groomed less than guessed"
        out.append(dict(item, share=round(share, 3) if share is not None else None, measured=measured,
                        verdict=verdict))
    return out


def trends(rows):
    dims = OrderedDict([
        ("Days since %g+ in of snow" % SETTINGS["big_snow_in"],
         lambda r: since_bin(number(r["days_since_2in"]))),
        ("New snow (24 h to the morning report)", lambda r: snow_bin(number(r["new_24h_in"]))),
        ("Weekday or weekend", lambda r: "weekend" if r["weekday"] in SETTINGS["weekend"] else "weekday"),
        ("Season stage (base depth)", lambda r: stage_bin(number(r["base_in"]))),
        ("Difficulty", lambda r: r["difficulty"] or "unknown"),
        ("Pod", lambda r: r["pod"] or "(not on the map)"),
    ])
    out = []
    for dim, fn in dims.items():
        counts = defaultdict(lambda: [0, 0])
        for r in rows:
            if r["open"] != "Y":
                continue
            for resort in (r["resort"], "all"):
                c = counts[(fn(r), resort)]
                c[0] += 1
                c[1] += r["groomed"] == "Y"
        for (value, resort), (open_n, groomed_n) in sorted(counts.items()):
            out.append({"dimension": dim, "value": value, "resort": resort, "open_run_days": open_n,
                        "groomed_run_days": groomed_n, "groomed_share": round(groomed_n / open_n, 3)})
    return out


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_xlsx(path, rows, runs, trend_rows, log_fields):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    bold, head = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")

    def sheet(ws, header, data, widths=None, pct=()):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = bold, head
        for row in data:
            ws.append(row)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(header, 1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, max(10, min(40, len(h) + 2)))
            if h in pct:
                for cell in ws.iter_cols(min_col=i, max_col=i, min_row=2):
                    for c in cell:
                        c.number_format = "0%"

    numeric = {"checks", "new_overnight_in", "new_24h_in", "new_48h_in", "new_72h_in", "base_in", "temp_f",
               "snotel_depth_change_in", "model_24h_in", "days_since_2in"}
    ws = wb.active
    ws.title = "Per run"
    cols = ["resort", "run", "map_names", "pod", "difficulty", "open_days", "groomed_days", "share", "measured",
            "guessed", "verdict", "first", "last"]
    sheet(ws, ["Resort", "Run (resort's name)", "Map name", "Pod", "Difficulty", "Open days", "Groomed days",
               "Groomed share", "Measured", "Guessed (maps)", "Verdict", "First day", "Last day"],
          [[r[c] for c in cols] for r in runs], {"Run (resort's name)": 30, "Map name": 30, "Verdict": 26},
          pct=("Groomed share",))
    ws = wb.create_sheet("Trends")
    sheet(ws, ["What", "Value", "Resort", "Open run-days", "Groomed run-days", "Groomed share"],
          [[t["dimension"], t["value"], t["resort"], t["open_run_days"], t["groomed_run_days"], t["groomed_share"]]
           for t in trend_rows], {"What": 36, "Value": 26}, pct=("Groomed share",))
    ws = wb.create_sheet("Daily log")
    sheet(ws, log_fields, [[number(r[f]) if f in numeric and r[f] != "" else r[f] for f in log_fields] for r in rows],
          {"run": 28, "map_names": 28, "report_time": 24, "pod": 18})
    ws = wb.create_sheet("Settings")
    ws.append(["Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")])
    ws.append(["Measured category", "a run's groomed share of its open days: " + ", ".join(
        "%s from %d%%" % (n, round(c * 100)) for n, c in SETTINGS["categories"])])
    ws.append(["Minimum open days", SETTINGS["min_open_days"]])
    ws.append(["Snow", "inches; a day's new snow is the 24 h to the morning report: the resort's figure, "
                       "else Open-Meteo's (new_snow_source)"])
    ws.append(["Groomed", "Y if any check that day (from 6am local) showed it groomed; groomed_by_check lists each"])
    ws.append(["Thresholds", "grooming/analysis.json in the banff-ski repository"])
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 22, 110
    for c in ws["A"]:
        c.font = bold
    wb.save(path)


def main(argv):
    rows = list(csv.DictReader(open(HERE / "log.csv", encoding="utf-8")))
    log_fields = list(rows[0].keys()) if rows else []
    runs = per_run(rows)
    trend_rows = trends(rows)
    write_csv(HERE / "per_run.csv", runs, ["resort", "run", "map_names", "pod", "difficulty", "open_days",
                                           "groomed_days", "share", "measured", "guessed", "verdict", "first", "last"])
    write_csv(HERE / "trends.csv", trend_rows, ["dimension", "value", "resort", "open_run_days", "groomed_run_days",
                                                "groomed_share"])
    measured = defaultdict(dict)
    for r in runs:
        if r["measured"]:
            for name in r["map_names"].split("; "):
                if name and not name.startswith("("):
                    measured[r["resort"]][name] = {"category": r["measured"], "share": r["share"],
                                                   "open_days": r["open_days"], "report_name": r["run"]}
    (HERE / "measured.json").write_text(json.dumps(
        {"_note": "Measured grooming per map run (analyze.py): the groomed share of open days, for runs with at "
                  "least %d open days. Thresholds in analysis.json." % SETTINGS["min_open_days"],
         "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "resorts": measured},
        indent=1, ensure_ascii=False), encoding="utf-8")
    xlsx = argv[argv.index("--xlsx") + 1] if "--xlsx" in argv else str(HERE / "Grooming log.xlsx")
    try:
        write_xlsx(xlsx, rows, runs, trend_rows, log_fields)
        print("wrote", xlsx)
    except ImportError:
        print("openpyxl is not installed: wrote the CSVs and measured.json only")
    counted = sum(1 for r in runs if r["measured"])
    print("%d log rows, %d runs, %d with enough open days" % (len(rows), len(runs), counted))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
