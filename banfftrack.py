"""Banff conditions tracker: an hourly log of lifts, runs, grooming and snow at Sunshine and Lake Louise.

Each `collect` saves one snapshot per resort: every lift and run (open, closed or standby, and groomed), the
resort's snow report, stations and forecast, and model weather at the base, mid and summit with a snow-quality
read. Every response is kept gzipped under data/raw, so `rebuild` can re-parse everything after a fix or a rule
change. The app reads out/ (see DATA_FORMAT.md).

Sources
  sunshine-lifts       skibanff.com lifts and trails report (HTML): lifts, runs, groomed, standby, lift hours
  sunshine-conditions  skibanff.com current conditions (HTML): snow by zone, summary, RWDI forecast
  louise-feed          ski-louise-status-page.vercel.app/api/ski-data, the resort's XML feed: lifts, runs,
                       groomed, three weather stations, snow by zone, forecast
  louise-grooming-pdf, louise-runs-pdf   the resort's own PDF reports (lamp10.skilouise.com), archived only
  open-meteo           model weather at each zone's elevation, 7 days back and 3 ahead

Usage
  python banfftrack.py collect [--scheduled]   one snapshot; --scheduled does nothing outside the window
  python banfftrack.py rebuild                 re-parse every saved response into a new database and out/
  python banfftrack.py summary [YYYY-MM-DD]    the day's digest (default today in Banff), also out/digest/
  python banfftrack.py check                   validate every field of out/latest.json
  python banfftrack.py export [folder]         CSVs of every table (default Downloads\\banfftrack)
"""

import csv
import gzip
import hashlib
import html
import json
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from math import exp
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "raw"
MANIFEST = RAW / "manifest.jsonl"
DB_PATH = DATA / "banfftrack.db"
OUT = HERE / "out"
LOG_PATH = HERE / "banfftrack.log"
CONFIG_PATH = HERE / "resorts.json"
FORMAT = "banfftrack/1"
USER_AGENT = "Mozilla/5.0 (compatible; banfftrack/1.0; personal hourly conditions log)"
SEASON_BASE_CM = (100, 150)  # season by mid-mountain base: under 100 cm early, 100-150 mid, 150 and up late

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetches (
    fetched_at  TEXT NOT NULL,     -- UTC start of the collect run
    resort      TEXT NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT,
    http_status INTEGER,
    bytes       INTEGER,
    sha256      TEXT,
    path        TEXT,              -- gzipped body under data/raw (identical bodies share a file)
    error       TEXT,
    PRIMARY KEY (fetched_at, resort, source)
);
CREATE TABLE IF NOT EXISTS snapshots (
    resort     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    local_date TEXT NOT NULL,      -- Banff date
    slot       TEXT NOT NULL,      -- Banff hour the snapshot stands for, HH:00
    ok         INTEGER NOT NULL,
    errors     TEXT,
    json       TEXT NOT NULL,      -- the whole snapshot, as written to out/latest_<resort>.json
    PRIMARY KEY (resort, fetched_at)
);
CREATE TABLE IF NOT EXISTS lift_status (
    fetched_at TEXT, resort TEXT, id TEXT, name TEXT, area TEXT, status TEXT, status_raw TEXT, hours TEXT,
    PRIMARY KEY (resort, fetched_at, id)
);
CREATE TABLE IF NOT EXISTS run_status (
    fetched_at TEXT, resort TEXT, id TEXT, name TEXT, area TEXT, level TEXT, status TEXT, status_raw TEXT,
    groomed INTEGER,
    PRIMARY KEY (resort, fetched_at, id)
);
CREATE TABLE IF NOT EXISTS zone_conditions (
    fetched_at TEXT, resort TEXT, zone TEXT, elev_m INTEGER,
    base_cm REAL, new_overnight_cm REAL, new_24h_cm REAL, new_48h_cm REAL, new_7d_cm REAL, season_cm REAL,
    station_temp_c REAL, station_wind_kmh REAL,
    model_temp_c REAL, model_tmin_24h_c REAL, model_tmax_24h_c REAL, model_snow_24h_cm REAL,
    model_snow_7d_cm REAL, model_rain_48h_mm REAL, model_wind_max_24h_kmh REAL, freezing_level_m REAL,
    quality_rule TEXT, quality_code TEXT, quality_label TEXT, quality_inputs TEXT,
    PRIMARY KEY (resort, fetched_at, zone)
);
"""


def log(message):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


# --- time: Banff is a fixed UTC-6 (see resorts.json) ---

def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def banff(dt, config):
    return dt.astimezone(timezone(timedelta(hours=config["utc_offset_hours"])))


def minutes(slot):
    hours, mins = slot.split(":")
    return int(hours) * 60 + int(mins)


def slot_of(local, config):
    """The scheduled slot a check stands for: the latest one at or before it, if under an hour ago. Scheduled runs
    start 5 minutes after their slot and GitHub may start them later, never earlier. Other times fall to the half
    hour."""
    now = local.hour * 60 + local.minute
    past = [s for s in config["window"]["slots"] if minutes(s) <= now]
    if past and now - minutes(past[-1]) < 60:
        return past[-1]
    return f"{local.hour:02d}:{local.minute - local.minute % 30:02d}"


def in_window(local, config):
    w = config["window"]
    return (date.fromisoformat(w["start"]) <= local.date() <= date.fromisoformat(w["end"])
            and slot_of(local, config) in w["slots"])


# --- fetching and the raw archive ---

def fetch(url, accept="*/*"):
    """Returns (http status, body bytes, error text)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.status, response.read(), None
        except urllib.error.HTTPError as e:
            error = f"HTTP {e.code}"
            if e.code < 500:
                return e.code, e.read(), error
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    return None, None, error


# The archive is public, so Sunshine's pages are kept only from the data block to the end of the page's main content
# (the parsers read nothing else), and Louise's PDFs only by hash and size. If a marker is missing, the whole page is
# kept, so a layout change loses nothing.
TRIMS = {"sunshine-lifts": ('id="block-lifts-trails"', "</main>"),
         "sunshine-conditions": ("block-feeds-weather", "</main>")}


def trim(source, body):
    if source not in TRIMS or body is None:
        return body
    page = body.decode("utf-8", "replace")
    start, end = page.find(TRIMS[source][0]), page.find(TRIMS[source][1])
    if start < 0 or end < start:
        return body
    start = page.rfind("<div", 0, start)
    return page[start:end].encode("utf-8")


