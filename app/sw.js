// Service worker for the Ski Run Finder app. build_app.py fills in VERSION and PRECACHE.
// The app shell and maps are saved at install, so they work offline on the mountain. Conditions files (the
// tracker's hourly JSON and the morning check's day files) are fetched fresh when there's signal and fall back to
// the saved copy when there isn't; a saved answer carries an X-RunFinder-Saved header so the page can say so.
"use strict";
var VERSION = "86e1f78e8b";
var PRECACHE = ["./", "sunshine.html", "sunshine_v22_terrain.jpg", "louise.html", "louise_v11_terrain.jpg", "crystal.html", "crystal_v1_terrain.jpg", "app.js", "manifest.webmanifest", "aliases/sunshine.json", "aliases/louise.json", "aliases/crystal.json", "icons/icon-180.png", "icons/icon-192.png", "icons/icon-32.png", "icons/icon-512.png", "icons/icon-maskable-512.png", "index.html", "day/crystal.js", "day/crystal.json", "day/louise.js", "day/louise.json", "day/sunshine.js", "day/sunshine.json"];
var CACHE = "runfinder-" + VERSION, FONTS = "runfinder-fonts", DATA = "runfinder-data";
var CONDITIONS = /(\.json|\/day\/[a-z]+\.js)$/;

self.addEventListener("install", function (e) {
  // cache: "reload" skips the browser's HTTP cache, so a new version never saves an old file.
  e.waitUntil(caches.open(CACHE)
    .then(function (c) { return c.addAll(PRECACHE.map(function (u) { return new Request(u, {cache: "reload"}); })); })
    .then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf("runfinder-") === 0 && k !== CACHE && k !== FONTS && k !== DATA; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

function cacheFirst(req) {
  return caches.open(CACHE).then(function (c) {
    return c.match(req, {ignoreSearch: true}).then(function (hit) { return hit || fetch(req); });
  });
}

function marked(res) {
  var h = new Headers(res.headers);
  h.set("X-RunFinder-Saved", "1");
  return res.blob().then(function (body) { return new Response(body, {status: res.status, statusText: res.statusText, headers: h}); });
}

// Conditions: try the network for up to 6 s and keep what it returns; otherwise the saved copy (from an earlier
// fetch, or the install for the day files), marked as saved.
function networkFirst(req) {
  var key = req.url.split("?")[0];
  return caches.open(DATA).then(function (c) {
    var net = fetch(req.url, {cache: "no-store"}).then(function (res) {
      if (res.ok) c.put(key, res.clone());
      return res;
    });
    net.catch(function () {});
    var late = new Promise(function (resolve) { setTimeout(resolve, 6000); });
    return Promise.race([net, late]).then(function (res) { return res && res.ok ? res : Promise.reject(); })
      .catch(function () {
        return c.match(key).then(function (hit) { return hit || caches.match(key, {ignoreSearch: true}); })
          .then(function (hit) {
            if (hit) return marked(hit);
            // Nothing saved: the network's answer if it comes, else a plain "offline" reply the page handles.
            return net.catch(function () { return new Response("", {status: 504, statusText: "Offline"}); });
          });
      });
  });
}

// Google Fonts: serve the saved copy and refresh it in the background. The pages fall back to system fonts.
function fonts(req) {
  return caches.open(FONTS).then(function (c) {
    return c.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) { if (res.ok || res.type === "opaque") c.put(req, res.clone()); return res; });
      return hit || net;
    });
  });
}

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") e.respondWith(fonts(req));
  else if (CONDITIONS.test(url.pathname)) e.respondWith(networkFirst(req));
  else if (url.origin === self.location.origin) e.respondWith(cacheFirst(req));
});

// The home page asks how much is saved, to show "Ready offline".
self.addEventListener("message", function (e) {
  if (!e.data || e.data.type !== "status" || !e.ports[0]) return;
  caches.open(CACHE).then(function (c) { return c.keys(); }).then(function (keys) {
    e.ports[0].postMessage({version: VERSION, saved: keys.length, total: PRECACHE.length});
  });
});
