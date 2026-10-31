/* Service worker do DUOTATTI: notificações da Tatti */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) { d = {corpo: event.data && event.data.text()}; }
  const titulo = d.titulo || 'Tatti';
  const opcoes = {
    body: d.corpo || 'Hora de estudar!',
    icon: d.icone || '/static/icones/neutro-192.png',
    image: d.imagem,
    badge: d.badge || '/static/icones/badge-96.png',
    tag: d.tag || 'tatti',
    renotify: true,
    vibrate: [120, 60, 120],
    data: {url: d.url || '/'}
  };
  const tarefas = [self.registration.showNotification(titulo, opcoes)];
  if ('setAppBadge' in self.navigator && d.pendentes) tarefas.push(self.navigator.setAppBadge(d.pendentes).catch(() => {}));
  event.waitUntil(Promise.all(tarefas));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(abas => {
    for (const c of abas) { if ('focus' in c) { c.navigate(url).catch(() => {}); return c.focus(); } }
    return clients.openWindow(url);
  }));
});