def archive(con, fetched_at, local_day, resort, source, url, status, body, error, ext, keep_body=True):
    row = {"fetched_at": fetched_at, "resort": resort, "source": source, "url": url, "http_status": status,
           "bytes": len(body) if body is not None else None, "sha256": None, "path": None, "error": error}
    if body is not None:
        row["sha256"] = hashlib.sha256(body).hexdigest()
        stored = trim(source, body)
        if stored is not body:
            row.update(trimmed=True, stored_bytes=len(stored))
        if keep_body:
            sha = hashlib.sha256(stored).hexdigest()
            rel = f"{local_day}/{sha[:20]}.{ext}.gz"
            path = RAW / rel
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(stored))
            row["path"] = rel
    RAW.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    save_fetch(con, row)
    return row


def save_fetch(con, row):
    con.execute("INSERT OR REPLACE INTO fetches VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(row[k] for k in ("fetched_at", "resort", "source", "url", "http_status", "bytes",
                                       "sha256", "path", "error")))


def read_raw(row):
    return gzip.decompress((RAW / row["path"]).read_bytes()) if row.get("path") else None


# --- shared helpers ---

def text_of(fragment):
    """HTML fragment to plain text: block tags become line breaks, inline tags vanish."""
    t = re.sub(r"<(?:br|/?p|/?div|/?h\d|/?li|/?ul|/?summary|/?details)\b[^>]*>", "\n", fragment or "")
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\r", "")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def num(value):
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    try:
        n = float(value)
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def first_known(*values):
    return next((v for v in values if v is not None), None)


def name_key(name):
    """Lower-case name for matching against the map's run names."""
    t = (name or "").lower().replace("’", "'").replace("&", " and ")
    t = re.sub(r"'s\b", "s", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def norm_status(raw):
    s = (raw or "").strip().lower()
    if not s:
        return None
    if "open" in s and "not" not in s:
        return "open"
    if any(w in s for w in ("standby", "hold", "delay", "pending")):
        return "standby"
    if any(w in s for w in ("expect", "schedul", "soon")):
        return "expected"
    if "clos" in s:
        return "closed"
    return "other"


LEVELS = {"easiest": "green", "beginner": "green", "more difficult": "blue", "intermediate": "blue",
          "most difficult": "black", "advanced": "black", "experts only": "double", "expert": "double"}

TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.I)


def timed_notes(text):
    """Sentences in the resort's text that give a time, e.g. 'Mad Trappers, open from 10:30 am'."""
    notes = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        for m in TIME_RE.finditer(sentence):
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower().startswith("p") else 0)
            notes.append({"time": f"{hour:02d}:{m.group(2) or '00'}", "text": sentence.strip()})
    return notes


# --- Sunshine (skibanff.com, server-rendered HTML) ---

NODE_RE = re.compile(r'<div data-history-node-id="(\d+)" class="node node--type-(lift|trail)\b')
AREA_RE = re.compile(r'<summary class="f-icon m-0 h3">\s*(.*?)\s*</summary>', re.S)
ICON_RE = re.compile(r'f-icon icon-(status|level|condition) icon-\d+">\s*<span class="visually-hidden">\s*(.*?)\s*'
                     r'</span>', re.S)
HOURS_RE = re.compile(r'<div class="[^"]*\bhours">\s*(.*?)\s*</div>', re.S)
VALUE_RE = re.compile(r'<span class="value">\s*(-?[\d.]+)\s*</span>\s*<(?:span|sup) class="t-label unit">\s*(.*?)'
                      r'\s*</(?:span|sup)>', re.S)


def parse_sunshine_lifts(page):
    areas = [(m.start(), html.unescape(m.group(1))) for m in AREA_RE.finditer(page)]
    nodes = list(NODE_RE.finditer(page))
    lifts, runs = [], []
    for i, m in enumerate(nodes):
        end = nodes[i + 1].start() if i + 1 < len(nodes) else len(page)
        chunk = page[m.start():end].split("</details>")[0]
        icons = {}
        for kind, label in ICON_RE.findall(chunk):
            icons.setdefault(kind, []).append(html.unescape(label).strip())
        hours = HOURS_RE.search(chunk)
        bare = re.sub(r'<span class="visually-hidden">.*?</span>', " ", HOURS_RE.sub(" ", chunk), flags=re.S)
        name = next((line for line in text_of(bare).split("\n") if line.strip()), "")
        status_raw = (icons.get("status") or [None])[0]
        item = {"id": m.group(1), "name": name, "key": name_key(name), "status": norm_status(status_raw),
                "status_raw": status_raw}
        if m.group(2) == "lift":
            item.update(area=None, hours=html.unescape(hours.group(1)).strip() if hours else None)
            lifts.append(item)
        else:
            raw_level = (icons.get("level") or [None])[0]
            conditions = icons.get("condition") or []
            item.update(area=next((a for pos, a in reversed(areas) if pos < m.start()), None),
                        level=LEVELS.get((raw_level or "").lower()), level_raw=raw_level,
                        groomed="Groomed" in conditions, conditions=conditions)
            runs.append(item)
    totals = {f"{kind.lower()}_{key}": int(v)
              for v, total, kind in re.findall(r'value">(\d+)</div>\s*<div[^>]*>of (\d+)</div>\s*</div>\s*'
                                               r'<figcaption[^>]*>(\w+) Open', page)
              for key, v in (("open", v), ("total", total))}
    if not lifts or not runs:
        raise ValueError(f"page layout changed: {len(lifts)} lifts, {len(runs)} runs found")
    return {"lifts": lifts, "runs": runs, "totals": totals}


