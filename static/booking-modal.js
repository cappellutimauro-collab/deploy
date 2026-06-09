(function () {
    var modal = document.querySelector('[data-service-modal]');
    if (!modal) return;
    if (modal.parentElement !== document.body) document.body.appendChild(modal);

    var slotInput = modal.querySelector('[data-service-modal-slot-id]');
    var slotLabel = modal.querySelector('[data-service-modal-slot]');
    var descriptionPanel = modal.querySelector('[data-service-description-panel]');
    var confirmButton = modal.querySelector('[data-service-confirm]');

    function buzz(ms) {
        if (navigator.vibrate) navigator.vibrate(ms || 8);
    }

    function setDescription(title, body) {
        if (!descriptionPanel) return;
        descriptionPanel.textContent = '';
        var strong = document.createElement('strong');
        strong.textContent = title || 'Descrizione';
        var paragraph = document.createElement('p');
        paragraph.textContent = body || 'Descrizione non inserita.';
        descriptionPanel.appendChild(strong);
        descriptionPanel.appendChild(paragraph);
    }

    function resetServices() {
        modal.querySelectorAll('input[type="radio"]').forEach(function (input) { input.checked = false; });
        modal.querySelectorAll('[data-service-tab]').forEach(function (tab) { tab.classList.remove('active'); });
        if (confirmButton) confirmButton.disabled = true;
        setDescription('Descrizione', 'Seleziona una tipologia.');
    }

    function openModal(button) {
        if (!button || !slotInput) return;
        slotInput.value = button.dataset.slotId || '';
        if (slotLabel) slotLabel.textContent = button.dataset.slotLabel || 'Appuntamento';
        resetServices();
        modal.hidden = false;
        modal.classList.add('is-open');
        document.body.classList.add('modal-open');
        buzz(8);
        window.setTimeout(function () {
            var first = modal.querySelector('input[type="radio"]');
            if (first) first.focus({ preventScroll: true });
        }, 50);
    }

    function closeModal() {
        modal.classList.remove('is-open');
        modal.hidden = true;
        document.body.classList.remove('modal-open');
    }

    function selectService(input) {
        if (!input) return;
        modal.querySelectorAll('[data-service-tab]').forEach(function (tab) { tab.classList.remove('active'); });
        var tab = input.closest('[data-service-tab]');
        if (tab) tab.classList.add('active');
        var label = tab ? tab.textContent.trim() : 'Prestazione';
        var description = input.dataset.serviceDescription || 'Descrizione non inserita.';
        setDescription(label, description);
        if (confirmButton) confirmButton.disabled = false;
        buzz(6);
    }

    document.addEventListener('click', function (event) {
        var openButton = event.target.closest('[data-open-service-modal]');
        if (openButton) {
            event.preventDefault();
            openModal(openButton);
            return;
        }
        if (event.target.closest('[data-close-service-modal]')) {
            event.preventDefault();
            closeModal();
        }
    });

    modal.addEventListener('change', function (event) {
        if (event.target.matches('input[type="radio"]')) selectService(event.target);
    });

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.hidden) closeModal();
    });
})();
