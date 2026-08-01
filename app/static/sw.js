/*
 * wger-hero service worker — deliberately small.
 *
 * It caches exactly one thing: the fixed list of static, non-sensitive assets
 * below. Nothing else ever enters Cache Storage. There is no "cache all GET
 * requests" strategy, no runtime caching of API or HTML responses, no
 * background sync, no push, and no offline write path.
 *
 * Why so strict: this is a single-user app whose pages contain personal
 * training data behind a login. A cached authenticated page would survive
 * logout in the browser profile, so pages are network-only, full stop. The
 * server marks them Cache-Control: no-store, private, and this worker respects
 * that by never looking at them.
 */

// Bump this on every asset change: the old cache is deleted on activate.
const CACHE_VERSION = 'wger-hero-static-v1';

// The complete allowlist. Only same-origin, static, non-sensitive files.
const STATIC_ASSETS = [
  '/offline',
  '/static/style.css',
  '/static/icons/icon.svg',
  '/static/icons/icon-maskable.svg',
  '/manifest.webmanifest',
];

// Never cached, never served from cache — every dynamic or authenticated path.
// Kept as an explicit list so the boundary is readable and testable rather than
// implied by the absence of a rule.
const NEVER_CACHE = [
  '/',
  '/login',
  '/logout',
  '/today',
  '/week',
  '/goals',
  '/habits',
  '/quests',
  '/japanese',
  '/settings',
  '/stats',
  '/achievements',
  '/healthz',
  '/sync',
];

function isStaticAsset(url) {
  return STATIC_ASSETS.indexOf(url.pathname) !== -1;
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_VERSION)
          .map((name) => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Only plain GETs are ever considered. A POST — completing a habit, logging
  // in, importing a SAVE — always goes to the network and is never stored.
  if (request.method !== 'GET') {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;   // third-party requests are none of this worker's business
  }

  // A navigation that fails offline gets the static offline page. The page
  // itself is never populated with user data, so nothing personal is shown.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/offline'))
    );
    return;
  }

  // Cache-first, but only for the fixed allowlist above.
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request))
    );
    return;
  }

  // Everything else: network-only, and the response is not stored.
});