def local_stamp(text):
    """'Sunday, November 15th, 2026 9:41 AM' (Banff time) to '2026-11-15T09:41:00-06:00'."""
    try:
        dt = datetime.strptime(re.sub(r"(\d)(st|nd|rd|th),", r"\1,", text or ""), "%A, %B %d, %Y %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone(timedelta(hours=load_config()["utc_offset_hours"]))).isoformat()


def snow_field(label):
    s = label.lower()
    for pattern, field in (("overnight", "new_overnight_cm"), ("24", "new_24h_cm"), ("48", "new_48h_cm"),
                           ("72", "new_72h_cm"), ("7 day", "new_7d_cm"), ("base", "base_cm"),
                           ("cumulative", "season_cm"), ("season", "season_cm"), ("year", "season_cm")):
        if pattern in s:
            return field
    return None


def parse_sunshine_conditions(page):
    out = {"updated": None, "summary": None, "zones": {}, "forecast": None}
    m = re.search(r"Updated:\s*([^<]+?)\s*</p>", page)
    out["updated"] = m and m.group(1).strip()
    out["updated_local"] = local_stamp(out["updated"])
    m = re.search(r"<h3>Summary</h3>(.*?)</p>", page, re.S)
    out["summary"] = m and text_of(m.group(1))
    groups = list(re.finditer(r'<h3 class="snow-group-title">(.*?)</h3>', page, re.S))
    for i, g in enumerate(groups):
        end = groups[i + 1].start() if i + 1 < len(groups) else page.find("Extended Forecast", g.start())
        chunk = page[g.end():end if end > 0 else len(page)]
        title = text_of(g.group(1))
        zone = {"title": title, "values": {}, "other": {}}
        cards = list(re.finditer(r"<h4>(.*?)</h4>", chunk, re.S))
        for j, c in enumerate(cards):
            body = chunk[c.end():cards[j + 1].start() if j + 1 < len(cards) else len(chunk)]
            v = VALUE_RE.search(body)
            label = text_of(c.group(1))
            value = num(v.group(1)) if v else None
            if v and v.group(2).strip() not in ("cm", ""):
                zone["other"][label] = f"{v.group(1)} {v.group(2)}"
                continue
            field = snow_field(label)
            if field:
                zone["values"][field] = value
            else:
                zone["other"][label] = value
        out["zones"][title.replace(" Snow Report", "")] = zone
    start = page.find("Extended Forecast")
    if start > 0:
        section = page[start:].split("Weather forecasting data")[0]
        fc = {"updated": None, "summary": None, "periods": []}
        m = re.search(r"Forecast updated:\s*([^<]+?)\s*</p>", section)
        fc["updated"] = m and m.group(1).strip()
        fc["updated_local"] = local_stamp(fc["updated"])
        m = re.search(r"<h3>Summary</h3>(.*?)</p>", section, re.S)
        fc["summary"] = m and text_of(m.group(1))
        heads = [h for h in re.finditer(r"<h3>([A-Z][A-Za-z ]+)</h3>", section) if h.group(1) != "Summary"]
        for j, h in enumerate(heads):
            card = section[h.end():heads[j + 1].start() if j + 1 < len(heads) else len(section)]

            def first(pattern):
                found = re.search(pattern, card, re.S)
                return num(found.group(1)) if found else None
            wind = re.search(r"Ridge wind\s*([A-Z]*)\s*:</strong>.*?<span class=\"value\">\s*(\d+)", card, re.S)
            precip = re.search(r'condition--precipitation">(.*?)</div>', card, re.S)
            summary = re.search(r'<p class="forecast-summary">(.*?)</p>', card, re.S)
            fc["periods"].append({
                "period": h.group(1),
                "high_c": first(r'<div class="high">.*?<span class="value">\s*(-?[\d.]+)'),
                "low_c": first(r'<div class="low">.*?<span class="value">\s*(-?[\d.]+)'),
                "summary": summary and text_of(summary.group(1)),
                "freezing_level_m": first(r'condition--freezing-level">.*?<span class="value">\s*(-?[\d.]+)'),
                "ridge_wind_dir": wind and wind.group(1) or None,
                "ridge_wind_kmh": wind and num(wind.group(2)),
                "precip": precip and re.sub(r"^Precipitation:\s*", "", text_of(precip.group(1)).replace("\n", " "))})
        out["forecast"] = fc
    if not out["zones"]:
        raise ValueError("page layout changed: no snow report found")
    return out


# --- Lake Louise (the resort's XML feed, served by ski-louise-status-page.vercel.app) ---

def parse_louise(body):
    root = ET.fromstring(body)
    cc = root.find("currentConditions")
    out = {"updated": root.get("updated"), "operations": dict(root.find("operations").attrib),
           "resortwide": dict(cc.find("resortwide").attrib), "zones": {}, "lifts": [], "runs": [], "parks": []}
    for loc in cc.find("resortLocations"):
        a = loc.attrib
        out["zones"][a.get("name")] = {
            "station": a.get("stationName"), "observed_at": a.get("weatherTimestamp") or None,
            "temp_c": num(a.get("temperature")), "wind_kmh": num(a.get("windSpeed")),
            "wind_dir": a.get("windDirection") or None, "surface": a.get("primarySurface") or None,
            "values": {"base_cm": num(a.get("base")), "new_overnight_cm": num(a.get("snowOverNight")),
                       "new_24h_cm": num(a.get("snow24Hours")), "new_48h_cm": num(a.get("snow48Hours")),
                       "new_7d_cm": num(a.get("snow7Days")), "season_cm": num(a.get("snowYearToDate"))}}
    lr = cc.find("liftsAndRuns")
    out["lifts_and_runs"] = dict(lr.attrib) if lr is not None else {}
    out["terrain_park"] = dict(cc.find("terrainPark").attrib) if cc.find("terrainPark") is not None else None
    info = cc.find("info")
    out["summary"] = text_of(info.get("html")) if info is not None else None
    out["forecast"] = {"periods": [{"period": d.get("name"), "high_c": num(d.get("high")),
                                    "low_c": num(d.get("low")), "summary": d.get("weather")}
                                   for d in root.iter("day")]}
    for area in root.find("facilities").find("areas").findall("area"):
        area_name = area.get("name")
        for lift in area.iter("lift"):
            a = lift.attrib
            out["lifts"].append({"id": a.get("id"), "name": a.get("name"), "key": name_key(a.get("name")),
                                 "area": area_name, "status": norm_status(a.get("status")),
                                 "status_raw": a.get("status"), "realtime": a.get("realtime"), "hours": None})
        for trail in area.iter("trail"):
            a = trail.attrib
            out["runs"].append({"id": a.get("id"), "name": a.get("name"), "key": name_key(a.get("name")),
                                "area": area_name, "level": LEVELS.get((a.get("difficulty") or "").lower()),
                                "level_raw": a.get("difficulty"), "status": norm_status(a.get("status")),
                                "status_raw": a.get("status"), "groomed": (a.get("groomed") or "").lower() == "yes"})
        for park in area.iter("park"):
            a = park.attrib
            out["parks"].append({"id": a.get("id"), "name": a.get("name"), "status": norm_status(a.get("status")),
                                 "status_raw": a.get("status"),
                                 "groomed": (a.get("groomedOrCut") or "").lower() == "yes"})
    if not out["lifts"] or not out["runs"]:
        raise ValueError(f"feed layout changed: {len(out['lifts'])} lifts, {len(out['runs'])} runs found")
    return out


# --- model weather (Open-Meteo) ---

OM_HOURLY = ("temperature_2m,relative_humidity_2m,precipitation,rain,snowfall,weather_code,cloud_cover,"
             "wind_speed_10m,wind_gusts_10m,wind_direction_10m,freezing_level_height,visibility")
LIFT_HOURS = range(9, 17)  # hourly samples 9:00-16:00 Banff cover the 9:30-4:30 lift day


def open_meteo_url(zones):
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": ",".join(str(z["lat"]) for z in zones), "longitude": ",".join(str(z["lon"]) for z in zones),
        "elevation": ",".join(str(z["elev_m"]) for z in zones), "hourly": OM_HOURLY, "past_days": 7,
        "forecast_days": 3, "timezone": "GMT", "wind_speed_unit": "kmh"})


