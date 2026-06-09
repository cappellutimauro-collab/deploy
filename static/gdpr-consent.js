(function () {
    var modal = document.querySelector('[data-gdpr-modal]');
    if (!modal) return;
    if (modal.parentElement !== document.body) document.body.appendChild(modal);

    var openBtn = document.querySelector('[data-open-gdpr]');
    var closeBtns = modal.querySelectorAll('[data-close-gdpr]');
    var scrollBox = modal.querySelector('[data-gdpr-scroll]');
    var acceptWrap = modal.querySelector('[data-gdpr-accept]');
    var checkbox = modal.querySelector('[data-gdpr-checkbox]');
    var hidden = document.querySelector('[data-gdpr-value]');
    var status = document.querySelector('[data-gdpr-status]');
    var bottomBtn = modal.querySelector('[data-gdpr-scroll-bottom]');
    var confirmBtn = modal.querySelector('[data-gdpr-final-confirm]');
    var form = document.querySelector('[data-registration-form]');

    function open() {
        modal.hidden = false;
        modal.classList.add('is-open');
        document.body.classList.add('modal-open');
    }

    function close() {
        modal.classList.remove('is-open');
        modal.hidden = true;
        document.body.classList.remove('modal-open');
    }

    function revealIfBottom() {
        if (!scrollBox || !acceptWrap) return;
        var atBottom = scrollBox.scrollTop + scrollBox.clientHeight >= scrollBox.scrollHeight - 8;
        if (atBottom) {
            acceptWrap.hidden = false;
            if (confirmBtn) confirmBtn.hidden = false;
        }
    }

    function updateConfirm() {
        if (confirmBtn && checkbox) confirmBtn.disabled = !checkbox.checked;
    }

    if (openBtn) openBtn.addEventListener('click', open);
    closeBtns.forEach(function (btn) { btn.addEventListener('click', close); });
    if (scrollBox) scrollBox.addEventListener('scroll', revealIfBottom);
    if (bottomBtn && scrollBox) bottomBtn.addEventListener('click', function () {
        scrollBox.scrollTo({ top: scrollBox.scrollHeight, behavior: 'smooth' });
        window.setTimeout(revealIfBottom, 350);
    });
    if (checkbox) checkbox.addEventListener('change', updateConfirm);
    if (confirmBtn) confirmBtn.addEventListener('click', function () {
        if (!checkbox || !checkbox.checked) return;
        if (hidden) hidden.value = '1';
        if (status) status.textContent = 'Consenso accettato';
        close();
    });
    if (form) form.addEventListener('submit', function (event) {
        if (!hidden || hidden.value !== '1') {
            event.preventDefault();
            open();
        }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.hidden) close();
    });
})();
