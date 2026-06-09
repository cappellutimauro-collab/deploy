(function () {
    function openDatePicker(input) {
        if (!input) return;
        input.focus({ preventScroll: true });
        if (typeof input.showPicker === "function") {
            try {
                input.showPicker();
                return;
            } catch (error) {
                // Some browsers only allow showPicker from a direct user gesture.
            }
        }
        input.click();
    }

    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-date-picker-button]");
        var trigger = button || event.target.closest("[data-date-picker-trigger]");
        if (!trigger) return;
        if (event.target.closest("a") && !button) return;
        if (event.target.matches("input[type='date']")) return;
        var scope = trigger.closest("[data-date-picker-trigger]") || trigger;
        var input = scope.querySelector("input[type='date']");
        if (!input) return;
        event.preventDefault();
        openDatePicker(input);
    });
})();
