(function () {
    function ready(fn) {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
        else fn();
    }

    function ensureLiveRegion() {
        var live = document.querySelector('[data-form-live-region]');
        if (live) return live;
        live = document.createElement('div');
        live.className = 'form-status-live';
        live.setAttribute('aria-live', 'polite');
        live.setAttribute('data-form-live-region', '');
        document.body.appendChild(live);
        return live;
    }

    function labelText(input) {
        var label = input.closest('label') || (input.id && document.querySelector('label[for="' + input.id + '"]'));
        return label ? label.textContent.replace(/\s+/g, ' ').trim() : (input.name || 'Campo');
    }

    function messageFor(input) {
        if (input.validity.valueMissing) return labelText(input) + ': campo obbligatorio.';
        if (input.validity.typeMismatch) return labelText(input) + ': formato non valido.';
        if (input.validity.tooShort) return labelText(input) + ': inserisci almeno ' + input.minLength + ' caratteri.';
        if (input.validity.rangeUnderflow) return labelText(input) + ': valore troppo basso.';
        if (input.validity.patternMismatch) return labelText(input) + ': formato non valido.';
        return input.validationMessage || 'Controlla questo campo.';
    }

    function errorNode(input) {
        var id = input.id || ('field-' + Math.random().toString(36).slice(2));
        input.id = id;
        var errorId = id + '-error';
        var node = document.getElementById(errorId);
        if (!node) {
            node = document.createElement('small');
            node.className = 'field-error';
            node.id = errorId;
            input.insertAdjacentElement('afterend', node);
        }
        input.setAttribute('aria-describedby', [input.getAttribute('aria-describedby'), errorId].filter(Boolean).join(' '));
        return node;
    }

    function validateField(input, announce) {
        if (!input.willValidate) return true;
        if (input.checkValidity()) {
            input.removeAttribute('aria-invalid');
            var old = document.getElementById((input.id || '') + '-error');
            if (old) old.textContent = '';
            return true;
        }
        input.setAttribute('aria-invalid', 'true');
        var message = messageFor(input);
        errorNode(input).textContent = message;
        if (announce) ensureLiveRegion().textContent = message;
        return false;
    }

    ready(function () {
        document.querySelectorAll('form').forEach(function (form) {
            form.addEventListener('input', function (event) {
                var target = event.target;
                if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) {
                    if (target.getAttribute('aria-invalid') === 'true') validateField(target, false);
                }
            });

            form.addEventListener('blur', function (event) {
                var target = event.target;
                if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) {
                    validateField(target, false);
                }
            }, true);

            form.addEventListener('submit', function (event) {
                if (event.defaultPrevented) return;
                var fields = Array.prototype.slice.call(form.querySelectorAll('input, select, textarea'));
                var invalid = fields.filter(function (field) { return !validateField(field, false); });
                if (!invalid.length) return;
                event.preventDefault();
                invalid[0].focus({ preventScroll: false });
                ensureLiveRegion().textContent = invalid.length === 1 ? messageFor(invalid[0]) : invalid.length + ' campi da controllare.';
            });
        });
    });
})();
