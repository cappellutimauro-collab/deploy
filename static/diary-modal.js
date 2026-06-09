(function () {
    var modal = document.querySelector('[data-diary-modal]');
    if (!modal) return;
    if (modal.parentElement !== document.body) document.body.appendChild(modal);

    var idInput = modal.querySelector('[data-diary-id]');
    var textarea = modal.querySelector('[data-diary-textarea]');
    var closeBtns = modal.querySelectorAll('[data-close-diary]');
    var categories = modal.querySelectorAll('[data-diary-category]');
    var active = new Map();

    function normalize(text) { return (text || '').trim(); }

    function rebuildTextarea(base) {
        var chunks = [normalize(base)].filter(Boolean);
        active.forEach(function (description) { chunks.push(description); });
        textarea.value = chunks.join('\n\n');
    }

    function open(button) {
        active.clear();
        categories.forEach(function (btn) { btn.classList.remove('active'); });
        if (idInput) idInput.value = button.dataset.appointmentId || '';
        if (textarea) textarea.value = button.dataset.diaryText || '';
        if (textarea) textarea.dataset.baseText = textarea.value || '';
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
        var opener = event.target.closest('[data-open-diary]');
        if (opener) {
            event.preventDefault();
            open(opener);
            return;
        }
        var category = event.target.closest('[data-diary-category]');
        if (category && textarea) {
            event.preventDefault();
            var key = category.dataset.serviceId || category.textContent;
            var description = normalize(category.dataset.description || category.textContent);
            if (active.has(key)) {
                active.delete(key);
                category.classList.remove('active');
            } else {
                active.set(key, description);
                category.classList.add('active');
            }
            rebuildTextarea(textarea.dataset.baseText || '');
        }
    });

    closeBtns.forEach(function (btn) { btn.addEventListener('click', close); });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.hidden) close();
    });
})();
