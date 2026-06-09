(function () {
    if (!document.body.classList.contains('signed-in')) return;
    if (!('Notification' in window)) return;

    var MAX_DELAY = 6 * 60 * 60 * 1000;

    function storageKey(event) {
        return 'rehabNotice:' + event.id;
    }

    function alreadySent(event) {
        return localStorage.getItem(storageKey(event)) === '1';
    }

    function markSent(event) {
        localStorage.setItem(storageKey(event), '1');
    }

    function ensureBanner() {
        var existing = document.querySelector('[data-notification-banner]');
        if (existing) return existing;
        var banner = document.createElement('aside');
        banner.className = 'notification-banner system-notice';
        banner.dataset.notificationBanner = 'true';
        banner.innerHTML = '<span class="system-notice__icon" aria-hidden="true"></span><span class="system-notice__text">Promemoria sedute</span><button type="button" class="system-notice__action">Attiva</button><button type="button" class="banner-close" aria-label="Chiudi">x</button>';
        document.body.appendChild(banner);
        banner.querySelector('button').addEventListener('click', function () {
            Notification.requestPermission().then(function () { banner.remove(); });
        });
        banner.querySelector('.banner-close').addEventListener('click', function () {
            localStorage.setItem('rehabNotificationsDismissed', '1');
            banner.remove();
        });
        return banner;
    }

    function maybeAskPermission(events) {
        if (!events.length) return;
        if (Notification.permission !== 'default') return;
        if (localStorage.getItem('rehabNotificationsDismissed') === '1') return;
        if (location.pathname !== '/' && location.pathname !== '/login') return;
        var installBanner = document.querySelector('[data-install-app-banner]');
        if (installBanner && !installBanner.hidden) return;
        ensureBanner();
    }

    function show(event) {
        if (alreadySent(event)) return;
        if (Notification.permission !== 'granted') return;
        markSent(event);
        var options = {
            body: event.body,
            tag: event.tag || event.id,
            icon: '/static/app-icon-192.png',
            badge: '/static/app-icon-192.png',
            data: { url: event.url || '/' }
        };
        if (navigator.serviceWorker && navigator.serviceWorker.ready) {
            navigator.serviceWorker.ready.then(function (registration) {
                registration.showNotification(event.title, options);
            }).catch(function () {
                new Notification(event.title, options);
            });
        } else {
            new Notification(event.title, options);
        }
    }

    function schedule(event) {
        if (alreadySent(event)) return;
        var due = new Date(event.due_at).getTime();
        var delay = due - Date.now();
        if (delay <= 0) {
            show(event);
            return;
        }
        window.setTimeout(function () { schedule(event); }, Math.min(delay, MAX_DELAY));
    }

    fetch('/api/notifications', { credentials: 'same-origin' })
        .then(function (response) { return response.ok ? response.json() : { events: [] }; })
        .then(function (payload) {
            var events = Array.isArray(payload.events) ? payload.events : [];
            maybeAskPermission(events);
            events.forEach(schedule);
        })
        .catch(function () {});
})();
