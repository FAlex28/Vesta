/*
 * Vesta service worker: only Web Push for emergency contacts.
 * The push payload carries no coordinates; tapping it opens /contact, which needs the
 * contact's own access. No caching, no background sync.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: "Vesta", body: event.data ? event.data.text() : "" };
  }
  const alert = data.kind === "ALERT_RAISED";
  event.waitUntil(
    self.registration.showNotification(data.title || "Vesta", {
      body: data.body || "",
      tag: data.tag || "vesta",
      renotify: true,
      requireInteraction: Boolean(data.requireInteraction),
      vibrate: alert ? [400, 150, 400, 150, 400] : [120],
      icon: "/vesta-icon.svg",
      badge: "/vesta-icon.svg",
      data: { url: data.url || "/contact" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/contact", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (client.url.startsWith(target) && "focus" in client) return client.focus();
      }
      return self.clients.openWindow(target);
    }),
  );
});
