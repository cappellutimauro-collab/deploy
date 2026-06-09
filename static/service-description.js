(function () {
    var modal = document.querySelector('[data-service-description-modal]');
    if (!modal) return;
    if (modal.parentElement !== document.body) document.body.appendChild(modal);

    var idInput = modal.querySelector('[data-service-description-id]');
    var textarea = modal.querySelector('[data-service-description-text]');
    var label = modal.querySelector('[data-service-description-label]');
    var counter = modal.querySelector('[data-service-description-counter]');
    var closeBtns = modal.querySelectorAll('[data-close-service-description]');

    function updateCounter() {
        if (counter && textarea) counter.textContent = String(textarea.value.length);
    }

    function open(button) {
        if (idInput) idInput.value = button.dataset.serviceId || '';
        if (textarea) textarea.value = button.dataset.serviceDescription || '';
        if (label) label.textContent = 'Descrizione ' + (button.dataset.serviceName || 'servizio');
        updateCounter();
        modal.hidden = false;
        modal.classList.add('is-open');
        document.body.classList.add('modal-open');
    }

    function close() {
        modal.classList.remove('is-open');
        modal.hidden = true;
        document.body.classList.remove('modal-open');
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-open-service-description]');
        if (!button) return;
        event.preventDefault();
        open(button);
    });
    if (textarea) textarea.addEventListener('input', updateCounter);
    closeBtns.forEach(function (btn) { btn.addEventListener('click', close); });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.hidden) close();
    });
})();
