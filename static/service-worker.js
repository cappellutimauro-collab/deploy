const CACHE_NAME = 'rehab-studio-static-v25';
const STATIC_ASSETS = [
  '/static/styles.css',
  '/static/design-system.css',
  '/static/ui-2026.css',
  '/static/date-picker.js',
  '/static/booking-modal.js',
  '/static/scanner.js',
  '/static/jsQR.min.js',
  '/static/gdpr-consent.js',
  '/static/consent-form.js',
  '/static/diary-modal.js',
  '/static/service-description.js',
  '/static/pwa.js',
  '/static/notifications.js',
  '/static/login-guard.js',
  '/static/form-validation.js',
  '/static/doctor-profile.js',
  '/static/ui-polish.js',
  '/static/app-icon-192.png',
  '/static/app-icon-512.png',
  '/manifest.webmanifest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET') return;
  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    event.respondWith(
      fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      }).catch(() => caches.match(request))
    );
    return;
  }
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => new Response(
        '<!doctype html><html lang="it"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Offline</title><body style="font-family:system-ui;margin:0;padding:32px;background:#f3f5f2;color:#18211e"><h1>Connessione assente</h1><p>Riprova quando il dispositivo torna online.</p></body></html>',
        { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      ))
    );
  }
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data && event.notification.data.url ? event.notification.data.url : '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(target);
      return undefined;
    })
  );
});
