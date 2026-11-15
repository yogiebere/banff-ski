# banfftrack data format (`banfftrack/1`)

What an app reads. Everything is UTF-8 JSON under `out/`. Times are UTC with a `Z`, or
Banff local time with `-06:00` (Alberta has been on UTC-6 all year since 2026). A "slot" is the scheduled Banff time a
check stands for: `"05:30"`, `"06:00"`, `"06:30"`, then `"07:00"` to `"17:00"` hourly (the list is in
`resorts.json`). A check counts for the latest slot at or before it, if that slot is under an hour earlier; checks at
other times fall to the half hour. Resort ids are `sunshine` and `louise`. Missing values are `null`, never left
out. Fields may be added later; none will be renamed or removed without changing `format`.

## Files
| file | what | updated |
|---|---|---|
| `out/latest.json` | `{format, generated_at, resorts: {sunshine: Snapshot, louise: Snapshot}}` | every check |
| `out/latest_sunshine.json`, `out/latest_louise.json` | one Snapshot each (same content as in `latest.json`) | every check |
| `out/days/index.json` | `{format, generated_at, dates: {sunshine: ["2026-12-24", ...], louise: [...]}}` | every check |
| `out/days/<resort>/<YYYY-MM-DD>.json` | DayLog: that day hour by hour | every check that day |
| `out/digest/<YYYY-MM-DD>.md` | plain-text digest for the morning summary | when `summary` runs |

The full history is in `data/raw/` (see README); `rebuild` turns it into `data/banfftrack.db` (SQLite).

