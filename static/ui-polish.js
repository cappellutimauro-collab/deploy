(function () {
    function ready(fn) {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
        else fn();
    }

    ready(function () {
        document.body.classList.add('ui-ready');
        var prefersReducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        if ('startViewTransition' in document && !prefersReducedMotion) {
            document.documentElement.classList.add('supports-view-transitions');
        }

        function focusableElements(root) {
            return Array.prototype.slice.call(root.querySelectorAll([
                'a[href]',
                'area[href]',
                'button:not([disabled])',
                'input:not([disabled]):not([type="hidden"])',
                'select:not([disabled])',
                'textarea:not([disabled])',
                '[tabindex]:not([tabindex="-1"])'
            ].join(','))).filter(function (element) {
                return element.offsetParent !== null || element === document.activeElement;
            });
        }

        function activeModal() {
            return document.querySelector('.booking-modal.is-open:not([hidden]), [role="dialog"]:not([hidden])');
        }

        var previousFocus = null;
        var lastOpenModal = null;

        function prepareModal(modal) {
            if (!modal || modal.dataset.a11yReady === 'true') return;
            modal.dataset.a11yReady = 'true';
            if (!modal.hasAttribute('aria-modal')) modal.setAttribute('aria-modal', 'true');
            if (!modal.hasAttribute('role')) modal.setAttribute('role', 'dialog');
        }

        function openModalA11y(modal) {
            if (!modal || modal === lastOpenModal) return;
            prepareModal(modal);
            previousFocus = document.activeElement;
            lastOpenModal = modal;
            window.setTimeout(function () {
                var first = focusableElements(modal)[0];
                if (first) first.focus({ preventScroll: true });
            }, 30);
        }

        function closeModalA11y(modal) {
            if (!modal || modal !== lastOpenModal) return;
            lastOpenModal = null;
            if (previousFocus && document.contains(previousFocus)) previousFocus.focus({ preventScroll: true });
            previousFocus = null;
        }

        document.querySelectorAll('.booking-modal, [role="dialog"]').forEach(prepareModal);
        var observer = new MutationObserver(function (mutations) {
            mutations.forEach(function (mutation) {
                var modal = mutation.target;
                if (!(modal instanceof HTMLElement)) return;
                if (!modal.matches('.booking-modal, [role="dialog"]')) return;
                var visible = !modal.hidden && (modal.classList.contains('is-open') || modal.matches('[role="dialog"]'));
                if (visible) openModalA11y(modal);
                else closeModalA11y(modal);
            });
        });
        document.querySelectorAll('.booking-modal, [role="dialog"]').forEach(function (modal) {
            observer.observe(modal, { attributes: true, attributeFilter: ['hidden', 'class'] });
        });

        document.addEventListener('keydown', function (event) {
            var modal = activeModal();
            if (!modal || event.key !== 'Tab') return;
            var items = focusableElements(modal);
            if (!items.length) return;
            var first = items[0];
            var last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });

        document.querySelectorAll('.app-nav-link').forEach(function (link) {
            try {
                var url = new URL(link.getAttribute('href'), window.location.origin);
                var current = window.location.pathname === url.pathname || (url.pathname === '/' && window.location.pathname === '/admin');
                if (current) link.setAttribute('aria-current', 'page');
            } catch (error) {}
        });

        (function openRequestedProfilePanel() {
            var params = new URLSearchParams(window.location.search);
            var requested = params.get('open') || (window.location.hash ? window.location.hash.slice(1) : '');
            if (!requested) return;
            var panel = document.getElementById(requested);
            if (!panel || !panel.matches('details.profile-box')) return;
            panel.open = true;
            window.setTimeout(function () {
                panel.scrollIntoView({
                    behavior: prefersReducedMotion ? 'auto' : 'smooth',
                    block: 'start'
                });
                panel.classList.add('is-focus-pulse');
                window.setTimeout(function () { panel.classList.remove('is-focus-pulse'); }, 1400);
            }, 120);
        })();

        document.querySelectorAll('.flash').forEach(function (flash) {
            var isError = flash.textContent.trim().toLowerCase().startsWith('errore');
            flash.classList.add('flash-toast');
            flash.setAttribute('role', isError ? 'alert' : 'status');
            flash.setAttribute('aria-live', isError ? 'assertive' : 'polite');
            flash.setAttribute('tabindex', '0');
            function dismiss() {
                flash.classList.add('is-hiding');
                window.setTimeout(function () { flash.remove(); }, 260);
            }
            flash.addEventListener('click', dismiss);
            flash.addEventListener('keydown', function (event) {
                if (event.key === 'Escape' || event.key === 'Enter') dismiss();
            });
            window.setTimeout(dismiss, 5200);
        });

        document.addEventListener('pointerdown', function (event) {
            var target = event.target.closest('button, .button, .filter-chip, .date-nav-btn, .booking-slot-card, .patient-card, .service-type-tab span');
            if (!target || target.disabled || target.classList.contains('calendar-icon-button')) return;
            var rect = target.getBoundingClientRect();
            var ripple = document.createElement('span');
            ripple.className = 'press-ripple';
            var size = Math.max(rect.width, rect.height);
            ripple.style.width = ripple.style.height = size + 'px';
            ripple.style.left = (event.clientX - rect.left - size / 2) + 'px';
            ripple.style.top = (event.clientY - rect.top - size / 2) + 'px';
            target.appendChild(ripple);
            window.setTimeout(function () { ripple.remove(); }, 520);
        }, { passive: true });

        document.querySelectorAll('[data-auto-submit]').forEach(function (input) {
            if (input.closest('[data-history-toolbar]')) return;
            input.addEventListener('change', function () {
                if (input.form) input.form.submit();
            });
        });

        document.querySelectorAll('[data-history-toolbar]').forEach(function (toolbar) {
            var scope = toolbar.closest('.profile-box-body') || toolbar.parentElement || document;
            var rows = Array.prototype.slice.call(scope.querySelectorAll('[data-history-row]'));
            var empty = scope.querySelector('[data-history-empty]');
            var dateInput = toolbar.querySelector('[data-history-date]');
            var dateButton = toolbar.querySelector('.date-filter-button');
            var reset = toolbar.querySelector('[data-history-reset]');
            var selectedStatus = (toolbar.querySelector('[data-history-status].active') || toolbar.querySelector('[data-history-status]') || {}).dataset;
            var currentStatus = selectedStatus ? selectedStatus.historyStatus : 'prenotata';

            function formatDateLabel(value) {
                if (!value) return 'Seleziona data';
                var parts = value.split('-');
                if (parts.length !== 3) return value;
                return parts[2] + '/' + parts[1] + '/' + parts[0];
            }

            function applyFilters(status, date) {
                currentStatus = status || currentStatus || 'prenotata';
                var visible = 0;
                rows.forEach(function (row) {
                    var matchesStatus = row.dataset.status === currentStatus;
                    var matchesDate = !date || row.dataset.date === date;
                    var show = matchesStatus && matchesDate;
                    row.hidden = !show;
                    if (show) visible += 1;
                });
                toolbar.querySelectorAll('[data-history-status]').forEach(function (chip) {
                    var active = chip.dataset.historyStatus === currentStatus;
                    chip.classList.toggle('active', active);
                    chip.setAttribute('aria-pressed', active ? 'true' : 'false');
                });
                if (empty) empty.hidden = visible !== 0;
                if (dateButton) dateButton.textContent = formatDateLabel(date || '');
                if (reset) reset.hidden = !date;
            }

            toolbar.querySelectorAll('[data-history-status]').forEach(function (chip) {
                chip.addEventListener('click', function (event) {
                    event.preventDefault();
                    applyFilters(chip.dataset.historyStatus, dateInput ? dateInput.value : '');
                });
            });
            if (dateInput) {
                dateInput.addEventListener('change', function () {
                    applyFilters(currentStatus, dateInput.value);
                });
            }
            if (reset) {
                reset.addEventListener('click', function (event) {
                    event.preventDefault();
                    if (dateInput) dateInput.value = '';
                    applyFilters(currentStatus, '');
                });
            }
            applyFilters(currentStatus, dateInput ? dateInput.value : '');
        });

        document.querySelectorAll('.booking-date-nav').forEach(function (nav) {
            var startX = 0;
            var startY = 0;
            var tracking = false;
            var input = nav.querySelector('input[type="date"]');
            if (!input) return;

            function addDays(days) {
                var current = new Date(input.value + 'T00:00:00');
                var min = new Date((input.min || input.value) + 'T00:00:00');
                current.setDate(current.getDate() + days);
                if (current < min) current = min;
                var next = current.toISOString().slice(0, 10);
                var url = new URL(window.location.href);
                url.searchParams.set('date', next);
                window.location.href = url.pathname + '?' + url.searchParams.toString();
            }

            nav.addEventListener('touchstart', function (event) {
                if (!event.touches || event.touches.length !== 1) return;
                tracking = true;
                startX = event.touches[0].clientX;
                startY = event.touches[0].clientY;
            }, { passive: true });

            nav.addEventListener('touchend', function (event) {
                if (!tracking || !event.changedTouches || event.changedTouches.length !== 1) return;
                tracking = false;
                var dx = event.changedTouches[0].clientX - startX;
                var dy = event.changedTouches[0].clientY - startY;
                if (Math.abs(dx) < 54 || Math.abs(dx) < Math.abs(dy) * 1.4) return;
                addDays(dx < 0 ? 1 : -1);
            }, { passive: true });
        });

        document.querySelectorAll('[data-debts-modal]').forEach(function (modal) {
            if (modal.parentElement !== document.body) document.body.appendChild(modal);
            var openers = document.querySelectorAll('[data-open-debts]');
            var closers = modal.querySelectorAll('[data-close-debts]');
            function openDebts() {
                modal.hidden = false;
                modal.classList.add('is-open');
                document.body.classList.add('modal-open');
            }
            function closeDebts() {
                modal.classList.remove('is-open');
                modal.hidden = true;
                document.body.classList.remove('modal-open');
            }
            openers.forEach(function (button) { button.addEventListener('click', openDebts); });
            closers.forEach(function (button) { button.addEventListener('click', closeDebts); });
            document.addEventListener('keydown', function (event) {
                if (event.key === 'Escape' && !modal.hidden) closeDebts();
            });
        });


        function formatCountdown(targetDate) {
            var diff = targetDate.getTime() - Date.now();
            if (!Number.isFinite(diff) || diff <= 0) return 'Ora';
            var totalMinutes = Math.max(Math.floor(diff / 60000), 0);
            var days = Math.floor(totalMinutes / 1440);
            var hours = Math.floor((totalMinutes % 1440) / 60);
            var minutes = totalMinutes % 60;
            if (days > 0) return days + 'g ' + hours + 'h';
            return hours + 'h ' + minutes + 'm';
        }

        var countdowns = Array.prototype.slice.call(document.querySelectorAll('[data-countdown-target]'));
        if (countdowns.length) {
            function updateCountdowns() {
                countdowns.forEach(function (node) {
                    var target = new Date(node.dataset.countdownTarget || '');
                    node.textContent = formatCountdown(target);
                });
            }
            updateCountdowns();
            window.setInterval(updateCountdowns, 30000);
        }
        document.querySelectorAll('form').forEach(function (form) {
            form.addEventListener('submit', function (event) {
                if (event.defaultPrevented || form.dataset.noLoading === 'true' || form.matches('[data-login-form]')) return;
                var submitter = event.submitter || form.querySelector('button[type="submit"], button:not([type]), .button');
                if (!submitter || submitter.disabled) return;
                if (!submitter.dataset.originalText) submitter.dataset.originalText = submitter.textContent.trim();
                submitter.classList.add('is-submitting');
                submitter.setAttribute('aria-busy', 'true');
                window.setTimeout(function () {
                    submitter.disabled = true;
                    form.querySelectorAll('button').forEach(function (button) {
                        if (button !== submitter) button.disabled = true;
                    });
                }, 0);
            });
        });
    });
})();




