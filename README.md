# banfftrack

An hourly log of Banff Sunshine Village and Lake Louise. For each resort it records:
- which lifts and runs are open, on standby or groomed, and when each opened during the day;
- the resort's snow reports and weather stations;
- model weather at the base, mid and summit, with a rule-based snow-quality read.

It checks at 5:30, 6:00 and 6:30am, then every hour from 7am to 5pm Banff time, on the dates in `resorts.json`: 14
checks a day per resort. The early checks catch the overnight grooming and snow reports before people head up. GitHub
Actions runs it and commits the results here. Standard library only, Python 3.13.

**Latest data:** `out/latest_sunshine.json` and `out/latest_louise.json` (format in `DATA_FORMAT.md`). The repository
is served by GitHub Pages from `main`, root folder, at `https://<owner>.github.io/<repo>/out/latest_<resort>.json`.
The empty `.nojekyll` file makes Pages serve the files as they are, with no Jekyll build. Pages caches files for about
10 minutes. The raw file URLs also allow cross-origin requests and are cached for 5 minutes.

`app/` holds a web app that reads `out/`; it's maintained separately and nothing here writes to it. Both writers run
`git pull --rebase` before pushing.

## Time
Alberta has kept UTC-6 all year since 2026. On 2026-11-15, Lake Louise's feed stamped `13:32 -06:00` at 19:32 UTC, and
skilouise.com said the province was moving to year-round Alberta Time. So the 5:30am-5pm Banff checks are at
**11:30-23:00 UTC**. The offset is `utc_offset_hours` in `resorts.json`.

## Sources (checked 2026-11-15)
| source | what it gives | notes |
|---|---|---|
| `sunshine-lifts`: skibanff.com lifts & trails report | 12 lifts with posted hours; 139 runs by area (Goat's Eye, Lookout, Standish), each with difficulty, Open / Closed / **Standby** and a Groomed icon | HTML rendered on the server: the page makes no JSON calls. Standby means expected to open |
| `sunshine-conditions`: skibanff.com current conditions | Upper and mid-mountain snow report (7-day, base, cumulative; 24 h and overnight cards appear after snowfall), summary text, the RWDI forecast (highs and lows, freezing level, ridge wind, precipitation) | Sunshine publishes **no live station temperatures** |
| `louise-feed`: ski-louise-status-page.vercel.app/api/ski-data | The resort's XML report: 13 lifts and 171 runs by area, each with Open / Closed and groomed yes / no; three stations (Operations, SM-Control, Paradise-Top) with temperature and wind; new snow (overnight, 24 h, 48 h, 7 days); base; season total; forecast; text | A third-party status page's proxy of the resort's feed. The feed's own URL isn't public |
| `louise-grooming-pdf`, `louise-runs-pdf` | Lake Louise's grooming and run report PDFs | Recorded by hash, size and time only, which shows when the resort republished them |
| `open-meteo` | Hourly model weather at each zone's elevation, 7 days back and 3 ahead | Free for non-commercial use |

Neither resort publishes a time a lift is expected to open. The log records it three ways:
- Sunshine's Standby status;
- every sentence in the resorts' text that gives a time (for example "Mad Trappers, open from 10:30 am");
- the hour each lift and run was first seen open (`first_open`, and `typical_first_open` across days).

Zones (`resorts.json`):
- **Sunshine:** base is the gondola base, 1660 m (where the Banff Ave ski-out ends). Mid is the Village, 2160 m (its
  mid-mountain report). Summit is Lookout, 2730 m (its upper report).
- **Lake Louise:** base 1645 m, mid at Whitehorn Lodge 2042 m, summit 2637 m. Its stations are at the base, mid and
  Paradise-Top.

## Snow quality
`snow_quality()` reads these per zone:
- new snow in 24 h (the resort's figure for that zone, else the model's);
- the temperature it fell at;
- the temperature now (Louise's station where there is one, else the model);
- hours above 0 °C in the last 24 and 72 h;
- snow since the last thaw;
- rain in 48 h;
- wind and gusts in 24 h.

It gives one code and label, plus flags and a one-line reason. The labels are: cold dry powder, dry powder, denser
powder, heavy wet new snow, wet, rain crust, refrozen, wind-affected, dusting, packed powder, and packed and firm. The
full table is in `DATA_FORMAT.md`, and the thresholds are `Q` in `banfftrack.py`. Every input is saved with the result,
so after a threshold change, `rebuild` re-scores the whole log.

## Files
- `data/raw/<date>/<sha>.<ext>.gz`: the stored response bodies, gzipped. Identical bodies on the same day share a file.
  - Sunshine's two pages are stored from their data block to the end of the main content, which is everything the
    parsers read. If the page layout changes, the whole page is stored instead.
  - `data/raw/manifest.jsonl` lists every fetch: time, resort, source, URL, HTTP status, size and SHA-256 of the full
    response, the stored file, and any error.
- `data/banfftrack.db` (SQLite, not committed): `snapshots` (the whole snapshot as JSON), `lift_status`, `run_status`,
  `zone_conditions` (snow, station, model and quality per zone) and `fetches`. A run rebuilds it from `data/raw` when
  it's missing.
- `out/`: `latest.json`, `latest_<resort>.json`, `days/<resort>/<date>.json`, `days/index.json`,
  `digest/<date>.md`.
- `banfftrack.log`: one line per resort per check, with any errors (UTC).

## Commands
```
python -I banfftrack.py collect               # one check now
python -I banfftrack.py collect --scheduled   # what the schedule runs: does nothing outside the window
python -I banfftrack.py summary [YYYY-MM-DD]  # the day's digest (default today in Banff), also out/digest/
python -I banfftrack.py check                 # validate every field of out/latest.json
python -I banfftrack.py rebuild               # re-parse everything in data/raw into a new database and out/
python -I banfftrack.py export [folder]       # CSVs (default ~/Downloads/banfftrack)
python -I test_banfftrack.py                  # tests, on responses saved 2026-11-15 (tests/fixtures)
```
A run takes about 5 seconds. It exits 1 if any source failed, but still saves what it got.

## Schedule
`.github/workflows/collect.yml` runs `collect --scheduled` 5 minutes after each slot (11:35, 12:05 and 12:35, then
13:05 to 23:05 UTC) on the window dates. It then commits `data/raw`, `out` and the log.
- The slots are listed in `resorts.json`. A check counts for the latest slot at or before it, until the next slot
  begins, so GitHub's late starts still land in the right slot. If two checks fall in one slot, the later one counts.
- To test outside the window, use Actions > collect > Run workflow with "force" ticked.
