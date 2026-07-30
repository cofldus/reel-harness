// Disables a form's submit button on submit -- prevents a double-click from
// firing two requests. The server-side idempotency_key (see job_new.html)
// is the real dedup guarantee; this is just UX polish so a slow request
// doesn't invite a second click.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!form.hasAttribute("data-disable-on-submit")) return;
  var button = form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = true;
    button.dataset.originalText = button.textContent;
    button.textContent = "처리 중...";
  }
});