def lift_day(h, day, offset_hours):
    """Forecast for one Banff day's lift hours, so the morning step needn't call Open-Meteo itself (Claude's cloud
    gets HTTP 429 from it)."""
    index = {t: k for k, t in enumerate(h["time"])}
    hours = []
    for local_hour in LIFT_HOURS:
        utc = datetime(day.year, day.month, day.day, local_hour) - timedelta(hours=offset_hours)
        k = index.get(utc.strftime("%Y-%m-%dT%H:00"))
        if k is None:
            continue

        def at(var):
            return (h.get(var) or [None] * len(h["time"]))[k]
        hours.append({"time": f"{local_hour:02d}:00", "temp_c": at("temperature_2m"), "snow_cm": at("snowfall"),
                      "rain_mm": at("rain"), "cloud_pct": at("cloud_cover"), "wind_kmh": at("wind_speed_10m"),
                      "gust_kmh": at("wind_gusts_10m"), "vis_m": at("visibility"), "weather_code": at("weather_code")})
    if not hours:
        return None

    def known(key):
        return [x[key] for x in hours if x[key] is not None]
    return {"date": day.isoformat(), "snow_cm": round(sum(known("snow_cm")), 1),
            "rain_mm": round(sum(known("rain_mm")), 1), "temp_min_c": min(known("temp_c"), default=None),
            "temp_max_c": max(known("temp_c"), default=None), "wind_max_kmh": max(known("wind_kmh"), default=None),
            "gust_max_kmh": max(known("gust_kmh"), default=None),
            "cloud_mean_pct": round(sum(known("cloud_pct")) / len(known("cloud_pct"))) if known("cloud_pct") else None,
            "vis_min_m": min(known("vis_m"), default=None), "hours": hours}


def zone_model(h, now, offset_hours=-6):
    """Model weather around `now` from one location's hourly arrays (UTC times)."""
    hour = now.strftime("%Y-%m-%dT%H:00")
    if hour not in h["time"]:
        return None
    i = h["time"].index(hour)
    today = (now + timedelta(hours=offset_hours)).date()

    def vals(var, a, b):
        return [x for x in h[var][max(a, 0):max(b, 0)] if x is not None]

    def past(var, n):
        return vals(var, i - n + 1, i + 1)

    def nxt(var, n):
        return vals(var, i + 1, i + 1 + n)

    def total(xs):
        return round(sum(xs), 1)

    temps24, snow24 = h["temperature_2m"][max(i - 23, 0):i + 1], h["snowfall"][max(i - 23, 0):i + 1]
    weighted = [(t, s) for t, s in zip(temps24, snow24) if t is not None and s]
    thaw = next((k for k in range(i, max(i - 168, -1), -1) if (h["temperature_2m"][k] or -99) > 0), None)
    snowed = next((k for k in range(i, max(i - 168, -1), -1) if (h["snowfall"][k] or 0) >= 0.1), None)
    return {
        "temp_c": h["temperature_2m"][i], "humidity_pct": h["relative_humidity_2m"][i],
        "tmin_24h_c": min(past("temperature_2m", 24), default=None),
        "tmax_24h_c": max(past("temperature_2m", 24), default=None),
        "hours_above_0_24h": sum(1 for t in past("temperature_2m", 24) if t > 0),
        "hours_above_0_72h": sum(1 for t in past("temperature_2m", 72) if t > 0),
        "hours_since_thaw": i - thaw if thaw is not None else None,
        "snow_since_thaw_cm": total(vals("snowfall", thaw + 1, i + 1)) if thaw is not None else None,
        "hours_since_snow": i - snowed if snowed is not None else None,
        "snow_1h_cm": h["snowfall"][i], "snow_12h_cm": total(past("snowfall", 12)),
        "snow_24h_cm": total(past("snowfall", 24)), "snow_48h_cm": total(past("snowfall", 48)),
        "snow_72h_cm": total(past("snowfall", 72)), "snow_7d_cm": total(past("snowfall", 168)),
        "snow_temp_c": round(sum(t * s for t, s in weighted) / sum(s for _, s in weighted), 1) if weighted else None,
        "rain_24h_mm": total(past("rain", 24)), "rain_48h_mm": total(past("rain", 48)),
        "precip_24h_mm": total(past("precipitation", 24)),
        "wind_kmh": h["wind_speed_10m"][i], "gust_kmh": h["wind_gusts_10m"][i],
        "wind_dir_deg": h["wind_direction_10m"][i],
        "wind_max_24h_kmh": max(past("wind_speed_10m", 24), default=None),
        "gust_max_24h_kmh": max(past("wind_gusts_10m", 24), default=None),
        "freezing_level_m": h["freezing_level_height"][i], "cloud_pct": h["cloud_cover"][i],
        "weather_code": h["weather_code"][i],
        "next_24h": {"snow_cm": total(nxt("snowfall", 24)), "rain_mm": total(nxt("rain", 24)),
                     "tmin_c": min(nxt("temperature_2m", 24), default=None),
                     "tmax_c": max(nxt("temperature_2m", 24), default=None),
                     "wind_max_kmh": max(nxt("wind_speed_10m", 24), default=None)},
        "next_72h_snow_cm": total(nxt("snowfall", 72)),
        "lift_hours": lift_day(h, today, offset_hours),
        "lift_hours_tomorrow": lift_day(h, today + timedelta(days=1), offset_hours),
    }


def parse_open_meteo(body, zones, now, offset_hours=-6):
    data = json.loads(body)
    data = data if isinstance(data, list) else [data]
    if len(data) != len(zones):
        raise ValueError(f"open-meteo returned {len(data)} locations for {len(zones)} zones")
    for d in data:
        units = d.get("hourly_units", {})
        if units.get("snowfall") != "cm" or units.get("wind_speed_10m") != "km/h" or \
                units.get("visibility", "m") != "m":
            raise ValueError(f"open-meteo units changed: {units}")
    return {z["zone"]: zone_model(d["hourly"], now, offset_hours) for z, d in zip(zones, data)}


# --- snow quality (rule v1: change the numbers here, then `rebuild`) ---

RULE = "v1"
Q = {"new_cm": 5, "dusting_cm": 1, "cold_dry_c": -10, "dry_c": -4, "dense_c": -1, "wet_now_c": 1.0,
     "refreeze_c": -1.0, "thaw_hours": 2, "rain_mm": 1.0, "wind_kmh": 30, "gust_kmh": 55, "firm_after_h": 72,
     "very_cold_c": -20}


def density(temp_c):
    """Fresh-snow density from air temperature (Hedstrom & Pomeroy 1998), kg/m3."""
    return round(67.92 + 51.25 * exp(min(temp_c, 0) / 2.59)) if temp_c is not None else None


