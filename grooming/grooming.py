"""Season-long grooming log: which runs each resort actually grooms, day by day, next to that day's snow.

Each `collect` checks every resort in resorts.json: the per-run open and groomed flags from the resort's own report,
its snow numbers, the nearest SNOTEL station where there is one, and Open-Meteo at mid-mountain. A day's checks are
merged into days/<resort>/<date>.json, and log.csv (one row per resort, run and day) is rebuilt from them. analyze.py
turns log.csv into the per-run summary and the trend tables. Every response is kept gzipped under raw/ (Sunshine's
pages trimmed to their data and Lake Louise's PDFs kept by hash only, as in banfftrack).

Usage (from the repository root or this folder; Python 3.7+, standard library only)
  python grooming.py collect [resort ...]   one check of every resort, or of the ones named
  python grooming.py log                    rebuild log.csv from days/
  python grooming.py backfill-banfftrack    turn banfftrack's saved Sunshine and Lake Louise checks into days/
"""

import csv
import gzip
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import banfftrack as bt  # noqa: E402  (fetching, trimming and the Sunshine and Lake Louise parsers)

CONFIG = HERE / "resorts.json"
RAW = HERE / "raw"
MANIFEST = RAW / "manifest.jsonl"
DAYS = HERE / "days"
LOG = HERE / "log.csv"
LOG_PATH = HERE / "grooming.log"
CM_PER_IN = 2.54
BIG_SNOW_CM = 2 * CM_PER_IN     # "days since the last snowfall of 2 in or more"
REPORT_HOUR = 8                 # a day's new snow is the 24 h to 8am local, as a morning report gives it
FIRST_HOUR = 6                  # checks before 6am local still show the day before's grooming; the log skips them


def log(message):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# --- time: Banff is a fixed UTC-6; US resorts follow US daylight time ---

def utc_offset(resort, utc_dt):
    tz = resort["tz"]
    if not tz.get("dst"):
        return tz["offset"]
    std = utc_dt.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=tz["offset"])
    march8, nov1 = datetime(std.year, 3, 8), datetime(std.year, 11, 1)
    start = march8 + timedelta(days=(6 - march8.weekday()) % 7, hours=2)
    end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=1)
    return tz["offset"] + 1 if start <= std < end else tz["offset"]


def local(resort, utc_dt):
    return utc_dt.astimezone(timezone(timedelta(hours=utc_offset(resort, utc_dt))))


# --- raw archive ---

def archive(fetched_at, day, resort_id, source, url, status, body, error, ext, keep_body=True):
    row = {"fetched_at": fetched_at, "resort": resort_id, "source": source, "url": url, "http_status": status,
           "bytes": len(body) if body is not None else None, "sha256": None, "path": None, "error": error}
    if body is not None:
        row["sha256"] = hashlib.sha256(body).hexdigest()
        stored = bt.trim(source, body)
        if stored is not body:
            row.update(trimmed=True, stored_bytes=len(stored))
        if keep_body:
            sha = hashlib.sha256(stored).hexdigest()
            rel = f"{day}/{sha[:20]}.{ext}.gz"
            path = RAW / rel
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(stored))
            row["path"] = rel
    RAW.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def head_time(url):
    """The Last-Modified time of a file (Lake Louise's grooming report PDF), as UTC ISO; two tries."""
    for _ in range(2):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": bt.USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                stamp = r.headers.get("Last-Modified")
            return datetime.strptime(stamp, "%a, %d %b %Y %H:%M:%S GMT").strftime("%Y-%m-%dT%H:%M:%SZ") if stamp \
                else None
        except Exception:
            continue
    return None


# --- resort readers: each returns (runs, snow, report_time, raw responses) ---
# runs: [{name, difficulty, area, status, groomed}]; snow: cm and degrees C, None where not reported.

LEVEL_WORDS = {"easiest": "green", "beginner": "green", "easy": "green", "more difficult": "blue",
               "intermediate": "blue", "most difficult": "black", "advanced": "black", "difficult": "black",
               "experts only": "double", "expert": "double", "extreme": "double", "freestyle terrain": "park"}


