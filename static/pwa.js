(function () {
    var deferredPrompt = null;
    var installButton = document.querySelector('[data-install-app]');
    var banner = document.querySelector('[data-install-app-banner]');
    var dismiss = document.querySelector('[data-install-dismiss]');
    var dismissed = localStorage.getItem('rehabInstallDismissed') === '1';

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/service-worker.js').catch(function () {});
        });
    }

    function showBanner() {
        if (banner && !dismissed) {
            if (location.pathname !== '/' && location.pathname !== '/login') return;
            var notificationBanner = document.querySelector('[data-notification-banner]');
            if (notificationBanner) notificationBanner.remove();
            banner.hidden = false;
        }
    }

    function hideBanner() {
        if (banner) banner.hidden = true;
    }

    window.addEventListener('beforeinstallprompt', function (event) {
        event.preventDefault();
        deferredPrompt = event;
        showBanner();
    });

    if (installButton) {
        installButton.addEventListener('click', async function () {
            if (!deferredPrompt) return;
            deferredPrompt.prompt();
            await deferredPrompt.userChoice;
            deferredPrompt = null;
            hideBanner();
        });
    }

    if (dismiss) {
        dismiss.addEventListener('click', function () {
            dismissed = true;
            localStorage.setItem('rehabInstallDismissed', '1');
            hideBanner();
        });
    }
})();