def snow_quality(inp):
    """Primary read plus flags for one zone. `inp` holds every number used, so the rule can change later."""
    t, new, ts = inp["temp_now_c"], inp["new_24h_cm"] or 0, inp["snow_temp_c"]
    rain = (inp["rain_48h_mm"] or 0) >= Q["rain_mm"]
    windy = (inp["wind_max_24h_kmh"] or 0) >= Q["wind_kmh"] or (inp["gust_max_24h_kmh"] or 0) >= Q["gust_kmh"]
    thawed = (inp["hours_above_0_72h"] or 0) >= Q["thaw_hours"]
    # Soft and wet: clearly warm now, or near 0 °C after a thaw today. Refrozen needs a real freeze (below -1 °C).
    warm = t is not None and (t >= Q["wet_now_c"] or (t > Q["refreeze_c"] and (inp["hours_above_0_24h"] or 0) >= 2))
    flags, why = [], []
    if new >= Q["dusting_cm"]:
        why.append(f"{new:g} cm new in 24 h ({inp['new_24h_source']})")
    if ts is not None and new >= Q["dusting_cm"]:
        why.append(f"fell at about {ts:.0f} °C (~{density(ts)} kg/m³)")
    if t is not None:
        why.append(f"{t:g} °C now ({inp['temp_now_source']})")
    if thawed:
        flags.append("freeze_thaw")
        why.append(f"{inp['hours_above_0_72h']} h above 0 °C in 72 h")
    if rain:
        flags.append("rain")
        why.append(f"{inp['rain_48h_mm']:g} mm rain in 48 h")
    if windy:
        flags.append("wind")
        why.append(f"wind to {inp['wind_max_24h_kmh'] or 0:.0f} km/h, gusts {inp['gust_max_24h_kmh'] or 0:.0f}")
    if t is not None and t <= Q["very_cold_c"]:
        flags.append("very_cold")

    if rain and t is not None and t > 0:
        code, label = "wet_rain", "Wet, rain-soaked snow"
    elif warm:
        code, label = "wet", "Warm, wet or soft snow"
    elif new >= Q["new_cm"]:
        flags.append("new_snow")
        temp = ts if ts is not None else t
        if temp is None or temp <= Q["cold_dry_c"]:
            code, label = "cold_dry_powder", "Cold, dry powder"
        elif temp <= Q["dry_c"]:
            code, label = "dry_powder", "Dry powder"
        elif temp <= Q["dense_c"]:
            code, label = "dense_powder", "Denser powder"
        else:
            code, label = "heavy_new_snow", "Heavy, wet new snow"
        if windy:
            label += ", wind-affected"
    elif rain:
        code, label = "rain_crust", "Rain crust, refrozen"
    elif thawed and (t is None or t <= Q["refreeze_c"]) and (inp["snow_since_thaw_cm"] or 0) < Q["new_cm"]:
        code, label = "refrozen", "Refrozen after a thaw: firm or crusty until it softens"
    elif windy and (inp["snow_72h_cm"] or 0) >= Q["new_cm"]:
        code, label = "wind_affected", "Wind-affected: packed, scoured or drifted"
    elif new >= Q["dusting_cm"]:
        code, label = "dusting", "A dusting over packed snow"
    elif inp["hours_since_snow"] is None or inp["hours_since_snow"] >= Q["firm_after_h"]:
        code, label = "packed_firm", "Packed and firm"
    else:
        code, label = "packed_powder", "Packed powder"
    return {"rule": RULE, "code": code, "label": label, "flags": flags, "why": "; ".join(why)}


# --- one snapshot per resort ---

def match_zone(reports, name):
    if not name:
        return None
    return next((v for k, v in reports.items() if k.lower().startswith(name.lower())), None)


def season_of(base_cm):
    if base_cm is None:
        return None
    return "early" if base_cm < SEASON_BASE_CM[0] else "mid" if base_cm < SEASON_BASE_CM[1] else "late"


def build_snapshot(resort, fetched_at, parsed, model, sources, config):
    now = parse_utc(fetched_at)
    local = banff(now, config)
    errors = [f"{s['source']}: {s['error']}" for s in sources if s.get("error")]
    snap = {"format": FORMAT, "resort": resort["id"], "name": resort["name"], "fetched_at": fetched_at,
            "local_time": local.isoformat(timespec="seconds"), "date": local.date().isoformat(),
            "slot": slot_of(local, config), "ok": True, "errors": errors, "report_updated": {}, "operations": None,
            "counts": {}, "lifts": [], "runs": [], "parks": [], "zones": [], "season": {}, "forecast": None,
            "text": {}, "sources": sources}
    reports = {}
    if resort["id"] == "sunshine":
        lifts, cond = parsed.get("sunshine-lifts"), parsed.get("sunshine-conditions")
        if lifts:
            snap.update(lifts=lifts["lifts"], runs=lifts["runs"])
            snap["counts"]["reported"] = lifts["totals"]
        if cond:
            snap["report_updated"]["conditions"] = cond["updated_local"] or cond["updated"]
            snap["forecast"] = cond["forecast"]
            snap["text"]["summary"] = cond["summary"]
            reports = {k: {"values": v["values"], "other": v["other"]} for k, v in cond["zones"].items()}
    else:
        feed = parsed.get("louise-feed")
        if feed:
            snap.update(lifts=feed["lifts"], runs=feed["runs"], parks=feed["parks"], forecast=feed["forecast"])
            snap["report_updated"]["feed"] = feed["updated"]
            snap["operations"] = {**feed["operations"], **{k: feed["resortwide"].get(k) for k in
                                                         ("lastSnowfallDate", "lastSnowfallUpdate")}}
            snap["text"]["summary"] = feed["summary"]
            snap["text"]["lifts_info"] = feed["lifts_and_runs"].get("info")
            snap["counts"]["reported"] = {k: num(v) for k, v in feed["lifts_and_runs"].items() if k != "info"}
            reports = feed["zones"]
    if snap["lifts"]:
        snap["counts"].update({
            "lifts_open": sum(x["status"] == "open" for x in snap["lifts"]), "lifts_total": len(snap["lifts"]),
            "runs_open": sum(x["status"] == "open" for x in snap["runs"]), "runs_total": len(snap["runs"]),
            "runs_groomed": sum(bool(x["groomed"]) for x in snap["runs"]),
            "lifts_standby": sum(x["status"] in ("standby", "expected") for x in snap["lifts"])})
    snap["text"]["notes"] = timed_notes("\n".join(v for v in snap["text"].values() if isinstance(v, str)))

    for z in resort["zones"]:
        rep = match_zone(reports, z.get("report")) or {}
        values = rep.get("values") or {}
        station = {k: rep.get(k) for k in ("station", "temp_c", "wind_kmh", "wind_dir", "observed_at")} \
            if rep.get("station") else None
        m = (model or {}).get(z["zone"])
        zone = {"zone": z["zone"], "label": z["label"], "elev_m": z["elev_m"], "report_section": z.get("report"),
                "snow": {k: values.get(k) for k in ("base_cm", "new_overnight_cm", "new_24h_cm", "new_48h_cm",
                                                     "new_7d_cm", "season_cm")},
                "snow_other": rep.get("other") or {}, "station": station, "model": m, "quality": None}
        if m:
            reported_24h = values.get("new_24h_cm")
            use_station = station and station.get("temp_c") is not None
            inputs = {k: m[k] for k in ("snow_temp_c", "tmax_24h_c", "hours_above_0_24h", "hours_above_0_72h",
                                        "snow_since_thaw_cm", "hours_since_snow", "snow_72h_cm", "rain_48h_mm",
                                        "wind_max_24h_kmh", "gust_max_24h_kmh")}
            inputs.update(
                new_24h_cm=reported_24h if reported_24h is not None else m["snow_24h_cm"],
                new_24h_source="resort" if reported_24h is not None else "model",
                temp_now_c=station["temp_c"] if use_station else m["temp_c"],
                temp_now_source=f"station {station['station']}" if use_station else "model",
                model_snow_24h_cm=m["snow_24h_cm"], resort_new_overnight_cm=values.get("new_overnight_cm"))
            zone["quality"] = {**snow_quality(inputs), "inputs": inputs}
        snap["zones"].append(zone)

    by_zone = {z["zone"]: z["snow"]["base_cm"] for z in snap["zones"]}
    if by_zone.get("mid") is not None:
        base, source = by_zone["mid"], "mid-mountain report"
    elif by_zone.get("base") is not None and by_zone.get("summit") is not None:
        base, source = round((by_zone["base"] + by_zone["summit"]) / 2), "average of base and summit reports"
    else:
        base, source = next(((v, f"{k} report") for k, v in by_zone.items() if v is not None), (None, None))
    snap["season"] = {"base_cm": base, "from": source, "season": season_of(base)}
    needed = ["sunshine-lifts", "sunshine-conditions"] if resort["id"] == "sunshine" else ["louise-feed"]
    snap["ok"] = not errors and all(k in parsed for k in needed + ["open-meteo"])
    return snap


