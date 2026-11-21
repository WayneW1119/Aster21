// Network-only: authenticated pages, conversations and tokens are never cached here.
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>event.respondWith(fetch(event.request)));
