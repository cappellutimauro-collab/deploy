(function () {
    function qs(root, selector) { return root.querySelector(selector); }
    function qsa(root, selector) { return Array.prototype.slice.call(root.querySelectorAll(selector)); }

    qsa(document, '[data-role-choice]').forEach(function (choice) {
        var form = choice.closest('.auth') ? choice.closest('.auth').querySelector('[data-doctor-register-form]') : document.querySelector('[data-doctor-register-form]');
        if (!form) return;
        var accountType = qs(form, '[data-account-type]');
        var fields = qs(form, '[data-doctor-registration-fields]');
        function setRole(role) {
            if (accountType) accountType.value = role === 'doctor' ? 'doctor' : 'patient';
            qsa(choice, '[data-register-role]').forEach(function (button) {
                button.classList.toggle('active', button.dataset.registerRole === role);
            });
            if (fields) fields.hidden = role !== 'doctor';
        }
        qsa(choice, '[data-register-role]').forEach(function (button) {
            button.addEventListener('click', function () { setRole(button.dataset.registerRole || 'patient'); });
        });
        setRole(accountType && accountType.value === 'doctor' ? 'doctor' : 'patient');
    });

    qsa(document, '[data-doctor-photo-uploader]').forEach(function (uploader) {
        var input = qs(uploader, '[data-doctor-photo-file]');
        var hidden = qs(uploader, '[data-doctor-photo-data]');
        var preview = qs(uploader, '[data-doctor-photo-preview]');
        var fileName = qs(uploader, '[data-doctor-photo-file-name]');
        var clearButton = qs(uploader, '[data-doctor-photo-clear]');
        var initialPreview = preview ? (preview.style.getPropertyValue('--preview-image') || preview.style.backgroundImage || '') : '';
        if (!input || !hidden) return;
        function cssUrl(value) {
            if (!value) return '';
            if (/^url\(/i.test(value)) return value;
            return 'url("' + String(value).replace(/["\\]/g, '\\$&') + '")';
        }
        function setPreviewImage(value) {
            if (!preview) return;
            var imageValue = cssUrl(value);
            if (!imageValue) {
                preview.style.removeProperty('--preview-image');
                preview.style.removeProperty('background-image');
                return;
            }
            preview.style.setProperty('--preview-image', imageValue);
            preview.style.setProperty('background-image', imageValue, 'important');
            preview.style.setProperty('background-position', 'center', 'important');
            preview.style.setProperty('background-repeat', 'no-repeat', 'important');
        }
        function setFileName(text) {
            if (fileName) fileName.textContent = text || 'Nessun file selezionato';
        }
        function setClearVisible(visible) {
            if (clearButton) clearButton.hidden = !visible;
        }
        function resetPreview(message) {
            hidden.value = '';
            input.value = '';
            if (preview) {
                preview.classList.toggle('has-image', Boolean(initialPreview));
                setPreviewImage(initialPreview);
                preview.textContent = message || '';
            }
            setFileName('Nessun logo caricato');
            setClearVisible(false);
        }
        input.addEventListener('change', function () {
            var file = input.files && input.files[0];
            hidden.value = '';
            if (!file) {
                resetPreview('');
                return;
            }
            if (!/^image\/(png|jpeg|webp)$/.test(file.type) || file.size > 2500000) {
                if (preview) {
                    preview.classList.remove('has-image');
                    setPreviewImage('');
                    preview.textContent = 'Immagine non valida';
                }
                setFileName('Formato non valido');
                setClearVisible(false);
                input.value = '';
                return;
            }
            var reader = new FileReader();
            reader.onload = function () {
                hidden.value = String(reader.result || '');
                if (preview) {
                    preview.textContent = '';
                    preview.classList.add('has-image');
                    setPreviewImage(hidden.value);
                }
                setFileName(file.name);
                setClearVisible(true);
            };
            reader.readAsDataURL(file);
        });
        if (clearButton) {
            clearButton.addEventListener('click', function () {
                resetPreview('');
            });
        }
    });
})();