def parse_sources(resort, fetched_at, bodies, config):
    """bodies: source -> raw bytes. Returns (parsed, model, parse errors)."""
    parsers = {"sunshine-lifts": lambda b: parse_sunshine_lifts(b.decode("utf-8")),
               "sunshine-conditions": lambda b: parse_sunshine_conditions(b.decode("utf-8")),
               "louise-feed": parse_louise}
    parsed, model, errors = {}, None, {}
    for source, body in bodies.items():
        if body is None:
            continue
        try:
            if source == "open-meteo":
                model = parse_open_meteo(body, resort["zones"], parse_utc(fetched_at), config["utc_offset_hours"])
                parsed[source] = True
            elif source in parsers:
                parsed[source] = parsers[source](body)
        except Exception as e:
            errors[source] = f"parse {type(e).__name__}: {e}"
    return parsed, model, errors


def save_snapshot(con, snap):
    key = (snap["fetched_at"], snap["resort"])
    con.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
                (snap["resort"], snap["fetched_at"], snap["date"], snap["slot"], int(snap["ok"]),
                 "\n".join(snap["errors"]) or None, json.dumps(snap, ensure_ascii=False)))
    con.executemany("INSERT OR REPLACE INTO lift_status VALUES (?,?,?,?,?,?,?,?)",
                    [(*key, x["id"], x["name"], x["area"], x["status"], x["status_raw"], x.get("hours"))
                     for x in snap["lifts"]])
    con.executemany("INSERT OR REPLACE INTO run_status VALUES (?,?,?,?,?,?,?,?,?)",
                    [(*key, x["id"], x["name"], x["area"], x["level"], x["status"], x["status_raw"],
                      int(bool(x["groomed"]))) for x in snap["runs"]])
    rows = []
    for z in snap["zones"]:
        s, st, m, q = z["snow"], z["station"] or {}, z["model"] or {}, z["quality"] or {}
        rows.append((*key, z["zone"], z["elev_m"], s["base_cm"], s["new_overnight_cm"], s["new_24h_cm"],
                     s["new_48h_cm"], s["new_7d_cm"], s["season_cm"], st.get("temp_c"), st.get("wind_kmh"),
                     m.get("temp_c"), m.get("tmin_24h_c"), m.get("tmax_24h_c"), m.get("snow_24h_cm"),
                     m.get("snow_7d_cm"), m.get("rain_48h_mm"), m.get("wind_max_24h_kmh"), m.get("freezing_level_m"),
                     q.get("rule"), q.get("code"), q.get("label"), json.dumps(q.get("inputs"))))
    con.executemany("INSERT OR REPLACE INTO zone_conditions VALUES (" + ",".join("?" * 24) + ")", rows)


