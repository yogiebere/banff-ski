"""Tests for banfftrack: the parsers on real responses saved 2026-11-15, the time window, the snow-quality rule and
the day log. Run: python -I test_banfftrack.py"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import banfftrack as b  # noqa: E402

FIX = HERE / "tests" / "fixtures"
FETCHED = "2026-11-15T19:49:35Z"  # when the open-meteo fixtures were fetched


def fixture(name):
    return (FIX / name).read_bytes()


def resort(rid):
    return next(r for r in b.load_config()["resorts"] if r["id"] == rid)


def snapshot(rid, fetched_at=FETCHED):
    config = b.load_config()
    bodies = {"sunshine": {"sunshine-lifts": fixture("sunshine_lifts.html"),
                           "sunshine-conditions": fixture("sunshine_conditions.html")},
              "louise": {"louise-feed": fixture("louise_feed.xml")}}[rid]
    bodies["open-meteo"] = fixture(f"open_meteo_{rid}.json")
    parsed, model, errors = b.parse_sources(resort(rid), FETCHED, bodies, config)
    sources = [{"source": s, "error": errors.get(s)} for s in bodies]
    return b.build_snapshot(resort(rid), fetched_at, parsed, model, sources, config)


class SunshineParsers(unittest.TestCase):
    def setUp(self):
        self.page = fixture("sunshine_lifts.html").decode("utf-8")

    def test_lifts_and_runs(self):
        d = b.parse_sunshine_lifts(self.page)
        self.assertEqual((len(d["lifts"]), len(d["runs"])), (12, 139))
        self.assertEqual(d["totals"], {"lifts_open": 3, "lifts_total": 12, "trails_open": 4, "trails_total": 139})
        self.assertEqual({x["name"] for x in d["lifts"] if x["status"] == "open"},
                         {"Gondola", "Mitey Mite Conveyor", "Strawberry Express Quad"})
        self.assertEqual({x["name"] for x in d["runs"] if x["status"] == "open"},
                         {"Mitey Mite Learning Area", "The Dell Valley", "Snowsnake", "Strawberry Terrain Park"})
        self.assertEqual({x["name"] for x in d["runs"] if x["groomed"]},
                         {"Mitey Mite Learning Area", "The Dell Valley", "Strawberry Terrain Park"})
        self.assertTrue(all(x["name"] and x["status"] in ("open", "closed") for x in d["lifts"] + d["runs"]))
        self.assertTrue(all(x["level"] in ("green", "blue", "black", "double") for x in d["runs"]))
        self.assertEqual({x["area"] for x in d["runs"]}, {"Goat's Eye Mountain", "Lookout Mountain", "Mount Standish"})
        gondola = next(x for x in d["lifts"] if x["name"] == "Gondola")
        self.assertEqual(gondola["hours"], "8:00am-5:30pm")

    def test_standby_status(self):
        # The legend lists Standby (icon-20); make Goat's Eye show it.
        i = self.page.index("Goat&#039;s Eye Express Quad")
        j = self.page.index("icon-status icon-19", i)
        page = self.page[:j] + self.page[j:].replace("Closed", "Standby", 1)
        lift = b.parse_sunshine_lifts(page)["lifts"][0]
        self.assertEqual((lift["name"], lift["status"], lift["status_raw"]), ("Goat's Eye Express Quad", "standby",
                                                                             "Standby"))

    def test_conditions(self):
        d = b.parse_sunshine_conditions(fixture("sunshine_conditions.html").decode("utf-8"))
        self.assertEqual(d["updated_local"], "2026-11-15T09:41:00-06:00")
        self.assertEqual(d["zones"]["Upper Mountain"]["values"], {"new_7d_cm": 7, "base_cm": 29, "season_cm": 29})
        self.assertEqual(d["zones"]["Mid Mountain"]["values"], {"new_7d_cm": 7, "base_cm": 20, "season_cm": 44})
        self.assertIn("Mad Trappers, open from 10:30 am", d["summary"])
        periods = d["forecast"]["periods"]
        self.assertEqual([p["period"] for p in periods], ["Today", "Tonight", "Monday", "Tuesday", "Wednesday"])
        self.assertEqual(periods[1], {"period": "Tonight", "high_c": None, "low_c": -6,
                                      "summary": "Cloudy and isolated flurries.", "freezing_level_m": 1300,
                                      "ridge_wind_dir": "SW", "ridge_wind_kmh": 15, "precip": "snow 1-3cm"})

    def test_trim(self):
        page = b"<html><main><div id=\"x\">" + fixture("sunshine_conditions.html") + b"</main><footer>menu</footer>"
        trimmed = b.trim("sunshine-conditions", page)
        self.assertTrue(trimmed.startswith(b"<div") and b"footer" not in trimmed)
        self.assertEqual(b.parse_sunshine_conditions(trimmed.decode()), b.parse_sunshine_conditions(page.decode()))
        body = b"<html>new layout</html>"
        self.assertIs(b.trim("sunshine-lifts", body), body)  # markers missing: keep the whole page
        self.assertIs(b.trim("louise-feed", body), body)

    def test_new_snow_labels(self):
        self.assertEqual([b.snow_field(x) for x in ("Overnight", "24 Hours", "48 Hrs", "7 Days", "Settled Base",
                                                    "Cumulative", "Snowmaking")],
                         ["new_overnight_cm", "new_24h_cm", "new_48h_cm", "new_7d_cm", "base_cm", "season_cm", None])


class LouiseParser(unittest.TestCase):
    def test_feed(self):
        d = b.parse_louise(fixture("louise_feed.xml"))
        self.assertEqual((len(d["lifts"]), len(d["runs"])), (13, 171))
        self.assertEqual({x["name"] for x in d["runs"] if x["status"] == "open"},
                         {"Juniper", "Bald Eagle", "Easy Street", "Sunny Side"})
        self.assertEqual({x["name"] for x in d["runs"] if x["groomed"]},
                         {"Juniper", "Bald Eagle", "Easy Street", "Sunny Side"})
        self.assertEqual({x["name"] for x in d["lifts"] if x["status"] == "open"},
                         {"Magic Carpet", "Glacier Express", "Juniper Express"})
        upper = d["zones"]["Upper Mountain"]
        self.assertEqual((upper["station"], upper["temp_c"], upper["wind_kmh"], upper["values"]["base_cm"]),
                         ("Paradise-Top", -3, 26, 26))
        self.assertIsNone(d["zones"]["Mid-Mountain"]["values"]["base_cm"])
        self.assertTrue(all(x["level"] for x in d["runs"]))


class Helpers(unittest.TestCase):
    def test_status_words(self):
        self.assertEqual([b.norm_status(s) for s in ("Open", "Closed", "Standby", "Wind Hold", "Expected",
                                                     "Not open", "", "Groomed")],
                         ["open", "closed", "standby", "standby", "expected", "other", None, "other"])

    def test_timed_notes(self):
        notes = b.timed_notes("Save some energy for Mad Trappers, open from 10:30 am. Goat's Eye opens at 9 AM!")
        self.assertEqual([n["time"] for n in notes], ["10:30", "09:00"])

    def test_window_and_slots(self):
        config = b.load_config()

        def check(utc):
            local = b.banff(datetime.strptime(utc, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc), config)
            return b.slot_of(local, config), b.in_window(local, config)
        self.assertEqual(len(config["window"]["slots"]), 14)
        # The workflow's start times (5 minutes after each slot) and late starts
        self.assertEqual(check("2026-12-24T11:35"), ("05:30", True))
        self.assertEqual(check("2026-12-24T11:58"), ("05:30", True))
        self.assertEqual(check("2026-12-24T12:05"), ("06:00", True))
        self.assertEqual(check("2026-12-24T12:35"), ("06:30", True))
        self.assertEqual(check("2026-12-24T12:59"), ("06:30", True))
        self.assertEqual(check("2026-12-24T13:05"), ("07:00", True))
        self.assertEqual(check("2026-12-24T13:50"), ("07:00", True))
        self.assertEqual(check("2026-12-24T23:05"), ("17:00", True))
        # Outside the slots or the dates
        self.assertEqual(check("2026-12-24T11:20"), ("05:00", False))
        self.assertEqual(check("2026-12-25T00:05"), ("18:00", False))
        self.assertEqual(check("2027-01-02T23:05"), ("17:00", True))
        self.assertEqual(check("2027-01-03T11:35"), ("05:30", False))
        self.assertEqual(check("2026-12-23T23:05"), ("17:00", False))

    def test_name_key(self):
        self.assertEqual(b.name_key("Goat's Eye Express Quad"), "goats eye express quad")
        self.assertEqual(b.name_key("Steep & Flat"), "steep and flat")
        self.assertEqual(b.name_key("Helen’s"), "helens")


def inputs(**kw):
    base = dict(new_24h_cm=0, new_24h_source="model", temp_now_c=-8, temp_now_source="model", snow_temp_c=None,
                tmax_24h_c=-5, hours_above_0_24h=0, hours_above_0_72h=0, snow_since_thaw_cm=None,
                hours_since_snow=30, snow_72h_cm=0, rain_48h_mm=0, wind_max_24h_kmh=10, gust_max_24h_kmh=20)
    base.update(kw)
    return base


class SnowQuality(unittest.TestCase):
    def code(self, **kw):
        return b.snow_quality(inputs(**kw))["code"]

    def test_new_snow_by_temperature(self):
        self.assertEqual(self.code(new_24h_cm=15, snow_temp_c=-14), "cold_dry_powder")
        self.assertEqual(self.code(new_24h_cm=15, snow_temp_c=-6), "dry_powder")
        self.assertEqual(self.code(new_24h_cm=15, snow_temp_c=-2), "dense_powder")
        self.assertEqual(self.code(new_24h_cm=15, snow_temp_c=-0.5, temp_now_c=-2), "heavy_new_snow")

    def test_wind_on_new_snow(self):
        q = b.snow_quality(inputs(new_24h_cm=12, snow_temp_c=-9, wind_max_24h_kmh=45))
        self.assertEqual(q["code"], "dry_powder")
        self.assertIn("wind-affected", q["label"])
        self.assertIn("wind", q["flags"])

    def test_warm_and_refrozen(self):
        self.assertEqual(self.code(temp_now_c=3, hours_above_0_24h=6, hours_above_0_72h=10), "wet")
        # Near 0 °C after a thaw today is soft, not refrozen (the Village on 2026-11-15).
        self.assertEqual(self.code(temp_now_c=0.5, hours_above_0_24h=2, hours_above_0_72h=19), "wet")
        self.assertEqual(self.code(temp_now_c=-6, hours_above_0_24h=0, hours_above_0_72h=8, snow_since_thaw_cm=0),
                         "refrozen")
        self.assertEqual(self.code(temp_now_c=-6, hours_above_0_72h=8, snow_since_thaw_cm=12, new_24h_cm=12,
                                   snow_temp_c=-7), "dry_powder")

    def test_rain(self):
        self.assertEqual(self.code(temp_now_c=2, rain_48h_mm=4, hours_above_0_24h=8), "wet_rain")
        self.assertEqual(self.code(temp_now_c=-5, rain_48h_mm=4), "rain_crust")

    def test_dry_spells(self):
        self.assertEqual(self.code(hours_since_snow=30), "packed_powder")
        self.assertEqual(self.code(hours_since_snow=100), "packed_firm")
        self.assertEqual(self.code(hours_since_snow=None), "packed_firm")
        self.assertEqual(self.code(new_24h_cm=2), "dusting")
        self.assertEqual(self.code(wind_max_24h_kmh=50, snow_72h_cm=20), "wind_affected")

    def test_density(self):
        self.assertEqual([b.density(t) for t in (-15, -5, 0, 3)], [68, 75, 119, 119])


class ModelWeather(unittest.TestCase):
    def test_zone_model(self):
        n = 24 * 10
        temps = [-5.0] * n
        snow = [0.0] * n
        times = [f"T{i}" for i in range(n)]
        i = 200
        for k in range(i - 5, i + 1):      # 6 h of snow at -12 °C ending now
            snow[k], temps[k] = 2.0, -12.0
        temps[i - 50] = 1.5                # one warm hour two days ago
        h = {"time": times, "temperature_2m": temps, "snowfall": snow, "relative_humidity_2m": [90] * n,
             "rain": [0.0] * n, "precipitation": [0.0] * n, "wind_speed_10m": [10.0] * n,
             "wind_gusts_10m": [20.0] * n, "wind_direction_10m": [180] * n, "freezing_level_height": [1200] * n,
             "cloud_cover": [100] * n, "weather_code": [73] * n}
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        h["time"][i] = now.strftime("%Y-%m-%dT%H:00")
        m = b.zone_model(h, now)
        self.assertEqual((m["snow_24h_cm"], m["snow_temp_c"], m["hours_since_snow"]), (12.0, -12.0, 0))
        self.assertEqual((m["hours_above_0_72h"], m["hours_since_thaw"], m["snow_since_thaw_cm"]), (1, 50, 12.0))
        self.assertEqual(m["tmax_24h_c"], -5.0)


class LiftHours(unittest.TestCase):
    def test_lift_hours_from_fixture(self):
        model = b.parse_open_meteo(fixture("open_meteo_louise.json"), resort("louise")["zones"],
                                   b.parse_utc(FETCHED), -6)
        today, tomorrow = model["mid"]["lift_hours"], model["mid"]["lift_hours_tomorrow"]
        self.assertEqual((today["date"], tomorrow["date"]), ("2026-11-15", "2026-11-16"))
        self.assertEqual([x["time"] for x in today["hours"]], [f"{h:02d}:00" for h in range(9, 17)])
        self.assertEqual(today["temp_max_c"], max(x["temp_c"] for x in today["hours"]))
        self.assertIsNone(today["vis_min_m"])  # the 2026-11-15 fixture predates asking for visibility


class Snapshots(unittest.TestCase):
    def test_sunshine_snapshot(self):
        s = snapshot("sunshine")
        self.assertTrue(s["ok"], s["errors"])
        self.assertEqual((s["date"], s["slot"]), ("2026-11-15", "13:00"))  # 13:49 Banff
        self.assertEqual(s["season"], {"base_cm": 20, "from": "mid-mountain report", "season": "early"})
        self.assertEqual([z["zone"] for z in s["zones"]], ["base", "mid", "summit"])
        self.assertTrue(all(z["quality"]["code"] for z in s["zones"]))
        self.assertEqual(s["zones"][2]["snow"]["base_cm"], 29)
        self.assertEqual(s["counts"]["runs_groomed"], 3)
        self.assertIn("10:30", [n["time"] for n in s["text"]["notes"]])
        json.dumps(s)

    def test_louise_snapshot(self):
        s = snapshot("louise")
        self.assertTrue(s["ok"], s["errors"])
        self.assertEqual(s["season"]["base_cm"], 24)  # no mid base: the average of 22 and 26
        summit = s["zones"][2]
        self.assertEqual(summit["station"]["station"], "Paradise-Top")
        self.assertEqual(summit["quality"]["inputs"]["temp_now_source"], "station Paradise-Top")
        self.assertEqual(summit["quality"]["inputs"]["new_24h_source"], "resort")

    def test_missing_source_is_not_ok(self):
        config = b.load_config()
        parsed, model, _ = b.parse_sources(resort("louise"), FETCHED, {"open-meteo": fixture("open_meteo_louise.json")},
                                           config)
        s = b.build_snapshot(resort("louise"), FETCHED, parsed, model,
                             [{"source": "louise-feed", "error": "HTTP 502"}], config)
        self.assertFalse(s["ok"])
        self.assertEqual(s["errors"], ["louise-feed: HTTP 502"])
        self.assertEqual(s["runs"], [])


class DayLog(unittest.TestCase):
    def test_changes_and_first_open(self):
        con = sqlite3.connect(":memory:")
        con.executescript(b.SCHEMA)
        first = snapshot("louise", "2026-12-28T13:00:00Z")
        second = snapshot("louise", "2026-12-28T15:00:00Z")
        grizzly = next(x for x in second["lifts"] if x["name"] == "Grizzly Gondola")
        grizzly["status"] = "open"
        helen = next(x for x in second["runs"] if x["name"] == "Helen's")
        helen["groomed"] = True
        for s in (first, second):
            b.save_snapshot(con, s)
        log = b.day_log(con, "louise", "2026-12-28")
        self.assertEqual(log["slots"], ["07:00", "09:00"])
        lift = next(x for x in log["lifts"] if x["name"] == "Grizzly Gondola")
        self.assertEqual((lift["status"], lift["first_open"]), (["closed", "open"], "09:00"))
        juniper = next(x for x in log["runs"] if x["name"] == "Juniper")
        self.assertEqual((juniper["first_open"], juniper["groomed_today"]), ("07:00", True))
        self.assertIn({"slot": "09:00", "kind": "lift", "id": grizzly["id"], "name": "Grizzly Gondola",
                       "field": "status", "from": "closed", "to": "open"}, log["changes"])
        self.assertTrue(any(c["name"] == "Helen's" and c["field"] == "groomed" for c in log["changes"]))
        self.assertEqual(b.typical_opening(con, "louise")[("lifts", grizzly["id"])], "09:00")

    def test_latest_has_yesterday(self):
        con = sqlite3.connect(":memory:")
        con.executescript(b.SCHEMA)
        yesterday = snapshot("louise", "2026-12-27T20:00:00Z")
        next(x for x in yesterday["runs"] if x["name"] == "Helen's").update(status="open", groomed=True)
        b.save_snapshot(con, yesterday)
        b.save_snapshot(con, snapshot("louise", "2026-12-28T13:00:00Z"))
        b.save_snapshot(con, snapshot("sunshine", "2026-12-28T13:00:00Z"))
        out = b.OUT
        with tempfile.TemporaryDirectory() as tmp:
            b.OUT = Path(tmp)
            try:
                b.write_outputs(con, b.load_config())
                latest = json.loads((Path(tmp) / "latest_louise.json").read_text(encoding="utf-8"))
                sunshine = json.loads((Path(tmp) / "latest_sunshine.json").read_text(encoding="utf-8"))
            finally:
                b.OUT = out
        runs = {x["name"]: x for x in latest["runs"]}
        self.assertEqual((runs["Helen's"]["open_yesterday"], runs["Helen's"]["groomed_yesterday"]), (True, True))
        self.assertEqual((runs["Juniper"]["open_yesterday"], runs["Mirkwood"]["open_yesterday"]), (True, False))
        self.assertIsNone(sunshine["runs"][0]["open_yesterday"])  # no log for yesterday
        self.assertEqual(latest["today"]["slots"], ["07:00"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
