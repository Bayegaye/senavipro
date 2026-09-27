// Service worker minimal pour rendre la page de commande installable
// ("Ajouter à l'écran d'accueil") sur mobile. Ne met en cache que les
// éléments statiques indispensables à l'affichage hors-ligne de l'écran de
// démarrage ; toutes les requêtes de données (catalogue, envoi de commande)
// repassent toujours par le réseau, pour ne jamais servir des prix ou un
// stock périmés.
const CACHE_NAME = "senavipro-shell-v1";
const SHELL_URLS = [
  "/static/img/logo.jpg",
  "/static/img/icon-192.png",
  "/static/img/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  // Ressources statiques : cache d'abord, réseau en repli.
  if (SHELL_URLS.some((u) => url.pathname === u)) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
    return;
  }
  // Tout le reste (pages, catalogue, formulaire) : toujours le réseau.
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