# --- outputs for the app (out/) ---

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def day_log(con, resort, day):
    by_slot = {}
    for slot, js in con.execute("SELECT slot, json FROM snapshots WHERE resort=? AND local_date=? "
                                "ORDER BY fetched_at", (resort, day)):
        by_slot[slot] = json.loads(js)  # the latest snapshot for each hour wins
    slots = sorted(by_slot)
    snaps = [by_slot[s] for s in slots]
    if not snaps:
        return None

    def series(kind, extra):
        items = {}
        for i, snap in enumerate(snaps):
            for x in snap[kind]:
                item = items.setdefault(x["id"], {"id": x["id"], "status": [None] * len(slots),
                                                  **({"groomed": [None] * len(slots)} if kind == "runs" else {})})
                item.update({k: x.get(k) for k in extra})
                item["status"][i] = x["status"]
                if kind == "runs":
                    item["groomed"][i] = bool(x["groomed"])
        for item in items.values():
            item["first_open"] = next((s for s, v in zip(slots, item["status"]) if v == "open"), None)
            item["standby_slots"] = [s for s, v in zip(slots, item["status"]) if v in ("standby", "expected")]
            if kind == "runs":
                item["groomed_today"] = any(item["groomed"])
        return list(items.values())

    lifts = series("lifts", ("name", "key", "area", "hours"))
    runs = series("runs", ("name", "key", "area", "level"))
    changes = []
    for kind, items in (("lift", lifts), ("run", runs)):
        for item in items:
            for i in range(1, len(slots)):
                before, after = item["status"][i - 1], item["status"][i]
                if before != after and None not in (before, after):
                    changes.append({"slot": slots[i], "kind": kind, "id": item["id"], "name": item["name"],
                                    "field": "status", "from": before, "to": after})
                if kind == "run" and item["groomed"][i - 1] is not None and \
                        item["groomed"][i] is not None and item["groomed"][i - 1] != item["groomed"][i]:
                    changes.append({"slot": slots[i], "kind": kind, "id": item["id"], "name": item["name"],
                                    "field": "groomed", "from": item["groomed"][i - 1], "to": item["groomed"][i]})
    changes.sort(key=lambda c: (c["slot"], c["kind"], c["name"]))
    weather = []
    for slot, snap in zip(slots, snaps):
        weather.append({"slot": slot, "fetched_at": snap["fetched_at"], "ok": snap["ok"],
                        "counts": snap["counts"], "report_updated": snap["report_updated"],
                        "zones": [{"zone": z["zone"], "elev_m": z["elev_m"],
                                   "temp_c": (z["quality"] or {}).get("inputs", {}).get("temp_now_c"),
                                   "station_temp_c": (z["station"] or {}).get("temp_c"),
                                   "model_temp_c": (z["model"] or {}).get("temp_c"),
                                   "wind_kmh": first_known((z["station"] or {}).get("wind_kmh"),
                                                           (z["model"] or {}).get("wind_kmh")),
                                   "new_24h_cm": (z["quality"] or {}).get("inputs", {}).get("new_24h_cm"),
                                   "snow": z["snow"], "quality": (z["quality"] or {}).get("code"),
                                   "quality_label": (z["quality"] or {}).get("label")} for z in snap["zones"]]})
    last = snaps[-1]
    return {"format": FORMAT, "resort": resort, "name": last["name"], "date": day, "slots": slots,
            "first_check": snaps[0]["fetched_at"], "last_check": last["fetched_at"],
            "lifts": lifts, "runs": runs, "changes": changes, "hourly": weather,
            "notes": [{"time": t, "text": x}
                      for t, x in sorted({(n["time"], n["text"]) for s in snaps for n in s["text"].get("notes", [])})],
            "summary_text": last["text"].get("summary")}


def typical_opening(con, resort):
    """Median first-open slot per lift and run, over every logged day it opened (the first check's slot means it
    was already listed open then)."""
    firsts = {}
    for (day,) in con.execute("SELECT DISTINCT local_date FROM snapshots WHERE resort=?", (resort,)):
        log_ = day_log(con, resort, day)
        for kind in ("lifts", "runs"):
            for item in log_[kind]:
                if item["first_open"]:
                    firsts.setdefault((kind, item["id"]), []).append(item["first_open"])
    return {k: statistics.median_low(sorted(v)) for k, v in firsts.items()}


def write_outputs(con, config, days=None):
    latest, index = {}, {}
    for resort in config["resorts"]:
        rid = resort["id"]
        row = con.execute("SELECT json FROM snapshots WHERE resort=? ORDER BY fetched_at DESC LIMIT 1",
                          (rid,)).fetchone()
        dates = [d for (d,) in con.execute("SELECT DISTINCT local_date FROM snapshots WHERE resort=? "
                                           "ORDER BY local_date", (rid,))]
        index[rid] = dates
        for day in (dates if days is None else [d for d in days if d in dates]):
            write_json(OUT / "days" / rid / f"{day}.json", day_log(con, rid, day))
        if not row:
            continue
        snap = json.loads(row[0])
        today = day_log(con, rid, snap["date"])
        yesterday = day_log(con, rid, (date.fromisoformat(snap["date"]) - timedelta(days=1)).isoformat())
        typical = typical_opening(con, rid)
        for kind in ("lifts", "runs"):
            log_items = {x["id"]: x for x in today[kind]}
            y_items = {x["id"]: x for x in yesterday[kind]} if yesterday else {}
            for x in snap[kind]:
                t, y = log_items.get(x["id"], {}), y_items.get(x["id"])
                x["first_open_today"] = t.get("first_open")
                x["typical_first_open"] = typical.get((kind, x["id"]))
                x["open_yesterday"] = None if not yesterday else bool(y and y["first_open"])
                if kind == "runs":
                    x["groomed_today"] = t.get("groomed_today")
                    x["groomed_yesterday"] = None if not yesterday else bool(y and y["groomed_today"])
        snap["today"] = {"slots": today["slots"], "changes": today["changes"]}
        write_json(OUT / f"latest_{rid}.json", snap)
        latest[rid] = snap
    generated = utc_iso(datetime.now(timezone.utc))
    write_json(OUT / "latest.json", {"format": FORMAT, "generated_at": generated, "resorts": latest})
    write_json(OUT / "days" / "index.json", {"format": FORMAT, "generated_at": generated, "dates": index})


# --- commands ---

def collect(scheduled=False):
    config = load_config()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    local = banff(now, config)
    if scheduled and not in_window(local, config):
        log(f"skip {utc_iso(now)}: {local:%Y-%m-%d %H:%M} Banff is outside the window")
        return 0
    fetched_at = utc_iso(now)
    if not DB_PATH.exists() and MANIFEST.exists():
        rebuild()  # a fresh checkout has the raw archive but not the database
    con = connect()
    problems = []
    for resort in config["resorts"]:
        urls = {f"{resort['id']}-{k.replace('_pdf', '-pdf').replace('_', '-')}": u
                for k, u in resort["sources"].items()}
        urls["open-meteo"] = open_meteo_url(resort["zones"])
        bodies, sources = {}, []
        for source, url in urls.items():
            ext = "pdf" if source.endswith("-pdf") else "json" if source == "open-meteo" else \
                "xml" if source == "louise-feed" else "html"
            status, body, error = fetch(url)
            if body is not None and status != 200:
                error = error or f"HTTP {status}"
            row = archive(con, fetched_at, local.date().isoformat(), resort["id"], source, url, status, body, error,
                          ext, keep_body=ext != "pdf" or config.get("archive_pdf_bodies", False))
            sources.append({k: row[k] for k in ("source", "url", "http_status", "bytes", "sha256", "error")})
            if not error:
                bodies[source] = body
        parsed, model, parse_errors = parse_sources(resort, fetched_at, bodies, config)
        for s in sources:
            s["error"] = s["error"] or parse_errors.get(s["source"])
        snap = build_snapshot(resort, fetched_at, parsed, model, sources, config)
        save_snapshot(con, snap)
        con.commit()
        c = snap["counts"]
        problems += [f"{resort['id']} {e}" for e in snap["errors"]]
        log(f"collect {fetched_at} ({snap['date']} {snap['slot']} Banff) {resort['id']}: "
            f"lifts {c.get('lifts_open')}/{c.get('lifts_total')}, runs {c.get('runs_open')}/{c.get('runs_total')}, "
            f"groomed {c.get('runs_groomed')}, " +
            ", ".join(f"{z['zone']} {(z['quality'] or {}).get('code')}" for z in snap["zones"]) +
            "".join(f"\n  ERROR {e}" for e in snap["errors"]))
    write_outputs(con, config, days=[local.date().isoformat()])
    return 1 if problems else 0


