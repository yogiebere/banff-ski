"""Morning prep for the Run Finder app: writes dist\\day\\<resort>.json, the report each page pre-fills Today from.

Usage (Python 3.13, standard library only):
    python morning.py sunshine|louise|crystal|all [--date YYYY-MM-DD] [--sample] [--offline]
                      [--site DIR] [--banfftrack DIR|URL] [--conditions DIR]

In the published repository it runs as app/prep/morning.py and finds the site (app/) and the tracker's out/ by
itself: python3 app/prep/morning.py sunshine

What goes in (formats: DAYFILE.md):
- Sunshine and Lake Louise: the Banff conditions tracker's output, banfftrack\\out (format banfftrack/1, see
  banfftrack\\DATA_FORMAT.md). latest_<resort>.json is today's report if it's dated today; days\\<resort>\\<yesterday>.json
  says what was open yesterday. Its mid-mountain Open-Meteo hours and snow-quality call are the measured side.
- Crystal has no tracker, so this script is its tracker: it reads Crystal's own mountain-report feed (the one its
  web page's widget loads), adds the Morse Lake SNOTEL station, and saves the result as a snapshot in
  conditions\\crystal\\<date>\\<HHMM>.json, so tomorrow's run knows what was open today.
- Every resort: Open-Meteo's hourly model at mid-mountain for the last 7 days and today. It's the forecast for
  picking the condition, and Crystal's measured-snow fallback when SNOTEL doesn't answer (labelled as a model).

What comes out: runs open, expected and closed, and open yesterday; the groomed runs; the resort's snow claim, the
measured snow and a verdict; the condition button and the new-snow box; the mid-mountain base and its season. Run
names are mapped to the map's names; anything that doesn't map is listed under "unmatched" for you to add to
aliases\\<resort>.json and rerun. Then rerun build_app.py if the pages changed, and upload dist (README.md).

--sample writes made-up snapshots for Sunshine and Lake Louise under samples\\ and builds from those, to test the
page side without the tracker. Day files from samples say so on the page.
"""
import datetime as dt
import glob
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SKIING = os.path.dirname(HERE)
CONDITIONS = os.path.join(HERE, "conditions")          # Crystal's snapshots (Banff's are the tracker's)
# The built site: app\dist here, or the folder above when this runs as app/prep/morning.py inside the published
# repository (the routine's copy). --site overrides it.
SITE = os.path.dirname(HERE) if os.path.exists(os.path.join(os.path.dirname(HERE), "sunshine.html")) else os.path.join(HERE, "dist")
UA = "RunFinder-morning-prep/1.0 (personal, non-commercial)"

RESORTS = {
    # name, fixed UTC offset in winter, mid-mountain point for the model, page file
    "sunshine": {"name": "Sunshine Village", "offset": -6, "lat": 51.0785, "lon": -115.7731, "elev_m": 2160},
    "louise": {"name": "Lake Louise", "offset": -6, "lat": 51.4450, "lon": -116.1550, "elev_m": 2050},
    "crystal": {"name": "Crystal Mountain", "offset": -8, "lat": 46.9353, "lon": -121.4746, "elev_m": 1676,
                "snotel": "642:WA:SNTL", "snotel_name": "Morse Lake SNOTEL", "snotel_elev_m": 1646},
}
# Alberta stays on UTC-6 all year; Crystal is on Pacific time (UTC-8 in winter, -7 from the second Sunday in March).
POWDER_CM = 8          # the page's "3+ in (8 cm) of new snow" box
SNOW_DAY_CM = 5        # a day with this much counts as a snow day for "days since snow"
LIFT_HOURS = (9, 16)


# ---------- time ----------
def tz_for(resort, when=None):
    off = RESORTS[resort]["offset"]
    if resort == "crystal" and when is not None:
        y = when.year
        start = dt.datetime(y, 3, 8 + (6 - dt.date(y, 3, 8).weekday()) % 7, 2)     # second Sunday in March
        end = dt.datetime(y, 11, 1 + (6 - dt.date(y, 11, 1).weekday()) % 7, 2)     # first Sunday in November
        if start <= when.replace(tzinfo=None) < end:
            off = -7
    return dt.timezone(dt.timedelta(hours=off))


def now_local(resort):
    utc = dt.datetime.now(dt.timezone.utc)
    return utc.astimezone(tz_for(resort, utc.replace(tzinfo=None) + dt.timedelta(hours=RESORTS[resort]["offset"])))


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def local_iso(s, resort):
    """A feed's timestamp moved to the resort's own clock (Crystal's feed stamps some times in Mountain time)."""
    t = parse_time(s)
    return iso(t.astimezone(tz_for(resort, t.replace(tzinfo=None)))) if t else None


def parse_time(s):
    """ISO 8601 with an offset like -06:00 or -0600."""
    if not s:
        return None
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s.strip().replace("Z", "+00:00"))
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


# ---------- fetching ----------
def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def open_meteo(resort):
    """Hourly model at mid-mountain: the last 7 days and today, as station-style rows plus today's forecast rows."""
    r = RESORTS[resort]
    data = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": r["lat"], "longitude": r["lon"], "elevation": r["elev_m"], "timezone": "GMT",
        "hourly": "temperature_2m,snowfall,snow_depth,precipitation,cloud_cover,wind_speed_10m,wind_gusts_10m,visibility",
        "past_days": 7, "forecast_days": 2})
    h, units = data["hourly"], data["hourly_units"]
    assert units["snowfall"] == "cm" and units["temperature_2m"] == "°C" and units["snow_depth"] == "m", units
    rows = []
    for i, t in enumerate(h["time"]):
        utc = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
        loc = utc.astimezone(tz_for(resort, utc.replace(tzinfo=None)))
        depth = h["snow_depth"][i]
        rows.append({"t": iso(loc), "temp_c": h["temperature_2m"][i], "snowfall_cm": h["snowfall"][i],
                     "snow_depth_cm": None if depth is None else round(depth * 100, 1), "precip_mm": h["precipitation"][i],
                     "cloud_pct": h["cloud_cover"][i], "wind_kmh": h["wind_speed_10m"][i], "gust_kmh": h["wind_gusts_10m"][i],
                     "visibility_m": h["visibility"][i]})
    url = "https://open-meteo.com/en/docs#latitude=%s&longitude=%s" % (r["lat"], r["lon"])
    return {"source": "Open-Meteo model", "url": url, "elev_m": r["elev_m"], "rows": rows}