def level_of(word):
    return LEVEL_WORDS.get((word or "").strip().lower(), (word or "").strip().lower() or None)


def read_sunshine(resort, fetch):
    lifts_body = fetch("sunshine-lifts", resort["urls"]["lifts"], "html")
    cond_body = fetch("sunshine-conditions", resort["urls"]["conditions"], "html")
    runs, snow, report_time = [], {}, None
    if lifts_body:
        for r in bt.parse_sunshine_lifts(lifts_body.decode("utf-8"))["runs"]:
            runs.append({"name": r["name"], "difficulty": r["level"], "area": r["area"], "status": r["status"],
                         "groomed": r["groomed"]})
    if cond_body:
        cond = bt.parse_sunshine_conditions(cond_body.decode("utf-8"))
        report_time = cond["updated_local"]
        mid = (cond["zones"].get("Mid Mountain") or {}).get("values", {})
        snow = {"overnight_cm": mid.get("new_overnight_cm"), "new_24h_cm": mid.get("new_24h_cm"),
                "new_48h_cm": mid.get("new_48h_cm"), "new_72h_cm": mid.get("new_72h_cm"),
                "new_7d_cm": mid.get("new_7d_cm"), "base_cm": mid.get("base_cm"), "temp_c": None}
    return runs, snow, report_time


def read_louise(resort, fetch):
    body = fetch("louise-feed", resort["urls"]["feed"], "xml")
    runs, snow, report_time = [], {}, None
    if body:
        feed = bt.parse_louise(body)
        runs = [{"name": r["name"], "difficulty": r["level"], "area": r["area"], "status": r["status"],
                 "groomed": r["groomed"]} for r in feed["runs"]]
        lower, mid, upper = (feed["zones"].get(k, {}) for k in ("Lower Mountain", "Mid-Mountain", "Upper Mountain"))
        new = upper.get("values") or lower.get("values") or {}
        snow = {"overnight_cm": new.get("new_overnight_cm"), "new_24h_cm": new.get("new_24h_cm"),
                "new_48h_cm": new.get("new_48h_cm"), "new_72h_cm": None, "new_7d_cm": new.get("new_7d_cm"),
                "base_cm": bt.first_known(mid.get("values", {}).get("base_cm"),
                                          _mean(lower.get("values", {}).get("base_cm"),
                                                upper.get("values", {}).get("base_cm"))),
                "temp_c": mid.get("temp_c")}
        report_time = head_time(resort["urls"]["grooming_pdf"]) or feed["updated"]
    return runs, snow, report_time


def _mean(a, b):
    return round((a + b) / 2) if a is not None and b is not None else bt.first_known(a, b)


def read_mtnpowder(resort, fetch):
    """Resorts on the Mountain Powder feed (Crystal): a widget config gives the token, the feed gives every trail."""
    cfg_body = fetch("mtnfeed-config", f"https://v4.mtnfeed.com/resorts/{resort['mtnfeed']}.json", "json")
    if not cfg_body:
        return [], {}, None
    cfg = json.loads(cfg_body)
    query = urllib.parse.urlencode([("bearer_token", cfg["bearerToken"])] + [("resortId[]", i) for i in cfg["resortIds"]])
    body = fetch("mtnpowder-feed", "https://mtnpowder.com/feed/v3.json?" + query, "json")
    if not body:
        return [], {}, None
    feed = json.loads(body)["Resorts"][0]
    runs = []
    for area in feed.get("MountainAreas") or []:
        for t in area.get("Trails") or []:
            g = (t.get("Grooming") or "").strip().lower()
            runs.append({"name": t["Name"], "difficulty": level_of(t.get("Difficulty")), "area": area.get("Name"),
                         "status": bt.norm_status(t.get("StatusEnglish") or t.get("Status")),
                         "groomed": None if g in ("", "--") else g not in ("no", "false", "0")})
    sr = feed.get("SnowReport") or {}
    allm, mid = sr.get("AllMountain") or {}, sr.get("MidMountainArea") or {}
    cc = (feed.get("CurrentConditions") or {}).get("MidMountain") or {}
    snow = {"overnight_cm": bt.num(allm.get("SinceLiftsClosedCm")), "new_24h_cm": bt.num(allm.get("Last24HoursCm")),
            "new_48h_cm": bt.num(allm.get("Last48HoursCm")), "new_72h_cm": bt.num(allm.get("Last72HoursCm")),
            "new_7d_cm": bt.num(allm.get("Last7DaysCm")), "base_cm": bt.num(mid.get("BaseCm")),
            "temp_c": bt.num(cc.get("TemperatureC"))}
    return runs, snow, sr.get("LastUpdate") or feed.get("LastUpdate")