## Snapshot (one resort, one check)
| field | type | notes |
|---|---|---|
| `format` | `"banfftrack/1"` | |
| `resort`, `name` | string | `"sunshine"`, `"Banff Sunshine Village"` |
| `fetched_at` | UTC | when the check ran |
| `local_time`, `date`, `slot` | Banff time | `"2026-12-28T07:09:04-06:00"`, `"2026-12-28"`, `"07:00"` |
| `ok` | bool | false if any source failed; the rest of the snapshot is still filled where possible |
| `errors` | [string] | e.g. `"louise-feed: HTTP 502"` |
| `report_updated` | object | the resort's own timestamp: Sunshine `conditions` (snow report), Louise `feed` |
| `operations` | object or null | Louise only: `resortStatus`, `openTime`, `closeTime`, `lastSnowfallDate`, ... (raw strings) |
| `counts` | object | `lifts_open`, `lifts_total`, `lifts_standby`, `runs_open`, `runs_total`, `runs_groomed`, and `reported`: the resort's own headline counts |
| `lifts` | [Lift] | every lift the resort lists |
| `runs` | [Run] | every run the resort lists |
| `parks` | [Park] | Louise only |
| `zones` | [Zone] | `base`, `mid`, `summit`, in that order |
| `season` | object | `{base_cm, from, season}`: the mid-mountain base depth and where it came from. `season` is `early` (<100 cm), `mid` (100-150) or `late` (150+). Sunshine uses its mid-mountain settled base; Louise has no mid base, so it's the average of its base and summit reports |
| `forecast` | object | the resort's forecast: `{updated, updated_local, summary, periods: [{period, high_c, low_c, summary, freezing_level_m, ridge_wind_dir, ridge_wind_kmh, precip}]}`. Louise's has only `period`, `high_c`, `low_c`, `summary` |
| `text` | object | `summary` (the resort's own conditions text), `lifts_info` (Louise), and `notes`: every sentence that gives a time, `[{time: "10:30", text: "...Mad Trappers, open from 10:30 am."}]` |
| `today` | object | `{slots, changes}` so far today (see DayLog) |
| `sources` | [object] | per source: `source`, `url`, `http_status`, `bytes`, `sha256`, `error` |

### Lift
`id` (the resort's id, stable), `name`, `key` (lower-case name for matching map names: `"goats eye express quad"`),
`area` (Louise's area; null at Sunshine), `status`, `status_raw`, `hours` (Sunshine's posted hours, e.g.
`"9:00am-4:00pm"`; null at Louise), `first_open_today` (first slot seen open today), `typical_first_open` (median
first-open slot over all logged days), `open_yesterday`. Louise also has `realtime` (raw).

`status` is one of `open`, `closed`, `standby` (Sunshine's "Standby", or a hold or delay: expected to open),
`expected`, `other`, or null. `status_raw` is the resort's word.

### Run
`id`, `name`, `key`, `area` (Sunshine: `Goat's Eye Mountain`, `Lookout Mountain`, `Mount Standish`; Louise: `Front
Side`, `Back Bowls`, `Larch`, `West Bowl`, `Richardson's Ridge`), `level` (`green`, `blue`, `black`, `double`),
`level_raw`, `status`, `status_raw`, `groomed` (bool, as the resort publishes it at that check),
`first_open_today`, `groomed_today` (groomed at any check today), `typical_first_open`, `open_yesterday` and
`groomed_yesterday` (at any check yesterday; null when yesterday wasn't logged). Sunshine also has `conditions` (its
condition icons, e.g. `["Groomed"]`). `groomed` is whatever the resort shows at that check, so early in the morning
it can still be the previous day's until the resort updates.

Run and lift names are the resort's, not OpenSkiMap's: match on `key`, and keep a small alias list for the
mismatches (Sunshine calls its lifts "... Express Quad").

### Zone
| field | notes |
|---|---|
| `zone`, `label`, `elev_m` | Sunshine: base = gondola base 1660 m (where the Banff Ave ski-out ends), mid = Village 2160 m, summit = Lookout 2730 m. Louise: base 1645 m, mid = Whitehorn Lodge 2042 m, summit 2637 m |
| `report_section` | the resort's snow-report section for this zone, or null |
| `snow` | the resort's numbers: `base_cm`, `new_overnight_cm`, `new_24h_cm`, `new_48h_cm`, `new_7d_cm`, `season_cm` (null where it doesn't report them; Sunshine only shows 24 h figures after a snowfall) |
| `snow_other` | any other snow-report card, by its label |
| `station` | the resort's weather station (Louise only): `{station, temp_c, wind_kmh, wind_dir, observed_at}` |
| `model` | Open-Meteo at this elevation: `temp_c`, `tmin_24h_c`, `tmax_24h_c`, `hours_above_0_24h`, `hours_above_0_72h`, `hours_since_thaw`, `snow_since_thaw_cm`, `hours_since_snow`, `snow_1h_cm`, `snow_12h_cm`, `snow_24h_cm`, `snow_48h_cm`, `snow_72h_cm`, `snow_7d_cm`, `snow_temp_c` (temperature while the last 24 h of snow fell), `rain_24h_mm`, `rain_48h_mm`, `precip_24h_mm`, `wind_kmh`, `gust_kmh`, `wind_dir_deg`, `wind_max_24h_kmh`, `gust_max_24h_kmh`, `freezing_level_m`, `cloud_pct`, `weather_code`, `humidity_pct`, `next_24h: {snow_cm, rain_mm, tmin_c, tmax_c, wind_max_kmh}`, `next_72h_snow_cm`, and `lift_hours` and `lift_hours_tomorrow`: the forecast for that Banff day's lift hours, `{date, snow_cm, rain_mm, temp_min_c, temp_max_c, wind_max_kmh, gust_max_kmh, cloud_mean_pct, vis_min_m, hours: [{time: "09:00".."16:00", temp_c, snow_cm, rain_mm, cloud_pct, wind_kmh, gust_kmh, vis_m, weather_code}]}`. Use these rather than calling Open-Meteo from Claude's cloud, which gets HTTP 429 there |
| `quality` | `{rule, code, label, flags, why, inputs}`: see below |

### Snow quality (rule `v1`)
`code` and `label`, first match wins:

| code | label | when |
|---|---|---|
| `wet_rain` | Wet, rain-soaked snow | ≥1 mm rain in 48 h and above 0 °C now |
| `wet` | Warm, wet or soft snow | ≥1 °C now, or above -1 °C after 2+ hours above 0 °C today |
| `cold_dry_powder` | Cold, dry powder | ≥5 cm in 24 h that fell at -10 °C or colder |
| `dry_powder` | Dry powder | ≥5 cm, fell at -10 to -4 °C |
| `dense_powder` | Denser powder | ≥5 cm, fell at -4 to -1 °C |
| `heavy_new_snow` | Heavy, wet new snow | ≥5 cm, fell above -1 °C |
| `rain_crust` | Rain crust, refrozen | rain in 48 h, now below 0 °C |
| `refrozen` | Refrozen after a thaw: firm or crusty until it softens | 2+ hours above 0 °C in 72 h, now -1 °C or colder, under 5 cm since |
| `wind_affected` | Wind-affected: packed, scoured or drifted | wind ≥30 km/h or gusts ≥55 in 24 h, with ≥5 cm in 72 h |
| `dusting` | A dusting over packed snow | 1-5 cm in 24 h |
| `packed_firm` | Packed and firm | no snow for 72 h |
| `packed_powder` | Packed powder | otherwise |

New-snow labels get ", wind-affected" when it was windy. `flags` can hold `new_snow`, `freeze_thaw`, `rain`, `wind`,
`very_cold` (-20 °C or colder). `why` is a short sentence of the numbers used. `inputs` holds every number the rule
read: `new_24h_cm` and `new_24h_source` (`resort` when the resort reports 24 h snow for that zone, else `model`),
`temp_now_c` and `temp_now_source` (the Louise station where there is one, else the model), and the model fields above.
The rule can change; the version is in `rule`, and the whole log is re-scored by `rebuild`.

### Park (Louise)
`id`, `name`, `status`, `status_raw`, `groomed`.

## DayLog (`out/days/<resort>/<date>.json`)
| field | notes |
|---|---|
| `format`, `resort`, `name`, `date` | |
| `slots` | `["05:30", "06:00", "06:30", "07:00", ...]`: the checks that day. If two checks fall in one slot, the later one counts |
| `first_check`, `last_check` | UTC |
| `lifts` | `[{id, name, key, area, hours, status: [per slot], first_open, standby_slots}]` |
| `runs` | `[{id, name, key, area, level, status: [per slot], groomed: [per slot], groomed_today, first_open, standby_slots}]` |
| `changes` | `[{slot, kind: "lift"/"run", id, name, field: "status"/"groomed", from, to}]`: what changed between checks, in order |
| `hourly` | per slot: `{slot, fetched_at, ok, counts, report_updated, zones: [{zone, elev_m, temp_c, station_temp_c, model_temp_c, wind_kmh, station_wind_kmh, model_wind_kmh, model_gust_kmh, new_24h_cm, snow, quality, quality_label}]}`. `temp_c` and `wind_kmh` are the station's where there is one, else the model's |
| `notes` | every timed sentence seen that day, `[{time, text}]` |
| `summary_text` | the resort's text at the last check |

`status` and `groomed` arrays line up with `slots`; a lift missing from one check is `null` there. `first_open` equal
to the first slot means it was already listed open at the first check of the day (resorts may list the day's planned
lifts as open before they start turning).
