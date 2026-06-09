(function () {
    var modal = document.querySelector('[data-consent-modal]');
    if (!modal) return;
    if (modal.parentElement !== document.body) document.body.appendChild(modal);

    var openers = document.querySelectorAll('[data-open-consent]');
    var closers = modal.querySelectorAll('[data-close-consent-modal]');
    var form = modal.querySelector('[data-consent-form]');
    var counter = modal.querySelector('[data-missing-counter]');
    var minorToggle = modal.querySelector('[data-minor-toggle]');
    var guardianBlock = modal.querySelector('[data-guardian-block]');
    var guardianRequired = guardianBlock ? guardianBlock.querySelectorAll('[data-guardian-required]') : [];
    var canvas = modal.querySelector('[data-signature-canvas]');
    var signatureInput = modal.querySelector('[data-signature-data]');
    var clearSignature = modal.querySelector('[data-clear-signature]');
    var ctx = canvas ? canvas.getContext('2d') : null;
    var drawing = false;
    var hasSignature = false;

    function open() {
        modal.hidden = false;
        modal.classList.add('is-open');
        document.body.classList.add('modal-open');
        setTimeout(function () {
            resizeCanvas();
            updateCounter();
        }, 40);
    }

    function close() {
        modal.classList.remove('is-open');
        modal.hidden = true;
        document.body.classList.remove('modal-open');
    }

    function isVisible(input) {
        return !input.disabled && !input.closest('[hidden]');
    }

    function setGuardianState() {
        var active = !!(minorToggle && minorToggle.checked);
        if (guardianBlock) guardianBlock.hidden = !active;
        guardianRequired.forEach(function (input) {
            input.required = active;
            input.disabled = !active;
        });
        updateCounter();
    }

    function requiredMissingCount() {
        if (!form) return 0;
        var missing = 0;
        var seenRadios = {};
        Array.prototype.forEach.call(form.querySelectorAll('[required]'), function (input) {
            if (input === signatureInput) return;
            if (!isVisible(input)) return;
            if (input.type === 'radio') {
                if (seenRadios[input.name]) return;
                seenRadios[input.name] = true;
                if (!form.querySelector('input[name="' + input.name + '"]:checked')) missing += 1;
                return;
            }
            if (input.type === 'checkbox') {
                if (!input.checked) missing += 1;
                return;
            }
            if (!String(input.value || '').trim()) missing += 1;
        });
        if (signatureInput && !signatureInput.value) missing += 1;
        return missing;
    }

    function updateCounter() {
        if (!counter) return;
        var missing = requiredMissingCount();
        counter.textContent = missing + (missing === 1 ? ' campo mancante' : ' campi mancanti');
    }

    function resizeCanvas() {
        if (!canvas || !ctx) return;
        var rect = canvas.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        var ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * ratio);
        canvas.height = Math.round(rect.height * ratio);
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.lineWidth = 3;
        ctx.strokeStyle = '#0b3a5c';
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, rect.width, rect.height);
        hasSignature = false;
        if (signatureInput) signatureInput.value = '';
    }

    function point(event) {
        var rect = canvas.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function startDraw(event) {
        if (!ctx) return;
        drawing = true;
        canvas.setPointerCapture && canvas.setPointerCapture(event.pointerId);
        var p = point(event);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        event.preventDefault();
    }

    function moveDraw(event) {
        if (!drawing || !ctx) return;
        var p = point(event);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
        hasSignature = true;
        if (signatureInput) signatureInput.value = canvas.toDataURL('image/png');
        updateCounter();
        event.preventDefault();
    }

    function endDraw(event) {
        if (!drawing) return;
        drawing = false;
        if (hasSignature && signatureInput) signatureInput.value = canvas.toDataURL('image/png');
        updateCounter();
        event.preventDefault();
    }

    openers.forEach(function (btn) { btn.addEventListener('click', open); });
    closers.forEach(function (btn) { btn.addEventListener('click', close); });
    if (minorToggle) minorToggle.addEventListener('change', setGuardianState);
    if (form) {
        form.addEventListener('input', updateCounter);
        form.addEventListener('change', updateCounter);
        form.addEventListener('submit', function (event) {
            updateCounter();
            if (requiredMissingCount() > 0) {
                event.preventDefault();
                counter && counter.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        });
    }
    if (canvas) {
        canvas.addEventListener('pointerdown', startDraw);
        canvas.addEventListener('pointermove', moveDraw);
        canvas.addEventListener('pointerup', endDraw);
        canvas.addEventListener('pointercancel', endDraw);
        canvas.addEventListener('pointerleave', endDraw);
    }
    if (clearSignature) clearSignature.addEventListener('click', function () { resizeCanvas(); updateCounter(); });
    window.addEventListener('resize', function () { if (!modal.hidden && !hasSignature) resizeCanvas(); });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.hidden) close();
    });
    setGuardianState();
})();