READERS = {"sunshine": read_sunshine, "louise": read_louise, "mtnpowder": read_mtnpowder}


# --- measured and modelled snow ---

def snotel_days(triplet, end):
    """SNOTEL daily snow depth and water equivalent (inches) for the 10 days to `end`."""
    data = bt_json("https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data", {
        "stationTriplets": triplet, "elements": "SNWD,WTEQ", "duration": "DAILY",
        "beginDate": (end - timedelta(days=10)).isoformat(), "endDate": end.isoformat()})
    days = {}
    for series in data[0]["data"]:
        key = {"SNWD": "depth_in", "WTEQ": "swe_in"}[series["stationElement"]["elementCode"]]
        for v in series.get("values", []):
            days.setdefault(v["date"][:10], {})[key] = v.get("value")
    return days


def bt_json(url, params):
    status, body, error = bt.fetch(url + "?" + urllib.parse.urlencode(params), accept="application/json")
    if error or body is None:
        raise RuntimeError(error or "no body")
    return json.loads(body)


def model_days(hourly, resort, now):
    """Modelled snow for each of the last 7 days: the 24 h to 8am local, the window a morning report covers."""
    out = {}
    times = hourly["time"]
    for back in range(0, 8):
        day = local(resort, now).date() - timedelta(days=back)
        end_local = datetime(day.year, day.month, day.day, REPORT_HOUR)
        end_utc = end_local - timedelta(hours=utc_offset(resort, now))
        stamps = {(end_utc - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(0, 24)}
        vals = [s for t, s in zip(times, hourly["snowfall"]) if t in stamps and s is not None]
        if len(vals) >= 20:
            out[day.isoformat()] = round(sum(vals), 1)
    return out


# --- one check ---

def check_resort(resort, now):
    fetched_at = bt.utc_iso(now)
    loc = local(resort, now)
    day = loc.date().isoformat()
    sources = []

    def fetch(source, url, ext, keep=True):
        status, body, error = bt.fetch(url)
        if body is not None and status != 200:
            error = error or f"HTTP {status}"
        row = archive(fetched_at, day, resort["id"], source, url, status, body, error, ext, keep)
        sources.append({k: row[k] for k in ("source", "url", "http_status", "bytes", "sha256", "error")})
        return None if error else body

    errors = []
    try:
        runs, snow, report_time = READERS[resort["kind"]](resort, fetch)
    except Exception as e:
        runs, snow, report_time = [], {}, None
        errors.append(f"{resort['kind']} parse {type(e).__name__}: {e}")
    m = resort["model"]
    model, history = None, {}
    zone = {"zone": "mid", "lat": m["lat"], "lon": m["lon"], "elev_m": m["elev_m"]}
    body = fetch("open-meteo", bt.open_meteo_url([zone]), "json")
    if body:
        try:
            model = bt.parse_open_meteo(body, [zone], now, utc_offset(resort, now))["mid"]
            history = model_days(json.loads(body)["hourly"], resort, now)
            model = {k: model[k] for k in ("temp_c", "tmin_24h_c", "tmax_24h_c", "snow_24h_cm", "snow_48h_cm",
                                           "snow_72h_cm", "snow_7d_cm", "hours_above_0_24h", "rain_24h_mm")}
        except Exception as e:
            errors.append(f"open-meteo parse {type(e).__name__}: {e}")
    snotel = None
    if resort.get("snotel"):
        try:
            d = snotel_days(resort["snotel"], loc.date())
            dates = sorted(k for k, v in d.items() if v.get("depth_in") is not None)
            if dates:
                last, prev = d[dates[-1]], d[dates[-2]] if len(dates) > 1 else {}
                snotel = {"station": resort["snotel"], "date": dates[-1], "depth_in": last.get("depth_in"),
                          "swe_in": last.get("swe_in"),
                          "depth_change_in": (last["depth_in"] - prev["depth_in"]) if prev.get("depth_in") is not None else None}
        except Exception as e:
            errors.append(f"snotel {type(e).__name__}: {e}")
    errors += [f"{s['source']}: {s['error']}" for s in sources if s["error"]]
    if not runs:
        errors.append("no runs read")
    return {"fetched_at": fetched_at, "local_time": loc.isoformat(timespec="seconds"), "date": day,
            "report_time": report_time, "ok": not errors, "errors": errors, "sources": sources, "runs": runs,
            "snow": snow, "model": model, "model_days": history, "snotel": snotel}


def merge(resort_id, check):
    """Adds a check to its day file: per run, one status and one groomed flag per check."""
    path = DAYS / resort_id / f"{check['date']}.json"
    day = json.loads(path.read_text(encoding="utf-8")) if path.exists() else \
        {"resort": resort_id, "date": check["date"], "checks": [], "runs": {}, "model_days": {}}
    if any(c["fetched_at"] == check["fetched_at"] for c in day["checks"]):
        return path
    n = len(day["checks"])
    day["checks"].append({k: check[k] for k in check if k not in ("runs", "model_days")})
    for r in check["runs"]:
        item = day["runs"].setdefault(r["name"], {"difficulty": r["difficulty"], "area": r["area"],
                                                  "status": [None] * n, "groomed": [None] * n})
        item.update(difficulty=r["difficulty"] or item["difficulty"], area=r["area"] or item["area"])
        item["status"].append(r["status"])
        item["groomed"].append(r["groomed"])
    for item in day["runs"].values():          # runs missing from this check get a gap
        for key in ("status", "groomed"):
            item[key] += [None] * (n + 1 - len(item[key]))
    day["model_days"].update(check["model_days"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


# --- run names: the resort's spelling to the map's (the app's rule: exact, alias file, Crystal's list, Upper/Lower) ---

def norm(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[’'`\.]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^the ", "", s)
    return s.replace(" avenue", " ave").replace("saint ", "st ")


def page_data(page):
    path = REPO / "app" / f"{page}.html"
    if not path.exists():
        return None
    s = path.read_text(encoding="utf-8")
    return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', s, re.S).group(1))


class Names:
    def __init__(self, resort):
        data = page_data(resort.get("page") or "") if resort.get("page") else None
        self.ratings = (data or {}).get("ratings", {})
        self.map = sorted(set(self.ratings) | {p["name"] for p in (data or {}).get("map", {}).get("pieces", [])
                                               if p.get("name")})
        self.by_norm, self.alias = {}, {}
        for n in self.map:
            self.by_norm.setdefault(norm(n), []).append(n)
        alias_path = REPO / "app" / "aliases" / f"{resort.get('page')}.json"
        if alias_path.exists():
            for k, v in json.loads(alias_path.read_text(encoding="utf-8")).items():
                if not k.startswith("_"):
                    self.alias[norm(k)] = [] if v is None else [v] if isinstance(v, str) else v
        extra = HERE / f"{resort['id']}_names.json"
        if extra.exists():
            for osm, v in json.loads(extra.read_text(encoding="utf-8")).items():
                if not osm.startswith("_") and isinstance(v, dict) and v.get("as"):
                    self.alias.setdefault(norm(v["as"]), []).append(osm)

    def match(self, name):
        """Map names for a report name: [] when it has no line on the map, None when unknown."""
        if not self.map:
            return None
        k = norm(name)
        if k in self.alias:
            return [n for n in self.alias[k] if n in self.map]
        if k in self.by_norm:
            return self.by_norm[k]
        parts = [n for p in ("upper ", "lower ", "middle ", "mid ") for n in self.by_norm.get(p + k, [])]
        if parts:
            return parts
        bare = re.sub(r"^(upper|lower|middle|mid) ", "", k)
        if bare != k and bare in self.by_norm:
            return self.by_norm[bare]
        squeezed = [n for kk, ns in self.by_norm.items() if kk.replace(" ", "") == k.replace(" ", "") for n in ns]
        return squeezed or None


# --- the log ---

LOG_COLUMNS = ["date", "weekday", "resort", "run", "map_names", "difficulty", "area", "pod", "guessed", "open",
               "groomed", "groomed_by_check", "checks", "report_time", "new_overnight_in", "new_24h_in",
               "new_48h_in", "new_72h_in", "new_snow_source", "base_in", "temp_f", "snotel_depth_change_in",
               "model_24h_in", "days_since_2in"]


def inches(cm, digits=1):
    return None if cm is None else round(cm / CM_PER_IN, digits)


def first_value(checks, section, key):
    for c in checks:
        v = (c.get(section) or {}).get(key)
        if v is not None:
            return v
    return None


def rebuild_log():
    config = load_config()
    rows = []
    for resort in config["resorts"]:
        folder = DAYS / resort["id"]
        if not folder.exists():
            continue
        names = Names(resort)
        days = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
        # Daily new snow (24 h to the morning report): the resort's figure, else the model's
        snow24 = {}
        for d in days:
            for k, v in d.get("model_days", {}).items():
                snow24.setdefault(k, ("model", v))
        for d in days:
            reported = first_value(d["checks"], "snow", "new_24h_cm")
            if reported is None:
                reported = first_value(d["checks"], "snow", "overnight_cm")
            if reported is not None:
                snow24[d["date"]] = ("resort", reported)
        for d in days:
            use = [i for i, c in enumerate(d["checks"]) if int(c["local_time"][11:13]) >= FIRST_HOUR]
            if not use:
                continue
            checks = [d["checks"][i] for i in use]
            day = date.fromisoformat(d["date"])
            since = None
            for back in range(0, 15):
                v = snow24.get((day - timedelta(days=back)).isoformat())
                if v is None:
                    break
                if v[1] >= BIG_SNOW_CM:
                    since = back
                    break
            new24 = snow24.get(d["date"])
            temp = bt.first_known(first_value(checks, "snow", "temp_c"), first_value(checks, "model", "temp_c"))
            common = {
                "date": d["date"], "weekday": day.strftime("%a"), "resort": resort["id"], "checks": len(checks),
                "report_time": next((c["report_time"] for c in reversed(checks) if c.get("report_time")), None),
                "new_overnight_in": inches(first_value(checks, "snow", "overnight_cm")),
                "new_24h_in": inches(new24[1]) if new24 else None, "new_snow_source": new24[0] if new24 else None,
                "new_48h_in": inches(first_value(checks, "snow", "new_48h_cm")),
                "new_72h_in": inches(first_value(checks, "snow", "new_72h_cm")),
                "base_in": inches(max([c["snow"]["base_cm"] for c in checks
                                       if (c.get("snow") or {}).get("base_cm") is not None], default=None), 0),
                "temp_f": None if temp is None else round(temp * 9 / 5 + 32),
                "snotel_depth_change_in": first_value(checks, "snotel", "depth_change_in"),
                "model_24h_in": inches(d.get("model_days", {}).get(d["date"])),
                "days_since_2in": since if since is not None else None}
            for name, r in sorted(d["runs"].items()):
                matched = names.match(name)
                guessed = sorted({(names.ratings.get(n) or {}).get("grooming") for n in matched or []} - {None})
                pods = sorted({(names.ratings.get(n) or {}).get("pod") for n in matched or []} - {None})
                groomed = [r["groomed"][i] for i in use]
                status = [r["status"][i] for i in use]
                if all(s is None for s in status):
                    continue
                rows.append(dict(common, run=name, map_names="; ".join(matched) if matched else
                                 ("(no line on the map)" if matched == [] else ""),
                                 difficulty=r["difficulty"], area=r["area"], pod="; ".join(pods),
                                 guessed="; ".join(guessed),
                                 open="Y" if "open" in status else "N",
                                 groomed="Y" if any(g is True for g in groomed) else
                                 ("N" if any(g is False for g in groomed) else ""),
                                 groomed_by_check="".join("Y" if g is True else "N" if g is False else "-"
                                                          for g in groomed)))
    with open(LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# --- commands ---

def collect(only=None):
    config = load_config()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    problems = 0
    for resort in config["resorts"]:
        if only and resort["id"] not in only:
            continue
        if not resort.get("enabled", True):
            continue
        check = check_resort(resort, now)
        merge(resort["id"], check)
        n_open = sum(r["status"] == "open" for r in check["runs"])
        n_groomed = sum(r["groomed"] is True for r in check["runs"])
        problems += not check["ok"]
        log(f"collect {check['fetched_at']} {resort['id']} ({check['local_time'][:16]} local): {len(check['runs'])} runs, "
            f"{n_open} open, {n_groomed} groomed, report {check['report_time']}"
            + "".join(f"\n  ERROR {e}" for e in check["errors"]))
    n = rebuild_log()
    log(f"log.csv: {n} rows")
    return 1 if problems else 0


def backfill_banfftrack():
    """banfftrack's saved checks (Sunshine and Lake Louise) as grooming checks, from its raw archive."""
    config = bt.load_config()
    resorts = {r["id"]: r for r in load_config()["resorts"]}
    rows = [json.loads(line) for line in bt.MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    runs = {}
    for row in rows:
        runs.setdefault((row["fetched_at"], row["resort"]), []).append(row)
    added = 0
    for (fetched_at, rid), fetched in sorted(runs.items()):
        bodies = {r["source"]: bt.read_raw(r) for r in fetched if not r["error"] and r.get("path")}
        btr = next(r for r in config["resorts"] if r["id"] == rid)
        parsed, model, _ = bt.parse_sources(btr, fetched_at, bodies, config)
        snap = bt.build_snapshot(btr, fetched_at, parsed, model, [], config)
        mid = next((z for z in snap["zones"] if z["zone"] == "mid"), None) or {}
        upper = next((z for z in snap["zones"] if z["zone"] == "summit"), None) or {}
        snow_zone = upper if rid == "louise" else mid
        s = snow_zone.get("snow") or {}
        m = mid.get("model") or {}
        check = {"fetched_at": fetched_at, "local_time": snap["local_time"], "date": snap["date"],
                 "report_time": snap["report_updated"].get("conditions") or snap["report_updated"].get("feed"),
                 "ok": snap["ok"], "errors": snap["errors"],
                 "sources": [{"source": r["source"], "url": r["url"], "http_status": r["http_status"],
                              "bytes": r["bytes"], "sha256": r["sha256"], "error": r["error"]} for r in fetched],
                 "runs": [{"name": r["name"], "difficulty": r["level"], "area": r["area"], "status": r["status"],
                           "groomed": r["groomed"]} for r in snap["runs"]],
                 "snow": {"overnight_cm": s.get("new_overnight_cm"), "new_24h_cm": s.get("new_24h_cm"),
                          "new_48h_cm": s.get("new_48h_cm"), "new_72h_cm": None, "new_7d_cm": s.get("new_7d_cm"),
                          "base_cm": snap["season"]["base_cm"],
                          "temp_c": (mid.get("station") or {}).get("temp_c")},
                 "model": {k: m.get(k) for k in ("temp_c", "tmin_24h_c", "tmax_24h_c", "snow_24h_cm", "snow_48h_cm",
                                                 "snow_72h_cm", "snow_7d_cm", "hours_above_0_24h", "rain_24h_mm")},
                 "model_days": {}, "snotel": None, "from": "banfftrack"}
        if rid in resorts:
            merge(rid, check)
            added += 1
    n = rebuild_log()
    log(f"backfill-banfftrack: {added} checks; log.csv: {n} rows")
    return 0


def main(argv):
    if sys.stdout is not None:
        sys.stdout.reconfigure(errors="replace")
    command = argv[1] if len(argv) > 1 else "collect"
    if command == "collect":
        return collect(set(argv[2:]) or None)
    if command == "log":
        print(f"log.csv: {rebuild_log()} rows")
        return 0
    if command == "backfill-banfftrack":
        return backfill_banfftrack()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