def snotel_hourly(resort, start, end):
    r = RESORTS[resort]
    data = get_json("https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data", {
        "stationTriplets": r["snotel"], "elements": "SNWD,WTEQ,TOBS", "duration": "HOURLY",
        "beginDate": start.strftime("%Y-%m-%d %H:%M"), "endDate": end.strftime("%Y-%m-%d %H:%M")})
    by_t = {}
    for series in data[0]["data"]:
        el = series["stationElement"]
        code, unit = el["elementCode"], el["storedUnitCode"]
        for v in series.get("values", []):
            if v.get("value") is None:
                continue
            # SNOTEL hours are local standard time with no offset in the string.
            t = dt.datetime.fromisoformat(v["date"]).replace(tzinfo=dt.timezone(dt.timedelta(hours=RESORTS[resort]["offset"])))
            row = by_t.setdefault(iso(t), {"t": iso(t)})
            if code == "SNWD":
                row["snow_depth_cm"] = round(v["value"] * 2.54, 1) if unit == "in" else v["value"]
            elif code == "WTEQ":
                row["swe_mm"] = round(v["value"] * 25.4, 1) if unit == "in" else v["value"]
            elif code == "TOBS":
                row["temp_c"] = round((v["value"] - 32) / 1.8, 1) if unit == "degF" else v["value"]
    return [by_t[k] for k in sorted(by_t)]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def strip_html(s):
    s = re.sub(r"<br\s*/?>|</div>|</p>", " ", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\s+", " ", s).strip()


def crystal_snapshot():
    """Crystal's lifts, trails and snow report from its mountain-report feed, in the tracker's snapshot format."""
    cfg = get_json("https://v4.mtnfeed.com/resorts/crystal-mountain.json")        # the widget's own settings
    feed = get_json("https://mtnpowder.com/feed/v3.json", {"bearer_token": cfg["bearerToken"], "resortId[]": cfg["resortIds"]})
    r = feed["Resorts"][0]
    page = "https://www.crystalmountainresort.com/the-mountain/mountain-report-and-webcams"
    fetched = now_local("crystal")

    def status(s):
        s = (s or "").lower()
        if "open" in s:
            return "open"
        if "expect" in s or "anticipat" in s or "delay" in s:
            return "expected"
        if "hold" in s:
            return "hold"
        if "schedul" in s:
            return "scheduled"
        return "closed"

    lifts, trails = [], []
    for area in r.get("MountainAreas", []):
        for l in area.get("Lifts") or []:
            lifts.append({"name": l["Name"], "status": status(l.get("StatusEnglish") or l.get("Status")), "time": None,
                          "note": None if l.get("WaitTimeString") in (None, "--") else l["WaitTimeString"], "source": "lifts_trails"})
        for t in area.get("Trails") or []:
            g = (t.get("Grooming") or "").strip()
            trails.append({"name": t["Name"], "status": status(t.get("StatusEnglish") or t.get("Status")), "time": None,
                           "groomed": None if g in ("", "--") else g.lower() not in ("no", "false"),
                           "level": t.get("Difficulty"), "area": area.get("Name"), "source": "lifts_trails"})
    sr = r.get("SnowReport", {})
    mid, top, base = sr.get("MidMountainArea", {}), sr.get("SummitArea", {}), sr.get("BaseArea", {})
    allm = sr.get("AllMountain", {})
    snap = {
        "schema": 1, "resort": "crystal", "fetched_at": iso(fetched),
        "sources": [{"id": "lifts_trails", "name": "Crystal mountain report", "url": page,
                     "as_of": local_iso(feed.get("LastUpdate"), "crystal"), "ok": True, "error": None},
                    {"id": "snow_report", "name": "Crystal snow report", "url": page,
                     "as_of": local_iso(sr.get("LastUpdate"), "crystal"), "ok": True, "error": None}],
        "lifts": lifts, "trails": trails,
        "report": {"source": "snow_report", "as_of": local_iso(sr.get("LastUpdate"), "crystal"),
                   "base_cm": {"lower": num(base.get("BaseCm")), "mid": num(mid.get("BaseCm")), "upper": num(top.get("BaseCm"))},
                   "new_cm": {"overnight": num(allm.get("SinceLiftsClosedCm")), "24h": num(allm.get("Last24HoursCm")),
                              "48h": num(allm.get("Last48HoursCm")), "7d": num(allm.get("Last7DaysCm"))},
                   "season_cm": num(sr.get("SeasonTotalCm")), "surface": sr.get("BaseConditions"),
                   "text": strip_html(sr.get("Alert") or sr.get("Report"))[:300],
                   "operating": r.get("OperatingStatus")},
        "stations": [],
    }
    try:
        rows = snotel_hourly("crystal", fetched.replace(tzinfo=None) - dt.timedelta(days=7), fetched.replace(tzinfo=None))
        snap["stations"].append({"id": "morse_lake", "name": RESORTS["crystal"]["snotel_name"], "elev_m": RESORTS["crystal"]["snotel_elev_m"],
                                 "url": "https://wcc.sc.egov.usda.gov/nwcc/site?sitenum=642", "hours": rows})
    except Exception as e:                       # the report still works without the station
        snap["sources"].append({"id": "snotel", "name": "Morse Lake SNOTEL", "url": None, "as_of": None, "ok": False, "error": str(e)[:200]})
    return snap


def save_snapshot(root, snap):
    t = parse_time(snap["fetched_at"])
    folder = os.path.join(root, snap["resort"], t.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    for path in (os.path.join(folder, t.strftime("%H%M") + ".json"), os.path.join(root, snap["resort"], "latest.json")):
        with open(path, "w", encoding="utf8") as fh:
            json.dump(snap, fh, indent=1, ensure_ascii=False)


def snapshots(root, resort, day):
    """All snapshots for a resort's local date, oldest first."""
    out = []
    for p in sorted(glob.glob(os.path.join(root, resort, day.isoformat(), "*.json"))):
        try:
            s = json.load(open(p, encoding="utf8"))
            s["_t"] = parse_time(s["fetched_at"])
            out.append(s)
        except (ValueError, KeyError, OSError):
            print("  skipped unreadable snapshot", p)
    return sorted(out, key=lambda s: s["_t"])


# ---------- the Banff conditions tracker (banfftrack/1, see banfftrack\DATA_FORMAT.md) ----------
# The tracker's out folder: next to the site in the repository, else this PC's banfftrack\out.
BANFFTRACK = next((d for d in (os.path.join(os.path.dirname(SITE), "out"), os.path.join(SKIING, "banfftrack", "out"))
                   if os.path.exists(os.path.join(d, "latest_sunshine.json"))), os.path.join(SKIING, "banfftrack", "out"))
TRACK_STATUS = {"open": "open", "closed": "closed", "standby": "expected", "expected": "expected"}
# The tracker's snow-quality codes (rule v1) to the page's condition buttons. Warm, wet snow has no button; packed
# powder is the neutral setting.
QUALITY_COND = {"cold_dry_powder": "bluebird_powder", "dry_powder": "bluebird_powder", "dense_powder": "bluebird_powder",
                "heavy_new_snow": "bluebird_powder", "refrozen": "bluebird_firm", "rain_crust": "bluebird_firm",
                "packed_firm": "bluebird_firm", "wet": "packed_powder", "wet_rain": "packed_powder",
                "wind_affected": "packed_powder", "dusting": "packed_powder", "packed_powder": "packed_powder"}


def zone(snap, name):
    return next((z for z in snap.get("zones", []) if z.get("zone") == name), None) or {}


def first(*vals):
    return next((v for v in vals if v is not None), None)


def read_tracker(root, rel):
    """A tracker file from its out folder or its published URL; None if it isn't there."""
    if root.startswith(("http://", "https://")):
        try:
            return get_json(root.rstrip("/") + "/" + rel)
        except Exception as e:
            print("  couldn't fetch", rel, "-", e)
            return None
    path = os.path.join(root, *rel.split("/"))
    return json.load(open(path, encoding="utf8")) if os.path.exists(path) else None


def tracker_snapshot(resort, day, root):
    """The tracker's latest check for this date, in this script's snapshot shape, plus yesterday's checks."""
    t = read_tracker(root, "latest_%s.json" % resort)
    if t is None:
        print("  no tracker file latest_%s.json in %s" % (resort, root))
        return None, []
    if t.get("format") != "banfftrack/1":
        sys.exit("latest_%s.json: format %r, expected banfftrack/1" % (resort, t.get("format")))
    ysnaps = []
    has_y = any(r.get("open_yesterday") is not None for r in t.get("runs", []))
    y = None if has_y else read_tracker(root, "days/%s/%s.json" % (resort, (day - dt.timedelta(days=1)).isoformat()))
    if y:
        tz = tz_for(resort)
        for i, slot in enumerate(y.get("slots", [])):
            h, mi = (int(x) for x in slot.split(":"))
            ysnaps.append({"_t": dt.datetime.combine(day - dt.timedelta(days=1), dt.time(h, mi), tz),
                           "trails": [{"name": r["name"], "status": TRACK_STATUS.get(r["status"][i])}
                                      for r in y.get("runs", []) if i < len(r.get("status") or [])]})
    if t.get("date") != day.isoformat():
        print("  tracker's latest check is for %s, not %s" % (t.get("date"), day))
        return None, ysnaps

    by_src = {x["source"]: x for x in t.get("sources", [])}
    upd = t.get("report_updated") or {}
    if resort == "sunshine":
        runs_src = {"id": "lifts_trails", "name": "Sunshine lifts and trails report", "url": (by_src.get("sunshine-lifts") or {}).get("url"),
                    "as_of": t["local_time"]}
        snow_src = {"id": "snow_report", "name": "Sunshine snow report", "url": (by_src.get("sunshine-conditions") or {}).get("url"),
                    "as_of": upd.get("conditions")}
        groom_src = dict(runs_src, id="grooming", name="Sunshine lifts and trails report (groomed icons)")
    else:
        runs_src = {"id": "lifts_trails", "name": "Lake Louise lift and run status", "url": "https://ski-louise-status-page.vercel.app/status",
                    "as_of": upd.get("feed") or t["local_time"]}
        snow_src = dict(runs_src, id="snow_report", name="Lake Louise snow report")
        groom_src = {"id": "grooming", "name": "Lake Louise grooming report", "url": (by_src.get("louise-grooming-pdf") or {}).get("url"),
                     "as_of": upd.get("feed") or t["local_time"]}
    # Expected later: on standby, or closed now but the tracker has seen it open by a usual time, or a timed resort
    # note names it ("Mad Trappers, open from 10:30 am").
    notes = (t.get("text") or {}).get("notes") or []
    trails = []
    for r in t.get("runs", []):
        st = TRACK_STATUS.get(r.get("status"), "closed")
        time_, note = None, None
        if st != "open":
            said = next((n for n in notes if r.get("key") and (" " + r["key"] + " ") in (" " + norm(n.get("text")) + " ")), None)
            if said:
                st, time_, note = "expected", said["time"], "the resort says"
            elif st == "expected" or r.get("typical_first_open"):
                time_ = r.get("typical_first_open")
                note = "standby" if st == "expected" else "usually opens by then"
                st = "expected"
        trails.append({"name": r["name"], "status": st, "time": time_, "note": note, "groomed": r.get("groomed"),
                       "open_yesterday": r.get("open_yesterday"), "first_open": r.get("first_open_today"), "source": "lifts_trails"})
    lifts = [{"name": l["name"], "status": TRACK_STATUS.get(l.get("status"), "closed"),
              "time": l.get("typical_first_open") if l.get("status") != "open" else None, "note": l.get("hours")} for l in t.get("lifts", [])]

    mid, top, low = zone(t, "mid"), zone(t, "summit"), zone(t, "base")
    snow = [z.get("snow") or {} for z in (mid, top, low)]
    report = {"source": "snow_report", "as_of": snow_src["as_of"],
              "base_cm": {"lower": snow[2].get("base_cm"), "mid": snow[0].get("base_cm"), "upper": snow[1].get("base_cm")},
              "new_cm": {k: first(*(z.get(f) for z in snow)) for k, f in (("overnight", "new_overnight_cm"), ("24h", "new_24h_cm"),
                                                                           ("48h", "new_48h_cm"), ("7d", "new_7d_cm"))},
              "season_cm": first(*(z.get("season_cm") for z in snow)), "surface": None, "text": None}
    # Measured: the tracker's Open-Meteo hours at mid-mountain, and its snow-quality call.
    mz = mid if mid.get("model") else top
    m = mz.get("model") or {}
    q = mz.get("quality") or {}
    measured_ = None
    if m:
        measured_ = {"new_cm": {"24h": m.get("snow_24h_cm"), "72h": m.get("snow_72h_cm"), "7d": m.get("snow_7d_cm")},
                     "temp_c": {"min": m["tmin_24h_c"], "max": m["tmax_24h_c"]} if m.get("tmin_24h_c") is not None else None,
                     "thaw_since_snow": (m.get("hours_above_0_72h") or 0) >= 2 and (m.get("snow_since_thaw_cm") or 0) < SNOW_DAY_CM,
                     "days_since_snow": None if m.get("hours_since_snow") is None else int(m["hours_since_snow"] // 24),
                     "how": "model snowfall", "quality": {k: q.get(k) for k in ("code", "label", "why")} if q else None,
                     "source": {"name": "Open-Meteo model at %s m, via the conditions tracker (an estimate, not a measurement)" % mz.get("elev_m"),
                                "url": (by_src.get("open-meteo") or {}).get("url"), "as_of": t["local_time"]}}
    season = t.get("season") or {}
    base = None
    if season.get("base_cm") is not None:
        base = {"cm": season["base_cm"], "upper_cm": snow[1].get("base_cm"), "how": season.get("from")}
    text, seen = [], set()
    for n in notes:
        if n["text"] not in seen:
            seen.add(n["text"])
            text.append(n["text"])
    snap = {"schema": 1, "resort": resort, "fetched_at": t["local_time"], "_t": parse_time(t["local_time"]),
            "sources": [runs_src, snow_src, groom_src], "lifts": lifts, "trails": trails, "report": report,
            "stations": [], "measured": measured_, "base": base, "notes_text": text,
            "errors": t.get("errors") or []}
    return snap, ysnaps


# ---------- names ----------
def norm(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[’'`\.]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^the ", "", s)
    return s.replace(" avenue", " ave").replace("saint ", "st ")


def page_data(resort):
    """The page's own data: run names, and the season cut-offs."""
    for p in (os.path.join(SITE, resort + ".html"),):
        if os.path.exists(p):
            s = open(p, encoding="utf8").read()
            return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', s, re.S).group(1))
    sys.exit("No page for %s in dist: run build_app.py first" % resort)


class Names:
    """Maps the resort's run names to the map's. Exact (normalized) first, then aliases\\<resort>.json, then Crystal's
    resort_ratings.json (the map session's list of Crystal's names for each map run), then Upper/Lower variants."""

    def __init__(self, resort, data):
        self.map = sorted(set(data["ratings"]) | {p["name"] for p in data["map"]["pieces"] if p.get("name")})
        self.by_norm = {}
        for n in self.map:
            self.by_norm.setdefault(norm(n), []).append(n)
        self.alias = {}
        # This folder's list where there is one (app\aliases on this PC, the source build_app.py copies), else the
        # published one next to the site (app/aliases in the repository, which the phones read too).
        path = next((x for x in (os.path.join(HERE, "aliases", resort + ".json"), os.path.join(SITE, "aliases", resort + ".json"))
                     if os.path.exists(x)), "")
        if os.path.exists(path):
            for k, v in json.load(open(path, encoding="utf8")).items():
                if not k.startswith("_"):
                    self.alias[norm(k)] = [] if v is None else [v] if isinstance(v, str) else v
        if resort == "crystal":
            rr = os.path.join(SKIING, "trailmap", "sources", "crystal", "resort_ratings.json")
            if os.path.exists(rr):
                for osm, v in json.load(open(rr, encoding="utf8")).items():
                    if not osm.startswith("_") and isinstance(v, dict) and v.get("as") and v.get("from") in ("report", "zone", "trail map"):
                        self.alias.setdefault(norm(v["as"]), []).append(osm)

    def match(self, name):
        """The map names for a report name: [] when the alias file says it has no line on the map, None when unknown."""
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
        return squeezed or None             # "Fire Weed" is "Fireweed", "Supermodel" is "Super Model"


# ---------- measured snow ----------
def sum_window(rows, key, end, hours):
    start = end - dt.timedelta(hours=hours)
    vals = [r[key] for r in rows if r.get(key) is not None and start < parse_time(r["t"]) <= end]
    return round(sum(vals), 1) if vals else None


def depth_change(rows, key, end, hours):
    pts = [(parse_time(r["t"]), r[key]) for r in rows if r.get(key) is not None and parse_time(r["t"]) <= end]
    if len(pts) < 2:
        return None
    now_t, now_v = pts[-1]
    before = [v for t, v in pts if t <= now_t - dt.timedelta(hours=hours)]
    if not before:
        return None
    return round(now_v - before[-1], 1)


def ratio(temp_c):
    """Snow-to-water ratio from temperature: about 10:1 near freezing, 15:1 at -8, 20:1 below -14."""
    if temp_c is None:
        return 12
    return max(8, min(20, 10 - (temp_c + 1) * 0.7))


def measured(station, model, end):
    """New snow, temperatures, thaw and days since snow, from a station if there is one, else the model."""
    out, how = {}, None
    rows = station["hours"] if station else []
    if rows and any(r.get("new_snow_cm") is not None for r in rows):
        how = "new-snow board"
        out["24h"], out["72h"], out["7d"] = (sum_window(rows, "new_snow_cm", end, h) for h in (24, 72, 168))
    elif rows and any(r.get("swe_mm") is not None for r in rows):
        how = "snow pillow water gain, at the temperature's snow-to-water ratio"
        temps = [r.get("temp_c") for r in rows[-24:] if r.get("temp_c") is not None]
        rat = ratio(sum(temps) / len(temps) if temps else None)
        for h, k in ((24, "24h"), (72, "72h"), (168, "7d")):
            d = depth_change(rows, "swe_mm", end, h)
            out[k] = None if d is None else round(max(0.0, d) * rat / 10, 1)
    elif rows and any(r.get("snow_depth_cm") is not None for r in rows):
        how = "rise in snow depth (settling makes this read low)"
        for h, k in ((24, "24h"), (72, "72h"), (168, "7d")):
            d = depth_change(rows, "snow_depth_cm", end, h)
            out[k] = None if d is None else max(0.0, d)
    src_rows = rows
    if how is None and model:
        how, src_rows = "model snowfall", model["rows"]
        out["24h"], out["72h"], out["7d"] = (sum_window(src_rows, "snowfall_cm", end, h) for h in (24, 72, 168))
    if how is None:
        return None
    temp_rows = (rows if rows and any(r.get("temp_c") is not None for r in rows) else (model or {}).get("rows", []))
    last24 = [r["temp_c"] for r in temp_rows if r.get("temp_c") is not None and end - dt.timedelta(hours=24) < parse_time(r["t"]) <= end]
    # Thaw and days since snow come from the model's hourly snowfall when the station has no hourly new snow.
    snow_rows = src_rows if how in ("new-snow board", "model snowfall") else (model or {}).get("rows", [])
    skey = "new_snow_cm" if how == "new-snow board" else "snowfall_cm"
    last_snow = max((parse_time(r["t"]) for r in snow_rows if (r.get(skey) or 0) >= 0.3 and parse_time(r["t"]) <= end), default=None)
    since = [r["temp_c"] for r in temp_rows if r.get("temp_c") is not None and parse_time(r["t"]) <= end and
             (last_snow is None or parse_time(r["t"]) > last_snow) and parse_time(r["t"]) > end - dt.timedelta(days=7)]
    days = None
    for back in range(0, 8):
        day_end = end - dt.timedelta(hours=24 * back)
        tot = sum_window(snow_rows, skey, day_end, 24)
        if tot is not None and tot >= SNOW_DAY_CM:
            days = back
            break
    return {"new_cm": {k: out.get(k) for k in ("24h", "72h", "7d")}, "how": how,
            "temp_c": {"min": min(last24), "max": max(last24)} if last24 else None,
            "thaw_since_snow": bool(since) and max(since) > 0.5, "days_since_snow": days}


def forecast_today(model, day, tz):
    hrs = [r for r in (model or {}).get("rows", []) if parse_time(r["t"]).date() == day and LIFT_HOURS[0] <= parse_time(r["t"]).hour <= LIFT_HOURS[1]]
    if not hrs:
        return None

    def vals(k):
        return [r[k] for r in hrs if r.get(k) is not None]
    return {"snow_cm": round(sum(vals("snowfall_cm")), 1), "gust_max": max(vals("gust_kmh"), default=None),
            "cloud_mean": sum(vals("cloud_pct")) / len(vals("cloud_pct")) if vals("cloud_pct") else None,
            "vis_min": min(vals("visibility_m"), default=None), "temp_max": max(vals("temp_c"), default=None)}


def snow_section(resort, snap, model, day, end):
    rep = (snap or {}).get("report") or {}
    claim_new = rep.get("new_cm") or {}
    src = {s.get("id"): s for s in (snap or {}).get("sources", [])}
    station = next((s for s in (snap or {}).get("stations", []) if s.get("hours")), None)
    m = (snap or {}).get("measured") or measured(station, model, end)
    if (snap or {}).get("measured"):
        msrc = m["source"]
    elif station and m and m["how"] != "model snowfall":
        msrc = {"name": "%s, %s m" % (station["name"], station.get("elev_m", "?")), "url": station.get("url"),
                "as_of": station["hours"][-1]["t"]}
    elif m:
        msrc = {"name": "Open-Meteo model at %d m (an estimate, not a measurement)" % RESORTS[resort]["elev_m"],
                "url": model["url"], "as_of": iso(end.replace(minute=0, second=0, microsecond=0))}
    else:
        msrc = None
    c24 = claim_new.get("24h") if claim_new.get("24h") is not None else claim_new.get("overnight")
    m24 = m["new_cm"]["24h"] if m else None
    surface = rep.get("surface") or ""
    bits = []
    c7, m7 = claim_new.get("7d"), (m["new_cm"]["7d"] if m else None)
    if (c24 is None or m24 is None) and c7 is not None and m7 is not None:
        # Sunshine posts 24-hour figures only after a snowfall, so check its 7-day total instead.
        tol = max(5.0, 0.35 * max(c7, m7))
        verdict = "agrees" if abs(c7 - m7) <= tol else "overstated" if c7 > m7 else "understated"
        bits.append("No 24-hour figure posted, so this checks the 7-day total: resort %g cm, data %g cm." % (c7, m7))
    elif c24 is None or m24 is None:
        verdict = "unverified"
        if c24 is None and m24 is not None:
            bits.append("The resort hasn't posted a new-snow figure.")
    else:
        tol = max(3.0, 0.4 * max(c24, m24))
        verdict = "agrees" if abs(c24 - m24) <= tol else "overstated" if c24 > m24 else "understated"
    if c24 is not None and m24 is not None and c7 is not None and m7 is not None:
        if abs(c7 - m7) > max(5.0, 0.35 * max(c7, m7)):
            bits.append("Over 7 days the resort says %g cm, the data %g cm." % (c7, m7))
    trusted24 = m24 if (verdict == "overstated" or c24 is None) else c24
    trusted72 = m["new_cm"]["72h"] if m else None
    s = surface.lower()
    if "powder" in s and "packed" not in s and trusted72 is not None and trusted72 < SNOW_DAY_CM:
        bits.append("It calls the surface powder, but under %d cm fell in 3 days." % SNOW_DAY_CM)
        if verdict == "agrees":
            verdict = "overstated"
    if m and m["thaw_since_snow"]:
        bits.append("It went above freezing since the last snow, so expect crust off the groomers.")

    fc = forecast_today(model, day, None)
    if fc and fc["snow_cm"] >= 2:
        cond, why = "storm", "%g cm forecast during lift hours." % fc["snow_cm"]
    elif fc and fc["gust_max"] is not None and fc["gust_max"] >= 60:
        cond, why = "wind_lowvis", "gusts to %d km/h forecast at mid-mountain." % fc["gust_max"]
    elif fc and fc["cloud_mean"] is not None and fc["cloud_mean"] >= 90 and fc["vis_min"] is not None and fc["vis_min"] < 1000:
        cond, why = "wind_lowvis", "cloud all day and visibility under 1 km forecast."
    elif trusted24 is not None and trusted24 >= POWDER_CM:
        cond, why = "bluebird_powder", "%g cm of new snow in 24 h." % trusted24
    elif m and (m.get("quality") or {}).get("code") in QUALITY_COND:
        q = m["quality"]
        cond = QUALITY_COND[q["code"]]
        if cond == "bluebird_powder":           # under 8 cm: the powder button would also drop the grooming adder
            cond = "packed_powder"
        why = "the tracker reads it as %s%s." % (q["label"].lower(), ": " + q["why"] if q.get("why") else "")
        if q["code"] in ("wet", "wet_rain"):
            why += " No button fits warm snow, so packed powder, the neutral setting."
    elif m and m["thaw_since_snow"]:
        cond, why = "bluebird_firm", "a thaw since the last snow."
    elif m and (m["days_since_snow"] is None or m["days_since_snow"] >= 4):
        cond, why = "bluebird_firm", ("no %d cm day in the last week." % SNOW_DAY_CM if m["days_since_snow"] is None
                                      else "%d days since %d cm fell." % (m["days_since_snow"], SNOW_DAY_CM))
    else:
        cond, why = "packed_powder", ("recent snow, cold since." if m else "no snow data, so the default.")
    if fc and cond not in ("storm", "wind_lowvis"):
        why += " Forecast for lift hours: %s, gusts to %d km/h." % (
            "clear" if (fc["cloud_mean"] or 0) < 30 else "partly cloudy" if (fc["cloud_mean"] or 0) < 70 else "cloudy", fc["gust_max"] or 0)
    csrc = src.get(rep.get("source")) or src.get("snow_report")
    return {
        "claim": {"source": {"name": csrc["name"], "url": csrc.get("url"), "as_of": rep.get("as_of") or csrc.get("as_of")} if csrc else None,
                  "surface": rep.get("surface"), "new_cm": {k: claim_new.get(k) for k in ("overnight", "24h", "48h", "7d")}} if rep else None,
        "measured": dict({"source": msrc}, **{k: m.get(k) for k in ("new_cm", "temp_c", "thaw_since_snow", "days_since_snow", "how", "quality")}) if m else None,
        "verdict": verdict, "verdict_text": " ".join(bits) or None,
        "condition": cond, "condition_why": why[0].upper() + why[1:],
        "new_snow": trusted24 is not None and trusted24 >= POWDER_CM,
        "forecast": fc,
    }


def base_section(snap, data):
    rep = (snap or {}).get("report") or {}
    b = rep.get("base_cm") or {}
    lo, hi = data["cover_base"]
    cm, how = b.get("mid"), "mid-mountain"
    if (snap or {}).get("base"):            # the tracker already picked it (Louise: average of base and summit)
        cm, how = snap["base"]["cm"], snap["base"].get("how") or how
        how = "mid-mountain" if how == "mid-mountain report" else how
    if cm is None and b.get("upper") is not None and b.get("lower") is not None:
        cm, how = round((b["upper"] + b["lower"]) / 2), "average of upper and lower (no mid-mountain figure)"
    elif cm is None and (b.get("upper") is not None or b.get("lower") is not None):
        cm, how = (b.get("upper"), "upper mountain (no mid-mountain figure)") if b.get("upper") is not None else (b.get("lower"), "lower mountain (no mid-mountain figure)")
    if cm is None:
        return None
    src = {s.get("id"): s for s in snap.get("sources", [])}.get(rep.get("source")) or {}
    check = None if how == "mid-mountain" else "Base is the " + how + "."
    station = next((s for s in snap.get("stations", []) if s.get("hours") and any(h.get("snow_depth_cm") is not None for h in s["hours"])), None)
    if station:
        d = [h["snow_depth_cm"] for h in station["hours"] if h.get("snow_depth_cm") is not None][-1]
        line = "%s (%s m) measures %g cm on the ground." % (station["name"], station.get("elev_m", "?"), d)
        check = (check + " " if check else "") + line
    return {"source": {"name": src.get("name", "Snow report"), "url": src.get("url"), "as_of": rep.get("as_of") or src.get("as_of")},
            "cm": round(cm), "upper_cm": None if b.get("upper") is None else round(b["upper"]), "season": "early" if cm < lo else "mid" if cm < hi else "late", "check": check}


def runs_section(snap, ysnaps, names):
    if not snap or not snap.get("trails"):
        return None, None, []
    rank = {"open": 3, "expected": 2, "closed": 1, "hold": 1, "scheduled": 1}
    best, groomed, unmatched, report_groomed, first_open = {}, set(), [], False, {}
    for t in snap["trails"]:
        hits = names.match(t["name"])
        if hits is None:
            unmatched.append(t["name"])
            continue
        st = t.get("status") or "closed"
        for n in hits:
            if n not in best or rank.get(st, 0) > rank.get(best[n][0], 0):
                best[n] = (st, t.get("time"), t.get("note"))
            if t.get("first_open") and (n not in first_open or t["first_open"] < first_open[n]):
                first_open[n] = t["first_open"]
            if t.get("groomed") is True:
                groomed.add(n)
        # A report with no run marked groomed is no grooming report: otherwise every run would count as ungroomed.
        if t.get("groomed") is True:
            report_groomed = True
    src = {s.get("id"): s for s in snap.get("sources", [])}
    tsrc = src.get(snap["trails"][0].get("source")) or src.get("lifts_trails") or {}
    yopen, yas = None, None
    lift_snaps = [y for y in ysnaps if LIFT_HOURS[0] <= y["_t"].hour <= LIFT_HOURS[1] + 1 and y.get("trails")]
    if any(t.get("open_yesterday") is not None for t in snap["trails"]):       # the tracker says it per run
        yopen = set()
        for t in snap["trails"]:
            if t.get("open_yesterday") is True:
                yopen.update(names.match(t["name"]) or [])
    elif lift_snaps:
        yopen = set()
        for y in lift_snaps:
            for t in y["trails"]:
                if t.get("status") == "open":
                    yopen.update(names.match(t["name"]) or [])
        yas = iso(lift_snaps[-1]["_t"])
    runs = {
        "source": {"name": tsrc.get("name", "Lift and trail report"), "url": tsrc.get("url"), "as_of": tsrc.get("as_of") or snap["fetched_at"]},
        "open": sorted(n for n, v in best.items() if v[0] == "open"),
        "expected": [{"name": n, "time": v[1], "note": v[2]} for n, v in sorted(best.items()) if v[0] == "expected"],
        "closed": sorted(n for n, v in best.items() if v[0] not in ("open", "expected")),
        "open_yesterday": sorted(yopen) if yopen is not None else None,
        "yesterday_as_of": yas,
        "first_open": first_open,
        "unmatched": sorted(set(unmatched)),
    }
    gsrc = src.get("grooming") or tsrc
    groom = {"source": {"name": gsrc.get("name", "Grooming report"), "url": gsrc.get("url"), "as_of": gsrc.get("as_of") or snap["fetched_at"]},
             "names": sorted(groomed), "unmatched": []} if report_groomed else None
    return runs, groom, unmatched


def notes_for(snap):
    notes = []
    for l in (snap or {}).get("lifts", []):
        if l.get("status") == "expected":
            notes.append("%s expected%s%s." % (l["name"], " at " + l["time"] if l.get("time") else "", " (" + l["note"] + ")" if l.get("note") else ""))
        elif l.get("status") == "hold":
            notes.append("%s on hold%s." % (l["name"], " (" + l["note"] + ")" if l.get("note") else ""))
    notes += (snap or {}).get("notes_text") or []
    if (snap or {}).get("errors"):
        notes.append("Tracker errors at this check: " + "; ".join(snap["errors"]))
    rep = (snap or {}).get("report") or {}
    if rep.get("operating") and rep["operating"].lower() != "open":
        notes.append("Resort status: %s." % rep["operating"])
    if rep.get("text"):
        notes.append(rep["text"])
    return notes


# ---------- sample data (until the tracker has some) ----------
def write_samples(root, resort, day, data):
    """Made-up snapshots for yesterday afternoon and this morning, shaped like the tracker's."""
    import random
    rnd = random.Random(resort + day.isoformat())
    tz = tz_for(resort)
    names = sorted(data["ratings"])
    R = data["ratings"]
    plan = {"sunshine": dict(surface="Packed Powder", c24=6, c7=31, m_new=5, mid=142, upper=165, t=(-16, -7)),
            "louise": dict(surface="Powder", c24=12, c7=20, m_new=3, mid=96, upper=120, t=(-9, -2))}[resort]
    closed = set(rnd.sample(names, len(names) // 6))
    expected = set(rnd.sample(sorted(set(names) - closed), 3))
    groomed = {n for n in names if R[n].get("grooming") == "always"} | set(rnd.sample(names, 4))
    yclosed = set(rnd.sample(sorted(closed), len(closed) // 2)) | set(rnd.sample(sorted(set(names) - closed), 4))
    rename = {names[0]: names[0].upper() + ".", names[5]: names[5].replace(" ", "  ")}       # spelling the matcher should absorb

    def trails(cl, ex, gr):
        out = [{"name": rename.get(n, n), "status": "closed" if n in cl else "expected" if n in ex else "open",
                "time": "11:00" if n in ex else None, "groomed": n in gr if gr is not None else None, "source": "lifts_trails"} for n in names]
        out.append({"name": "Sample Run Not On The Map", "status": "open", "groomed": False, "source": "lifts_trails"})
        return out

    def hours(end, new_cm):
        rows, depth, swe = [], plan["mid"] - 10, 380.0
        for i in range(72, -1, -1):
            t = end - dt.timedelta(hours=i)
            snowing = 6 <= i <= 14
            if snowing:
                depth += new_cm / 9
                swe += new_cm / 9 / 12 * 10
            frac = (t.hour - 4) % 24 / 24
            temp = plan["t"][0] + (plan["t"][1] - plan["t"][0]) * (1 - abs(frac - 0.5) * 2)
            rows.append({"t": iso(t), "temp_c": round(temp, 1), "snow_depth_cm": round(depth, 1), "swe_mm": round(swe, 1),
                         "precip_mm": None, "wind_kmh": 12, "gust_kmh": 30})
        return rows

    morning = dt.datetime.combine(day, dt.time(6, 5), tz)
    yday = morning - dt.timedelta(days=1)
    src = [{"id": "lifts_trails", "name": "%s lifts and trails report (sample)" % RESORTS[resort]["name"], "url": None,
            "as_of": iso(morning - dt.timedelta(minutes=20)), "ok": True, "error": None},
           {"id": "snow_report", "name": "%s snow report (sample)" % RESORTS[resort]["name"], "url": None,
            "as_of": iso(morning - dt.timedelta(minutes=35)), "ok": True, "error": None}]
    for when, cl, ex, gr in ((yday.replace(hour=11), yclosed, set(), None), (yday.replace(hour=15), yclosed, set(), None),
                             (morning, closed, expected, groomed)):
        snap = {"schema": 1, "resort": resort, "fetched_at": iso(when), "sample": True, "sources": src,
                "lifts": [{"name": "Sample Express", "status": "expected", "time": "10:30", "note": "after avalanche control"}],
                "trails": trails(cl, ex, gr),
                "report": {"source": "snow_report", "as_of": iso(when - dt.timedelta(minutes=35)),
                           "base_cm": {"lower": None, "mid": plan["mid"], "upper": plan["upper"]},
                           "new_cm": {"overnight": plan["c24"] - 2, "24h": plan["c24"], "48h": plan["c24"] + 3, "7d": plan["c7"]},
                           "season_cm": 240, "surface": plan["surface"], "text": None},
                "stations": [{"id": "sample", "name": "Sample snow pillow", "elev_m": 2200, "url": None, "hours": hours(when, plan["m_new"])}]}
        save_snapshot(root, snap)


# ---------- the day file ----------
def prepare(resort, day, root, offline=False, sample=False, tracker=None):
    data = page_data(resort)
    names = Names(resort, data)
    tz = tz_for(resort)
    if sample:
        write_samples(root, resort, day, data)
    if resort == "crystal" and not offline and not sample:
        try:
            save_snapshot(root, crystal_snapshot())
        except Exception as e:
            print("  Crystal feed failed:", e)
    now = now_local(resort)
    upto = now if day == now.date() else dt.datetime.combine(day, dt.time(23, 59), tz)
    if resort in ("sunshine", "louise") and not sample:
        snap, ysnaps = tracker_snapshot(resort, day, tracker or BANFFTRACK)
    else:
        snaps_now = [s for s in snapshots(root, resort, day) if s["_t"] <= upto]
        snap = snaps_now[-1] if snaps_now else None
        ysnaps = snapshots(root, resort, day - dt.timedelta(days=1))
    model = None
    if not offline:
        try:
            model = open_meteo(resort)
        except Exception as e:
            print("  Open-Meteo failed:", e)
    end = snap["_t"] if snap else upto
    runs, groomed, unmatched = runs_section(snap, ysnaps, names)
    out = {
        "schema": 1, "resort": resort, "date": day.isoformat(), "prepared_at": iso(now.replace(microsecond=0)),
        "prepared_by": "Claude, morning prep", "sample": bool(sample or (snap or {}).get("sample")),
        "snapshot_at": iso(snap["_t"]) if snap else None,
        "runs": runs, "lifts": [{"name": l["name"], "status": l.get("status"), "time": l.get("time")} for l in (snap or {}).get("lifts", [])],
        "groomed": groomed, "snow": snow_section(resort, snap, model, day, end) if (snap or model) else None,
        "base": base_section(snap, data) if snap else None, "notes": notes_for(snap),
    }
    day_dir = os.path.join(SITE, "day")
    os.makedirs(day_dir, exist_ok=True)
    # What the reviewer wrote by hand survives a rerun the same day (the 6:55 pass): "take", a sentence or two for the
    # family, and "override", a condition button they chose over the rule's ({"condition", "condition_why"}).
    out["take"], out["override"] = None, None
    old_path = os.path.join(day_dir, resort + ".json")
    if os.path.exists(old_path):
        try:
            old = json.load(open(old_path, encoding="utf8"))
            if old.get("date") == out["date"]:
                out["take"], out["override"] = old.get("take"), old.get("override")
        except ValueError:
            pass
    ov = out["override"]
    if ov and out["snow"] and ov.get("condition"):
        out["snow"]["condition"] = ov["condition"]
        out["snow"]["condition_why"] = ov.get("condition_why") or "Set by hand at the morning check."
    body = json.dumps(out, indent=1, ensure_ascii=False)
    with open(os.path.join(day_dir, resort + ".json"), "w", encoding="utf8") as fh:
        fh.write(body)
    with open(os.path.join(day_dir, resort + ".js"), "w", encoding="utf8") as fh:      # for pages opened from disk
        fh.write("window.RUNFINDER_DAY = window.RUNFINDER_DAY || {};\nwindow.RUNFINDER_DAY[%s] = %s;\n" % (json.dumps(resort), body))
    summarize(out, snap)
    return out


def summarize(d, snap):
    print("%s %s%s" % (RESORTS[d["resort"]]["name"], d["date"], " (sample)" if d["sample"] else ""))
    print("  snapshot:", d["snapshot_at"] or "none for this date")
    r = d["runs"]
    if r:
        print("  runs: %d open, %d expected, %d closed; yesterday %s" % (len(r["open"]), len(r["expected"]), len(r["closed"]),
              "%d open" % len(r["open_yesterday"]) if r["open_yesterday"] is not None else "no snapshots"))
        if r["unmatched"]:
            print("  UNMATCHED report names (add to aliases\\%s.json):" % d["resort"], "; ".join(r["unmatched"]))
    print("  groomed:", "%d runs" % len(d["groomed"]["names"]) if d["groomed"] else "no grooming report")
    s = d["snow"]
    if s:
        print("  snow: %s, %s -> %s%s" % (s["verdict"], s["verdict_text"] or "", s["condition"], " + new snow" if s["new_snow"] else ""))
    b = d["base"]
    print("  base:", "%g cm, %s season" % (b["cm"], b["season"]) if b else "none")
    for n in d["notes"]:
        print("  note:", n[:160])


def main():
    args = sys.argv[1:]
    if not args or args[0] not in list(RESORTS) + ["all"]:
        sys.exit(__doc__)
    which = list(RESORTS) if args[0] == "all" else [args[0]]
    sample = "--sample" in args
    global SITE
    if "--site" in args:
        SITE = os.path.abspath(args[args.index("--site") + 1])
    root = CONDITIONS
    if "--conditions" in args:
        root = os.path.abspath(args[args.index("--conditions") + 1])
    elif sample:
        root = os.path.join(HERE, "samples", "conditions")
    for resort in which:
        day = now_local(resort).date()
        if "--date" in args:
            day = dt.date.fromisoformat(args[args.index("--date") + 1])
        tracker = os.path.abspath(args[args.index("--banfftrack") + 1]) if "--banfftrack" in args else BANFFTRACK
        prepare(resort, day, root, offline="--offline" in args, sample=sample and resort != "crystal", tracker=tracker)


if __name__ == "__main__":
    main()