def rebuild():
    config = load_config()
    rows = [json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = connect()
    runs = {}
    for row in rows:
        save_fetch(con, row)
        runs.setdefault((row["fetched_at"], row["resort"]), []).append(row)
    resorts = {r["id"]: r for r in config["resorts"]}
    for (fetched_at, rid), fetched in sorted(runs.items()):
        bodies = {r["source"]: read_raw(r) for r in fetched if not r["error"]}
        parsed, model, parse_errors = parse_sources(resorts[rid], fetched_at, bodies, config)
        sources = [{k: r[k] for k in ("source", "url", "http_status", "bytes", "sha256", "error")} for r in fetched]
        for s in sources:
            s["error"] = s["error"] or parse_errors.get(s["source"])
        save_snapshot(con, build_snapshot(resorts[rid], fetched_at, parsed, model, sources, config))
    con.commit()
    write_outputs(con, config)
    log(f"rebuild: {len(runs)} snapshots from {len(rows)} saved responses")
    return 0


def digest(day=None):
    config = load_config()
    con = connect()
    day = day or banff(datetime.now(timezone.utc), config).date().isoformat()
    lines = [f"# Banff conditions, {day}", ""]
    for resort in config["resorts"]:
        log_ = day_log(con, resort["id"], day)
        lines.append(f"## {resort['name']}")
        if not log_:
            lines += ["No snapshots.", ""]
            continue
        last = log_["hourly"][-1]
        n = len(log_["slots"])
        lines.append(f"{n} check{'s' * (n != 1)}, {log_['slots'][0]} to {log_['slots'][-1]} Banff time. "
                     f"Last: lifts {last['counts'].get('lifts_open')}/{last['counts'].get('lifts_total')}, "
                     f"runs {last['counts'].get('runs_open')}/{last['counts'].get('runs_total')}.")
        open_lifts = [f"{x['name']} ({x['first_open']})" for x in log_["lifts"] if x["first_open"]]
        lines.append("- Lifts open (first seen open): " + (", ".join(open_lifts) or "none"))
        standby = [x["name"] for x in log_["lifts"] if x["standby_slots"]]
        if standby:
            lines.append("- Lifts on standby at some point: " + ", ".join(standby))
        groomed = [x["name"] for x in log_["runs"] if x["groomed_today"]]
        lines.append(f"- Groomed ({len(groomed)}): " + (", ".join(groomed) or "none"))
        later = [f"{c['name']} {c['to']} at {c['slot']}" for c in log_["changes"] if c["field"] == "status"]
        if later:
            lines.append("- During the day: " + "; ".join(later))
        for z in last["zones"]:
            snow = ", ".join(f"{k.replace('_cm', '').replace('_', ' ')} {v} cm"
                             for k, v in z["snow"].items() if v is not None)
            lines.append(f"- {z['zone']} ({z['elev_m']} m): {z['temp_c']} °C, {z['quality_label']}"
                         + (f"; reported {snow}" if snow else ""))
        times = {}
        for note in log_["notes"]:
            times.setdefault(note["text"], []).append(note["time"])
        for text, ts in times.items():
            lines.append(f"- Note ({', '.join(ts)}): {text}")
        lines.append("")
    text = "\n".join(lines)
    print(text)
    path = OUT / "digest" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return 0


def check():
    """Validate every field of out/latest.json. Prints problems; exit 1 if there are any."""
    data = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
    problems, statuses = [], {"open", "closed", "standby", "expected", "other"}
    for rid, s in data["resorts"].items():
        def bad(msg):
            problems.append(f"{rid}: {msg}")
        if not s["ok"]:
            bad(f"not ok: {s['errors']}")
        for kind in ("lifts", "runs"):
            if not s[kind]:
                bad(f"no {kind}")
            for x in s[kind]:
                if not x["id"] or not x["name"] or x["status"] not in statuses:
                    bad(f"{kind[:-1]} {x}")
                if kind == "runs" and (not isinstance(x["groomed"], bool) or x["level"] not in LEVELS.values()
                                       or not x["area"]):
                    bad(f"run {x['name']}: groomed {x['groomed']!r}, level {x['level']!r}, area {x['area']!r}")
        if [z["zone"] for z in s["zones"]] != ["base", "mid", "summit"]:
            bad("zones are not base, mid, summit")
        for z in s["zones"]:
            if not z["model"] or z["model"]["temp_c"] is None or not (z["quality"] or {}).get("code"):
                bad(f"zone {z['zone']}: model or quality missing")
            # Louise's mid-mountain station reports temperature and wind but no snow numbers
            if z["report_section"] and all(v is None for v in z["snow"].values()) and \
                    (z["station"] or {}).get("temp_c") is None:
                bad(f"zone {z['zone']}: nothing from '{z['report_section']}'")
        if s["season"]["base_cm"] is None:
            bad("no base depth for the season")
        if not s["report_updated"] or not any(s["report_updated"].values()):
            bad("no resort update time")
        c = s["counts"]
        print(f"{rid} {s['local_time']}: {c['lifts_open']}/{c['lifts_total']} lifts, {c['runs_open']}/{c['runs_total']} "
              f"runs, {c['runs_groomed']} groomed, reported {c.get('reported')}, season {s['season']}, "
              f"quality {[z['quality']['code'] for z in s['zones'] if z['quality']]}")
    print("\n".join(problems) or "all fields parsed")
    return 1 if problems else 0


def export(folder):
    folder.mkdir(parents=True, exist_ok=True)
    con = connect()
    for table in ("fetches", "lift_status", "run_status", "zone_conditions"):
        cursor = con.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3")
        with open(folder / f"{table}.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([c[0] for c in cursor.description])
            writer.writerows(cursor)
    log(f"export to {folder}")
    return 0


def main(argv):
    if sys.stdout is not None:  # pythonw has no console; a piped Windows console may not know every character
        sys.stdout.reconfigure(errors="replace")
    command = argv[1] if len(argv) > 1 else "collect"
    if command == "collect":
        return collect(scheduled="--scheduled" in argv)
    if command == "rebuild":
        return rebuild()
    if command == "summary":
        return digest(argv[2] if len(argv) > 2 else None)
    if command == "check":
        return check()
    if command == "export":
        return export(Path(argv[2]) if len(argv) > 2 else Path.home() / "Downloads" / "banfftrack")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
