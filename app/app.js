// Ski Run Finder app layer. build_app.py adds this after each run-finder page's own script, and the home page
// loads it too. It registers the service worker (offline use) and adds a home button. On a resort page it gets
// today's conditions, fills in the Today panel and shows where each value came from.
//
// Conditions come from two files (formats: DAYFILE.md):
// - the tracker's hourly file, <data>/latest_<resort>.json (banfftrack/1; Sunshine and Lake Louise), for what changes
//   through the day: runs open, grooming, base, the measured snow;
// - the morning check, day/<resort>.json from morning.py, for the judgment: the verdict on the resort's snow claim
//   and the condition button. It's the only file for Crystal, which has no tracker.
// They're fetched when the app opens, when it comes back to the front, every hour while it's open, and from the
// Update now button. With no signal the last saved copy shows, with its time. The page hook it prefers is described
// in PAGE_HOOK.md.
(function () {
  "use strict";
  function meta(name) { var m = document.querySelector('meta[name="' + name + '"]'); return m ? m.content : null; }
  var RESORT = meta("runfinder:resort");
  var DATA = meta("runfinder:data") || "";            // where the tracker's files are, e.g. "../out/"; empty for none
  var LIVE = ["sunshine", "louise"];                  // resorts the tracker covers
  var CONDS = {wind_lowvis: "Wind or low vis", bluebird_powder: "Bluebird powder", packed_powder: "Bluebird, packed powder",
               bluebird_firm: "Bluebird, firm", storm: "Storm day"};
  var VERDICT = {agrees: "backed by the data", overstated: "more than the data shows", understated: "less than the data shows",
                 unverified: "not checked"};
  var POWDER_CM = 8;                                   // the page's "3+ in (8 cm) of new snow" box
  // The tracker's snow-quality codes (rule v1) to the page's condition buttons, as in morning.py.
  var QUALITY_COND = {cold_dry_powder: "bluebird_powder", dry_powder: "bluebird_powder", dense_powder: "bluebird_powder",
                      heavy_new_snow: "bluebird_powder", refrozen: "bluebird_firm", rain_crust: "bluebird_firm", packed_firm: "bluebird_firm",
                      wet: "packed_powder", wet_rain: "packed_powder", wind_affected: "packed_powder", dusting: "packed_powder",
                      packed_powder: "packed_powder"};
  var RES_NAME = {sunshine: "Sunshine", louise: "Lake Louise", crystal: "Crystal"};

  // ---------- small helpers ----------
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function storeJSON(k, v) { if (v === undefined) { try { return JSON.parse(store(k) || "null"); } catch (e) { return null; } } store(k, JSON.stringify(v)); }
  // Times are shown as the resort's local clock, read straight from the ISO string, whatever the phone's zone.
  function clock(iso) {
    var m = /T(\d\d):(\d\d)/.exec(iso || ""); if (!m) return "";
    var h = +m[1]; return (h % 12 || 12) + ":" + m[2] + (h < 12 ? " AM" : " PM");
  }
  function hhmm(t) { var m = /^(\d\d?):(\d\d)/.exec(t || ""); if (!m) return t || ""; var h = +m[1]; return (h % 12 || 12) + ":" + m[2] + (h < 12 ? " AM" : " PM"); }
  // Elevations in feet, like the maps (the user, 2026-11-20: "ft for elevation"). Snow depths stay in cm, as the resorts
  // report them and the pages' base box takes them.
  function feet(m) { return m == null || isNaN(+m) ? "?" : Math.round(+m * 3.28084).toLocaleString("en-US") + " ft"; }
  function phoneClock(ms) { var d = new Date(ms), h = d.getHours(); return (h % 12 || 12) + ":" + ("0" + d.getMinutes()).slice(-2) + (h < 12 ? " AM" : " PM"); }
  var DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function dayName(ymd) {
    var p = (ymd || "").split("-").map(Number); if (p.length < 3) return ymd || "";
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2])); return DOW[d.getUTCDay()] + " " + MON[p[1] - 1] + " " + p[2];
  }
  function ymd(d) { return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function localToday() { return ymd(new Date()); }
  function dayBefore(s) { var p = s.split("-").map(Number); return ymd(new Date(p[0], p[1] - 1, p[2] - 1)); }
  // "Today", "Yesterday", or the date, from the phone's own date (Banff time on the trip).
  function relDay(d) { var t = localToday(); return d === t ? "Today" : d === dayBefore(t) ? "Yesterday" : dayName(d); }
  var curDate = "";
  function srcLine(s) {
    if (!s) return "";
    var name = s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.name) + "</a>" : esc(s.name);
    // The date shows only when it isn't the report's own day (yesterday's snow report, say).
    return name + (s.as_of ? ", " + (s.as_of.slice(0, 10) !== curDate ? dayName(s.as_of.slice(0, 10)) + " " : "") + clock(s.as_of) : "");
  }
  function list(names, max) {
    max = max || 12; var shown = names.slice(0, max).map(esc).join(", ");
    return names.length > max ? shown + " and " + (names.length - max) + " more" : shown;
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")); }
  function first() { for (var i = 0; i < arguments.length; i++) if (arguments[i] != null) return arguments[i]; return null; }

  // ---------- service worker and app updates ----------
  var swOK = "serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1");
  if (swOK) {
    var hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.register("sw.js").catch(function () {});
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (!hadController) { hadController = true; return; }        // first install, nothing to reload
      toast('App updated. <button type="button" id="rf-reload">Reload</button>');
      var b = document.getElementById("rf-reload"); if (b) b.addEventListener("click", function () { location.reload(); });
    });
  }

  var css = document.createElement("style");
  css.textContent = [
    ".rf-toast { position: fixed; left: 50%; bottom: calc(16px + env(safe-area-inset-bottom, 0px)); transform: translateX(-50%); z-index: 50;",
    "  background: #15212C; color: #fff; padding: 9px 14px; border-radius: 99px; font: 600 14px Barlow, 'Segoe UI', Arial, sans-serif; box-shadow: 0 4px 16px rgba(0,0,0,.3); }",
    ".rf-toast button { margin-left: 8px; border: 0; border-radius: 99px; padding: 4px 10px; font: inherit; background: #B3261E; color: #fff; cursor: pointer; }",
    ".rf-home { width: 40px; height: 40px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--ink);",
    "  box-sizing: border-box; box-shadow: 0 1px 3px rgba(0,0,0,.15); display: flex; align-items: center; justify-content: center; text-decoration: none; }",
    ".rf-home:focus-visible { outline: 2px solid var(--patrol); outline-offset: 2px; }",
    ".rf-back { display: inline-block; margin-bottom: 6px; font: 600 13px var(--body); color: var(--patrol); text-decoration: none; }",
    ".rf-day { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 7px; font-size: 13px; }",
    ".rf-day h3 { margin: 0; font: 700 16px var(--display); }",
    ".rf-fresh { display: flex; gap: 8px; align-items: center; justify-content: space-between; flex-wrap: wrap; font-size: 12.5px; color: var(--muted); }",
    ".rf-fresh b { color: var(--ink); font-weight: 600; }",
    ".rf-update { border: 1px solid var(--line); background: var(--snow); color: var(--ink); border-radius: 8px; padding: 5px 10px; font: 600 13px var(--body); cursor: pointer; }",
    ".rf-update:focus-visible { outline: 2px solid var(--patrol); outline-offset: 2px; }",
    ".rf-update[disabled] { opacity: .6; cursor: default; }",
    ".rf-day dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 10px; margin: 0; }",
    ".rf-day dt { font: 700 12px var(--display); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); padding-top: 1px; }",
    ".rf-day dd { margin: 0; }",
    ".rf-day .src { display: block; font-size: 11.5px; color: var(--muted); margin-top: 2px; }",
    ".rf-day .src a, .rf-day details a { color: inherit; }",
    ".rf-day details { font-size: 12.5px; color: var(--muted); } .rf-day details summary { font-weight: 600; color: var(--ink); }",
    ".rf-day details p { margin: 4px 0 0; }",
    ".rf-warn { background: var(--patrol-soft); color: var(--patrol); border-radius: 6px; padding: 5px 8px; font-weight: 600; }",
    ".rf-ok { color: #1E7A3C; font-weight: 600; } .rf-bad { color: var(--patrol); font-weight: 600; }",
    ".rf-take { border-left: 3px solid var(--gold); padding: 2px 0 2px 8px; font-size: 13.5px; }",
    "@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) .rf-ok { color: #6FD08C; } }"
  ].join("\n");
  document.head.appendChild(css);

  function toast(html) {
    var t = document.querySelector(".rf-toast") || document.body.appendChild(document.createElement("div"));
    t.className = "rf-toast"; t.setAttribute("role", "status"); t.innerHTML = html;
  }

  // ---------- run names ----------
  // The resort's spelling to the map's, as morning.py's Names does: exact (normalized), the alias list, Upper/Lower
  // variants, then the same letters without spaces. Returns [] for "no line on the map", null for unknown.
  function norm(s) {
    s = (s || "").toLowerCase().replace(/&/g, " and ").replace(/[’'`.]/g, "").replace(/[^a-z0-9]+/g, " ").trim().replace(/^the /, "");
    return s.replace(/ avenue/g, " ave").replace(/saint /g, "st ");
  }
  // Lift names: the resort's "Wawa Fixed Grip Quad" is the map's "Wawa Quad Chair", so compare them without the
  // words that only describe the chair; the alias list's "_lifts" covers the rest (Sunshine's "Gondola").
  var LIFT_WORDS = /\b(express|quad|chair|chairlift|lift|fixed|grip|luxury|six|pack|high|speed|detachable)\b/g;
  function liftCore(s) { return norm(s).replace(LIFT_WORDS, " ").replace(/\s+/g, ""); }
  function liftMatcher(mapLifts, aliases) {
    var byCore = {}, al = {};
    mapLifts.forEach(function (n) { if (n) (byCore[liftCore(n)] = byCore[liftCore(n)] || []).push(n); });
    var la = (aliases || {})._lifts || {};
    Object.keys(la).forEach(function (k) { if (k.charAt(0) !== "_") al[norm(k)] = la[k] === null ? [] : typeof la[k] === "string" ? [la[k]] : la[k]; });
    return function (name) { var k = norm(name); return al[k] || byCore[liftCore(name)] || null; };
  }
  function matcher(mapNames, aliases) {
    var byNorm = {}, alias = {}, known = {};
    mapNames.forEach(function (n) { known[n] = 1; (byNorm[norm(n)] = byNorm[norm(n)] || []).push(n); });
    Object.keys(aliases || {}).forEach(function (k) {
      if (k.charAt(0) === "_") return;
      var v = aliases[k]; alias[norm(k)] = v === null ? [] : typeof v === "string" ? [v] : v;
    });
    return function (name) {
      var k = norm(name);
      if (alias[k]) return alias[k].filter(function (n) { return known[n]; });
      if (byNorm[k]) return byNorm[k];
      var parts = [];
      ["upper ", "lower ", "middle ", "mid "].forEach(function (p) { parts = parts.concat(byNorm[p + k] || []); });
      if (parts.length) return parts;
      var bare = k.replace(/^(upper|lower|middle|mid) /, "");
      if (bare !== k && byNorm[bare]) return byNorm[bare];
      var flat = k.replace(/ /g, ""), squeezed = [];
      Object.keys(byNorm).forEach(function (kk) { if (kk.replace(/ /g, "") === flat) squeezed = squeezed.concat(byNorm[kk]); });
      return squeezed.length ? squeezed : null;
    };
  }

  // ---------- the tracker's hourly file (banfftrack/1) to a day report ----------
  var TRACK = {open: "open", closed: "closed", standby: "expected", expected: "expected"};
  var RANK = {open: 3, expected: 2, closed: 1};
  function zone(t, name) { return (t.zones || []).filter(function (z) { return z.zone === name; })[0] || {}; }
  function sources(t) {
    var by = {}; (t.sources || []).forEach(function (s) { by[s.source] = s; });
    var upd = t.report_updated || {};
    if (t.resort === "sunshine") {
      var runs = {name: "Sunshine lifts and trails report", url: (by["sunshine-lifts"] || {}).url, as_of: t.local_time};
      return {runs: runs, snow: {name: "Sunshine snow report", url: (by["sunshine-conditions"] || {}).url, as_of: upd.conditions || null},
              groom: {name: "Sunshine lifts and trails report (groomed icons)", url: runs.url, as_of: t.local_time}, model: by["open-meteo"]};
    }
    var feed = upd.feed || t.local_time;
    return {runs: {name: "Lake Louise lift and run status", url: "https://ski-louise-status-page.vercel.app/status", as_of: feed},
            snow: {name: "Lake Louise snow report", url: "https://ski-louise-status-page.vercel.app/status", as_of: feed},
            groom: {name: "Lake Louise grooming report", url: (by["louise-grooming-pdf"] || {}).url, as_of: feed}, model: by["open-meteo"]};
  }
  // yLog: yesterday's day log, used only when the hourly file has no open_yesterday field yet.
  function liveToDay(t, match, yLog) {
    match = match || function (n) { return [n]; };
    var src = sources(t), notes = (t.text && t.text.notes) || [];
    var best = {}, groomed = {}, firstOpen = {}, unmatched = [], anyGroomed = false;
    var hasY = (t.runs || []).some(function (r) { return r.open_yesterday === true || r.open_yesterday === false; });
    var yopen = hasY ? {} : null;
    (t.runs || []).forEach(function (r) {
      var st = TRACK[r.status] || "closed", time = null, note = null;
      if (st !== "open") {
        var said = notes.filter(function (n) { return r.key && (" " + norm(n.text) + " ").indexOf(" " + r.key + " ") >= 0; })[0];
        if (said) { st = "expected"; time = said.time; note = "the resort says"; }
        else if (st === "expected" || r.typical_first_open) {
          time = r.typical_first_open || null; note = st === "expected" ? "standby" : "usually opens by then"; st = "expected";
        }
      }
      var hits = match(r.name);
      if (hits === null) { unmatched.push(r.name); return; }
      hits.forEach(function (n) {
        if (!best[n] || RANK[st] > RANK[best[n].st]) best[n] = {st: st, time: time, note: note};
        if (r.groomed === true) groomed[n] = 1;
        // The earliest slot the tracker saw it open today; the run card shows it ("first open 9:00 am").
        if (r.first_open_today && (!firstOpen[n] || r.first_open_today < firstOpen[n])) firstOpen[n] = r.first_open_today;
        if (yopen && r.open_yesterday === true) yopen[n] = 1;
      });
      if (r.groomed === true) anyGroomed = true;
    });
    var yAsOf = null;
    if (!yopen && yLog && yLog.slots) {
      yopen = {};
      var lift = yLog.slots.map(function (s) { return s >= "09:00" && s <= "17:00"; });
      (yLog.runs || []).forEach(function (r) {
        if ((r.status || []).some(function (s, i) { return lift[i] && s === "open"; })) (match(r.name) || []).forEach(function (n) { yopen[n] = 1; });
      });
      var last = yLog.slots.filter(function (s, i) { return lift[i]; }).pop();
      yAsOf = last ? yLog.date + "T" + last + ":00-06:00" : null;
    }
    var names = Object.keys(best).sort();
    var mid = zone(t, "mid"), top = zone(t, "summit"), low = zone(t, "base");
    var snow = [mid.snow || {}, top.snow || {}, low.snow || {}];
    function claim(f) { return first(snow[0][f], snow[1][f], snow[2][f]); }
    var mz = mid.model ? mid : top, m = mz.model || {}, q = mz.quality || null;
    var measured = mz.model ? {
      source: {name: "Open-Meteo model at " + feet(mz.elev_m) + ", via the conditions tracker (an estimate, not a measurement)",
               url: src.model ? src.model.url : null, as_of: t.local_time},
      new_cm: {"24h": m.snow_24h_cm, "72h": m.snow_72h_cm, "7d": m.snow_7d_cm},
      temp_c: m.tmin_24h_c != null ? {min: m.tmin_24h_c, max: m.tmax_24h_c} : null,
      thaw_since_snow: (m.hours_above_0_72h || 0) >= 2 && (m.snow_since_thaw_cm || 0) < 5,
      days_since_snow: m.hours_since_snow == null ? null : Math.floor(m.hours_since_snow / 24),
      how: "model snowfall", quality: q ? {code: q.code, label: q.label, why: q.why} : null} : null;
    var season = t.season || {}, seen = {}, notesOut = [];
    notes.forEach(function (n) { if (!seen[n.text]) { seen[n.text] = 1; notesOut.push(n.text); } });
    if (t.errors && t.errors.length) notesOut.push("Tracker errors at this check: " + t.errors.join("; "));
    return {
      resort: t.resort, date: t.date, as_of: t.local_time, live: true,
      runs: {source: src.runs, open: names.filter(function (n) { return best[n].st === "open"; }),
             expected: names.filter(function (n) { return best[n].st === "expected"; }).map(function (n) { return {name: n, time: best[n].time, note: best[n].note}; }),
             closed: names.filter(function (n) { return best[n].st === "closed"; }),
             open_yesterday: yopen ? Object.keys(yopen).sort() : null, yesterday_as_of: yAsOf, first_open: firstOpen, unmatched: unmatched},
      lifts: (t.lifts || []).map(function (l) { return {name: l.name, status: TRACK[l.status] || "closed", time: l.status !== "open" ? l.typical_first_open || null : null}; }),
      groomed: anyGroomed ? {source: src.groom, names: Object.keys(groomed).sort(), unmatched: []} : null,
      snow: {claim: {source: src.snow, surface: null, new_cm: {overnight: claim("new_overnight_cm"), "24h": claim("new_24h_cm"), "48h": claim("new_48h_cm"), "7d": claim("new_7d_cm")}},
             measured: measured},
      base: season.base_cm != null ? {source: src.snow, cm: season.base_cm, upper_cm: snow[1].base_cm != null ? snow[1].base_cm : null, season: season.season,
                                       check: season.from && season.from !== "mid-mountain report" ? "Base is the " + season.from + "." : null} : null,
      notes: notesOut
    };
  }

  // The morning check's judgment on top of the latest hourly data. Without a morning check for that day, the
  // condition follows the tracker's snow-quality call and the verdict says it wasn't checked.
  function merge(morning, live) {
    // A newer morning check wins over an older hourly file, unless it's empty (no data reached the check).
    if (!live || (morning && morning.date > live.date && (morning.runs || morning.base || (morning.snow && morning.snow.claim)))) return morning;
    var d = JSON.parse(JSON.stringify(live)), mine = morning && morning.date === live.date ? morning : null;
    var claim = d.snow.claim, m = d.snow.measured;
    if (mine && mine.snow) {
      ["verdict", "verdict_text", "condition", "condition_why", "new_snow"].forEach(function (k) { d.snow[k] = mine.snow[k]; });
      d.take = mine.take || null;
      d.morning_at = mine.prepared_at; d.sample = mine.sample;
    } else {
      var c24 = first(claim.new_cm["24h"], claim.new_cm.overnight), m24 = m ? m.new_cm["24h"] : null, trusted = first(c24, m24);
      var q = m && m.quality, cond = q && QUALITY_COND[q.code] || "packed_powder";
      if (trusted != null && trusted >= POWDER_CM) cond = "bluebird_powder"; else if (cond === "bluebird_powder") cond = "packed_powder";
      d.snow.verdict = "unverified";
      d.snow.verdict_text = "No morning check yet for " + dayName(live.date) + ", so the resort's figures are shown as they are.";
      d.snow.condition = cond; d.snow.new_snow = trusted != null && trusted >= POWDER_CM;
      d.snow.condition_why = q ? "The tracker reads it as " + q.label.toLowerCase() + "." : "No snow data, so the default.";
    }
    return d;
  }

  // ---------- fetching, with the saved copy as the fallback ----------
  function fetchJSON(url) {
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 10000);
    // GitHub Pages caches files for about 10 minutes; a new query string each time gets the current one.
    url += (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
    return fetch(url, {cache: "no-store", signal: ctl ? ctl.signal : undefined}).then(function (r) {
      clearTimeout(timer);
      if (!r.ok) throw new Error("HTTP " + r.status);
      var saved = r.headers.get("X-RunFinder-Saved");          // the service worker answered from its saved copy
      return r.json().then(function (j) { return {json: j, saved: !!saved}; });
    }, function (e) { clearTimeout(timer); throw e; });
  }
  // The morning check. Opened from disk, fetch is blocked, so fall back to day/<resort>.js.
  function loadMorning(resort) {
    return fetchJSON("day/" + resort + ".json").catch(function () {
      return new Promise(function (resolve) {
        var s = document.createElement("script"); s.src = "day/" + resort + ".js";
        s.onload = function () { var j = (window.RUNFINDER_DAY || {})[resort]; resolve(j ? {json: j, saved: true} : null); };
        s.onerror = function () { resolve(null); };
        document.head.appendChild(s);
      });
    });
  }
  // Today's report for a resort: {day, fresh, checked}. fresh is true when the hourly file came over the network
  // just now. The last good report is also kept in localStorage, so it shows even if the browser drops its cache.
  function getDay(resort, match) {
    var key = "runfinder-last-" + resort, live = DATA && LIVE.indexOf(resort) >= 0;
    var jobs = [loadMorning(resort), live ? fetchJSON(DATA + "latest_" + resort + ".json").catch(function () { return null; }) : null];
    return Promise.all(jobs).then(function (res) {
      var morning = res[0] && res[0].json && res[0].json.resort === resort ? res[0].json : null;
      var hourly = res[1] && res[1].json && res[1].json.format === "banfftrack/1" ? res[1].json : null;
      // Only an hourly file without the open_yesterday field needs yesterday's log; null there means no record.
      var needY = hourly && !(hourly.runs || []).some(function (r) { return "open_yesterday" in r; });
      var yJob = needY ? fetchJSON(DATA + "days/" + resort + "/" + dayBefore(hourly.date) + ".json").then(function (y) { return y.json; }, function () { return null; }) : null;
      return Promise.resolve(yJob).then(function (yLog) {
        var day = merge(morning, hourly ? liveToDay(hourly, match, yLog) : null);
        var fresh = !!((res[1] && !res[1].saved && hourly) || (res[0] && !res[0].saved && morning));
        var last = storeJSON(key);
        if (day && (!last || !last.day || (day.as_of || day.prepared_at || "") >= (last.day.as_of || last.day.prepared_at || ""))) {
          if (fresh) storeJSON(key, {day: day, checked: Date.now()});
        } else if (last && last.day) {
          day = last.day;                                       // nothing newer reached us: the last good report
        }
        var lastOk = fresh ? Date.now() : (last && last.checked) || null;
        return {day: day, fresh: fresh, checked: lastOk, tried: Date.now()};
      });
    });
  }

  // Refresh when the app opens, comes back to the front (2 minutes or more since the last try), comes back online,
  // and hourly while it's open. Phones don't run web apps in the background, so this is what keeps it current: open
  // it in the car with signal and it saves the morning's data for the mountain.
  function autoRefresh(run) {
    var lastTry = 0, lastOk = 0;
    function go(force) {
      var now = Date.now();
      if (!force && now - lastTry < 2 * 60 * 1000) return;
      lastTry = now;
      run().then(function (ok) { if (ok) lastOk = Date.now(); });
    }
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") go(false); });
    window.addEventListener("online", function () { go(true); });
    setInterval(function () { if (document.visibilityState === "visible" && Date.now() - lastOk > 55 * 60 * 1000) go(true); }, 5 * 60 * 1000);
    go(true);
    return function () { go(true); };
  }
  function freshLine(r, busy) {
    var line = busy ? "Checking for new conditions&hellip;"
      : r && r.fresh ? "Updated on this phone at " + phoneClock(r.tried) + "."
      : location.protocol === "file:" ? "Opened from a file: showing the conditions saved with it."
      : r && r.day ? "Couldn't reach the internet" + (r.checked ? "; showing what was saved at " + phoneClock(r.checked) : "; showing the saved copy") + "."
      : "No conditions saved yet. Open the app with signal to get them.";
    if (!busy && r && r.day && r.day.date && r.day.date !== localToday()) {
      line += " " + (relDay(r.day.date) === "Yesterday" ? "Today's first check is at 5:30 AM Banff time; it's usually here by 6:00." : "");
    }
    return '<span>' + line + '</span><button type="button" class="rf-update"' + (busy ? " disabled" : "") + ">Update now</button>";
  }
  window.RunFinderApp = {getDay: getDay, autoRefresh: autoRefresh, freshLine: freshLine, clock: clock, dayName: dayName, relDay: relDay,
                         localToday: localToday, esc: esc, CONDS: CONDS, LIVE: LIVE, DATA: DATA};

  if (!RESORT) return;          // the home page has its own script; the rest is for resort pages

  // ---------- resort page: home button ----------
  var zoom = document.querySelector(".zoom");
  if (zoom) {
    var home = document.createElement("a"); home.className = "rf-home"; home.href = "./"; home.setAttribute("aria-label", "All resorts");
    home.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 11 12 3l9 8M5.5 9v11h5v-6h3v6h5V9" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';
    zoom.appendChild(home);
  }
  var railHead = document.querySelector(".rail h1");
  if (railHead) {
    var back = document.createElement("a"); back.className = "rf-back"; back.href = "./"; back.textContent = "← All resorts";
    railHead.parentNode.insertBefore(back, railHead);
  }

  // The page's own data: run names, and how much each run's cover adder can be.
  var D = null, builtIn = {};
  try { D = JSON.parse(document.getElementById("data").textContent); } catch (e) {}
  try { builtIn = JSON.parse((document.getElementById("rf-aliases") || {}).textContent || "{}"); } catch (e) {}
  var MAP_NAMES = D ? Object.keys(D.ratings).concat(D.map.pieces.map(function (p) { return p.name; }).filter(Boolean)) : null;
  // The alias list as published (aliases/<resort>.json), so a fix from the morning check reaches phones without
  // rebuilding the pages; the copy built into the page if it can't be fetched.
  var MAP_LIFTS = D ? (D.map.lifts || []).map(function (l) { return l.name; }) : [];
  var liftMatch = liftMatcher(MAP_LIFTS, builtIn);
  function loadMatcher() {
    if (!MAP_NAMES) return Promise.resolve(null);
    return fetchJSON("aliases/" + RESORT + ".json").then(function (a) { return a.json; }, function () { return builtIn; })
      .then(function (al) { liftMatch = liftMatcher(MAP_LIFTS, al || builtIn); return matcher(MAP_NAMES, al || builtIn); });
  }
  // What the page draws for today: the runs object, plus the map's runs the report doesn't mention ("no report",
  // never drawn as open: the user saw Paris drawn open while closed), and the lifts by map name.
  function statusFor(day) {
    var r = day.runs, seen = {}, lifts = {}, rank = {open: 3, expected: 2, closed: 1};
    (r.open || []).concat(r.closed || [], (r.expected || []).map(function (e) { return e.name; })).forEach(function (n) { seen[n] = 1; });
    var unknown = (MAP_NAMES || []).filter(function (n, i, a) { return !seen[n] && a.indexOf(n) === i; }).sort();
    (day.lifts || []).forEach(function (l) {
      var st = l.status === "hold" || l.status === "scheduled" ? "closed" : l.status;
      (liftMatch(l.name) || []).forEach(function (n) { if (!lifts[n] || (rank[st] || 0) > (rank[lifts[n]] || 0)) lifts[n] = st; });
    });
    return Object.assign({}, r, {unknown: unknown, lifts: lifts});
  }
  var RF = window.RunFinder || null;      // the page hook, once build_page.py has it
  var APPLIED = "runfinder-applied-" + RESORT;

  function seasonFor(cm) { var b = D ? D.cover_base : [100, 150]; return cm < b[0] ? "early" : cm < b[1] ? "mid" : "late"; }

  // Each control is set when its value in the report changes, so a family member's own change sticks until the
  // report has something new for that control. force sets them all (the reset button, or a new day).
  function applyToday(day, force) {
    var s = day.snow || {}, g = day.groomed, b = day.base;
    var t = {cond: CONDS[s.condition] ? s.condition : null, newSnow: typeof s.new_snow === "boolean" ? s.new_snow : null,
             groomed: g && g.names ? g.names : null, base: b && typeof b.cm === "number" ? b.cm : null};
    var last = storeJSON(APPLIED) || {};
    if (last.date !== day.date) { last = {date: day.date}; force = true; }
    var todo = {};
    ["cond", "newSnow", "groomed", "base"].forEach(function (k) {
      var v = t[k] === null ? null : JSON.stringify(t[k]);
      if (v !== null && (force || last[k] !== v)) { todo[k] = t[k]; last[k] = v; }
    });
    storeJSON(APPLIED, last);
    if (!Object.keys(todo).length) return;
    if (todo.groomed) todo.groomedNote = (day.live ? "Grooming report, " : "Morning report, ") + (g.source && g.source.as_of ? clock(g.source.as_of) + ": " : "") +
      plural(todo.groomed.length, "run") + " groomed. Every other black or double counts as ungroomed (+2°); greens and blues count as groomed.";
    // Picking powder or storm turns the new-snow box on, so a new condition always carries the report's box with it.
    if ("cond" in todo && t.newSnow !== null) todo.newSnow = t.newSnow;
    if (RF && RF.setToday) { RF.setToday(todo); return; }
    // Stopgap until the page has the hook: drive its controls the way a person would. A condition click also sets
    // the new-snow box, so the box goes second.
    if ("cond" in todo) { var cb = document.querySelector('#cond button[data-key="' + todo.cond + '"]'); if (cb) cb.click(); }
    if ("newSnow" in todo || "cond" in todo) {
      var ns = document.getElementById("newsnow");
      if (ns && t.newSnow !== null) { ns.checked = t.newSnow; ns.dispatchEvent(new Event("change")); }
    }
    if (todo.groomed) {
      var ta = document.getElementById("groom"), ap = document.getElementById("applygroom");
      if (ta && ap) { ta.value = todo.groomed.join(", "); ap.click(); }
    }
    if ("base" in todo) { var bb = document.getElementById("basecm"); if (bb) { bb.value = todo.base; bb.dispatchEvent(new Event("input")); } }
  }

  function render(res, busy) {
    var today = document.querySelector(".today");
    if (!today) return;
    var box = document.getElementById("rf-day") || document.createElement("div");
    box.id = "rf-day"; box.className = "rf-day";
    var h2 = today.querySelector("h2");
    if (!box.parentNode) { if (h2 && h2.nextSibling) today.insertBefore(box, h2.nextSibling); else today.appendChild(box); }
    var day = res && res.day;
    var fresh = '<div class="rf-fresh">' + freshLine(res, busy) + "</div>";
    if (!day) {
      box.innerHTML = "<h3>Today's conditions</h3>" + fresh + '<div class="small">Until then, set the controls below by hand.</div>';
      wire(); return;
    }
    curDate = day.date;
    var isToday = day.date === localToday(), rel = relDay(day.date);
    var rows = [];
    // Snow quality: the resort's claim, the measured data, and the condition button.
    var s = day.snow;
    if (s) {
      var c = s.claim || {}, m = s.measured || {}, nc = c.new_cm || {}, mc = m.new_cm || {};
      var claimBits = [];
      if (c.surface) claimBits.push(esc(c.surface));
      if (nc["24h"] != null) claimBits.push(nc["24h"] + " cm in 24 h");
      else if (nc.overnight != null) claimBits.push(nc.overnight + " cm overnight");
      if (nc["7d"] != null) claimBits.push(nc["7d"] + " cm in 7 days");
      var meas = [];
      if (mc["24h"] != null) meas.push(mc["24h"] + " cm in 24 h");
      if (mc["72h"] != null) meas.push(mc["72h"] + " cm in 72 h");
      if (m.temp_c && m.temp_c.min != null) meas.push(Math.round(m.temp_c.min) + " to " + Math.round(m.temp_c.max) + " °C");
      if (m.thaw_since_snow) meas.push("thawed since the last snow");
      var v = s.verdict || "unverified", cls = v === "agrees" ? "rf-ok" : v === "unverified" ? "" : "rf-bad";
      rows.push(["Snow",
        (claimBits.length ? "Resort says " + claimBits.join(", ") + ": " : "") + '<span class="' + cls + '">' + esc(VERDICT[v] || v) + "</span>." +
        (meas.length ? " Measured " + meas.join(", ") + (m.how ? " (" + esc(m.how) + ")" : "") + "." : "") +
        (m.quality && m.quality.label ? " Snow quality from the data: <b>" + esc(m.quality.label.toLowerCase()) + "</b>." : "") +
        (s.verdict_text ? " " + esc(s.verdict_text) : "") +
        (s.condition ? "<br>Set to <b>" + esc(CONDS[s.condition] || s.condition) + "</b>" + (s.new_snow ? ", new snow (no grooming adder)" : "") +
          (s.condition_why ? ": " + esc(s.condition_why) : ".") : "") +
        '<span class="src">' + [day.morning_at ? "Verdict and condition: morning check, " + clock(day.morning_at) : "",
          c.source ? "Claim: " + srcLine(c.source) : "", m.source ? "Measured: " + srcLine(m.source) : ""].filter(Boolean).join(" · ") + "</span>"]);
    }
    // Base depth, the season it sets, and which runs that adds degrees to.
    var b = day.base;
    if (b && typeof b.cm === "number") {
      var season = b.season || seasonFor(b.cm), scale = D ? D.cover_scale[season] : 0, max = D ? D.cover_max * scale : 0;
      var hit = [], openSet = null;
      if (D && max) {
        // With a lift and trail report, only the runs open or opening today; without one, every run.
        if (day.runs) { openSet = {}; (day.runs.open || []).concat((day.runs.expected || []).map(function (e) { return e.name; })).forEach(function (n) { openSet[n] = 1; }); }
        Object.keys(D.ratings).forEach(function (n) { var r = D.ratings[n]; if (r.cover && (!openSet || openSet[n])) hit.push([n, r.cover * max]); });
        hit.sort(function (a, z) { return z[1] - a[1]; });
      }
      rows.push(["Base", b.cm + " cm mid-mountain" + (b.upper_cm != null ? ", " + b.upper_cm + " cm up top" : "") + ": <b>" + season + " season</b>. " +
        (max ? "Up to +" + (+max.toFixed(1)) + "° on runs whose rocks and features need a deep base" +
          (hit.length ? ": " + plural(hit.length, "run") + (openSet ? " open today" : "") + ". Most exposed: " +
            hit.slice(0, 5).map(function (h) { return esc(h[0]) + " +" + h[1].toFixed(1) + "°"; }).join(", ") : openSet ? ", none of them open today" : "") + "."
          : "Deep enough that rocks and features are filled in: no cover adder.") +
        (b.check ? " " + esc(b.check) : "") + '<span class="src">' + srcLine(b.source) + "</span>"]);
    }
    // Runs open, expected and closed, and the change from yesterday.
    var r = day.runs;
    if (r) {
      var open = r.open || [], exp = r.expected || [], cl = r.closed || [], y = {};
      (r.open_yesterday || []).forEach(function (n) { y[n] = 1; });
      var closedNow = cl.filter(function (n) { return y[n]; });
      var newly = open.filter(function (n) { return r.open_yesterday && !y[n]; });
      var txt = plural(open.length, "run") + " open" + (exp.length ? ", " + exp.length + " expected later" : "") + (cl.length ? ", " + cl.length + " closed" : "") + ".";
      if (exp.length) txt += " Expected: " + exp.map(function (e) { return esc(e.name) + (e.time ? " " + hhmm(e.time) : "") + (e.note ? " (" + esc(e.note) + ")" : ""); }).join(", ") + ".";
      if (closedNow.length) txt += ' <span class="rf-bad">Open yesterday, closed now: ' + list(closedNow) + ".</span>";
      if (newly.length) txt += " New today: " + list(newly) + ".";
      if (r.open_yesterday === null || r.open_yesterday === undefined) txt += " No record of yesterday.";
      // The reports keep the day's statuses after the lifts stop (the tracker, 2026-11-15), so say so in the evening.
      var asOfTime = (/T(\d\d:\d\d)/.exec(day.as_of || day.prepared_at || "") || [])[1];
      if (day.live && asOfTime && asOfTime >= "16:30") txt += " The lifts stop around 4:30 PM, and the report keeps the day's statuses after that, so \u201copen\u201d here means it was open today.";
      var noReport = statusFor(day).unknown.length;
      if (noReport) txt += " " + plural(noReport, "run") + " on this map " + (noReport === 1 ? "isn't" : "aren't") +
        " in the resort's report (freeride zones, unlisted parks, connectors): no report, so not shown as open.";
      if (RF && RF.setStatus && isToday) txt += " Closed runs are faded on the map; “Open today” by the level chips hides them.";
      rows.push(["Runs", txt + '<span class="src">' + srcLine(r.source) + (r.yesterday_as_of ? " · yesterday as of " + dayName(r.yesterday_as_of.slice(0, 10)) + " " + clock(r.yesterday_as_of) : "") + "</span>"]);
    }
    // Lifts: which are turning, from the same report.
    if (day.lifts && day.lifts.length) {
      var lo = day.lifts.filter(function (l) { return l.status === "open"; }), le = day.lifts.filter(function (l) { return l.status === "expected"; });
      rows.push(["Lifts", plural(lo.length, "lift") + " of " + day.lifts.length + " open" + (lo.length ? ": " + lo.map(function (l) { return esc(l.name); }).join(", ") : "") + "." +
        (le.length ? " Expected: " + le.map(function (l) { return esc(l.name) + (l.time ? " " + hhmm(l.time) : ""); }).join(", ") + "." : "") +
        (RF && RF.setStatus && isToday ? " Lifts that aren't open are faded on the map." : "") + '<span class="src">' + srcLine(r && r.source) + "</span>"]);
    }
    var g = day.groomed;
    if (g && g.names) {
      rows.push(["Groomed", plural(g.names.length, "run") + " on the grooming report count +0°; any other black or double +2°, and greens and blues count as groomed" + (s && s.new_snow ? " (no adder today: new snow)" : "") + ". " +
        (g.names.length ? "<details><summary>Which runs</summary><p>" + g.names.map(esc).join(", ") + "</p></details>" : "") +
        '<span class="src">' + srcLine(g.source) + "</span>"]);
    }
    var unmatched = [].concat((r && r.unmatched) || [], (g && g.unmatched) || []);
    var asOf = day.as_of || day.prepared_at;
    var html = "<h3>" + (isToday ? "Today's conditions" : rel === "Yesterday" ? "Yesterday's conditions" : "Conditions from " + dayName(day.date)) + "</h3>" + fresh +
      '<div><b>' + (rel === "Yesterday" ? "Yesterday, " : "") + dayName(day.date) + ", as of " + clock(asOf) + "</b>" + (day.live ? " (the resort's own report, checked hourly)" : " (morning check)") + "</div>";
    if (day.sample) html += '<div class="rf-warn">Sample data for testing, not a real report.</div>';
    if (day.take) html += '<div class="rf-take"><b>Claude\'s take' + (day.morning_at || day.prepared_at ? " (" + clock(day.morning_at || day.prepared_at) + ")" : "") +
      ":</b> " + esc(day.take) + "</div>";
    if (!isToday) html += '<div class="rf-warn">' + (rel === "Yesterday" ? "These are yesterday's conditions" : "These conditions are from " + dayName(day.date)) +
      ", not today's, so the controls below aren't set from them. " + '<button type="button" class="btn" id="rf-apply">Use them anyway</button></div>';
    html += "<dl>" + rows.map(function (x) { return "<dt>" + x[0] + "</dt><dd>" + x[1] + "</dd>"; }).join("") + "</dl>";
    if (day.notes && day.notes.length) html += "<div>" + day.notes.map(esc).join("<br>") + "</div>";
    if (unmatched.length) html += "<details><summary>Report names not on this map (" + unmatched.length + ")</summary><p>" + unmatched.map(esc).join(", ") + "</p></details>";
    if (isToday) html += '<div class="row"><button type="button" class="btn" id="rf-apply">Reset the controls to these conditions</button></div>';
    box.innerHTML = html;
    var btn = document.getElementById("rf-apply");
    if (btn) btn.addEventListener("click", function () { applyToday(day, true); render(res); });
    wire();
  }
  function wire() { var u = document.querySelector("#rf-day .rf-update"); if (u) u.addEventListener("click", function () { refreshNow(); }); }

  var current = null;
  var refreshNow = autoRefresh(function () {
    render(current, true);
    return loadMatcher().then(function (match) { return getDay(RESORT, match); }).then(function (res) {
      current = res;
      var day = res.day;
      if (day && day.resort !== RESORT) day = res.day = null;
      // Only today's open and closed runs go on the map; yesterday's 4 PM closures would grey it out at 5:45 AM.
      var st = day && day.runs && day.date === localToday() ? statusFor(day) : null;
      window.RunFinderApp.status = st;                   // what the map was given, for checking
      if (RF && RF.setStatus) RF.setStatus(st);
      if (day && day.date === localToday()) applyToday(day, false);
      render(res);
      return res.fresh;
    }, function () { render(current); return false; });
  });
})();
