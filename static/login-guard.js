(function () {
    var form = document.querySelector('[data-login-form]');
    if (!form) return;

    var message = form.querySelector('[data-login-guard-message]');
    var confirmed = form.querySelector('[data-login-confirmed]');
    var submitIntentAt = 0;

    function setConfirmed(value) {
        if (confirmed) confirmed.value = value ? '1' : '0';
    }

    function showGuard() {
        setConfirmed(false);
        if (message) message.hidden = false;
        var remember = form.querySelector('input[name="remember"]');
        if (remember) remember.focus({ preventScroll: false });
    }

    function hideGuard() {
        if (message) message.hidden = true;
    }

    function markSubmitIntent() {
        submitIntentAt = Date.now();
        setConfirmed(true);
        hideGuard();
    }

    setConfirmed(false);

    form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (button) {
        button.addEventListener('pointerdown', markSubmitIntent, { passive: true });
        button.addEventListener('touchstart', markSubmitIntent, { passive: true });
        button.addEventListener('click', markSubmitIntent);
    });

    form.addEventListener('keydown', function () {
        setConfirmed(false);
        hideGuard();
    });

    form.addEventListener('input', function () {
        setConfirmed(false);
        hideGuard();
    });

    form.addEventListener('change', function () {
        setConfirmed(false);
        hideGuard();
    });

    form.addEventListener('submit', function (event) {
        var recentIntent = Date.now() - submitIntentAt < 8000;
        var explicit = confirmed && confirmed.value === '1' && recentIntent;
        if (explicit) return;
        event.preventDefault();
        showGuard();
    });
})();

