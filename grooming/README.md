# Grooming log

A season-long, per-run record of which runs each resort actually grooms, next to that day's snow. The aim is to
measure each run's grooming instead of guessing it. Standard library only (`analyze.py` also uses openpyxl for the
spreadsheet).

## What it collects
`grooming.py collect` checks each resort in `resorts.json`. The workflow (`.github/workflows/grooming.yml`) runs it at
14:30, 17:30 and 20:30 UTC every day from November to May, after the morning grooming reports post, plus later
re-checks in case they're revised.

| resort | source | per-run grooming |
|---|---|---|
| Banff Sunshine | skibanff.com lifts and trails report (HTML) | the Groomed icon on each run |
| Lake Louise | the resort's XML report (via ski-louise-status-page.vercel.app); the grooming PDF's time | `groomed` yes/no on each run |
| Crystal Mountain | Mountain Powder feed (mtnpowder.com, token from v4.mtnfeed.com) | `Grooming` on each trail |

Snow for each day:
- **New snow:** the resort's report first (overnight, 24, 48 and 72 h, 7 days, base), then the nearest SNOTEL station
  (Crystal: Morse Lake), then Open-Meteo at mid-mountain.
- **Days since snow:** "days since the last 2 in" uses each day's 24 h to the morning report, the resort's figure where
  given, else the model's.

## Files
- `days/<resort>/<date>.json`: every check that day, with one status and one groomed flag per run per check.
- `log.csv`: one row per resort, run and day. A run is open if any check that day showed it open. It's groomed if any
  check from 6am local showed it groomed; earlier checks still show the day before. Run names are matched to the maps'
  names with the app's aliases (`app/aliases/`) and `crystal_names.json`.
- `raw/`: every response, gzipped (Sunshine's pages trimmed to their data); `raw/manifest.jsonl` lists them.
- `analyze.py` (weekly) writes:
  - `per_run.csv`: open and groomed days, the measured category, and the maps' guess;
  - `trends.csv`: the groomed share by days since snow, new snow, weekday or weekend, season stage, difficulty and
    pod;
  - `measured.json`: measured categories for map runs with enough open days;
  - `Grooming log.xlsx`.
  Thresholds are in `analysis.json`.

## Commands
```
python -I grooming/grooming.py collect [resort ...]
python -I grooming/grooming.py log                  # rebuild log.csv from days/
python -I grooming/grooming.py backfill-banfftrack  # Sunshine and Lake Louise checks from the trip tracker's archive
python -I grooming/analyze.py [--xlsx PATH]
```
