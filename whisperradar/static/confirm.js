/* A centered, in-page replacement for window.confirm().
 *
 * The native confirm() dialog is drawn by the browser itself, anchored to
 * the top of the window ("127.0.0.1 says...") - there is no way to move or
 * restyle it from a page. wrConfirm() shows an actual DOM modal, centered
 * over the page, and returns a Promise<boolean> instead of blocking.
 *
 * Two helpers cover the two call patterns used across the templates:
 *   - wrConfirmSubmit(form, message): drop-in for
 *     onsubmit="return confirm('...')" -> onsubmit="return wrConfirmSubmit(this, '...')"
 *   - wrConfirmClick(button, message): drop-in for
 *     onclick="return confirm('...')" -> onclick="return wrConfirmClick(this, '...')"
 * Both always return false immediately (blocking the native, synchronous
 * submit/click) and resubmit the form themselves once the user answers.
 */
(function () {
  function ensureModal() {
    if (document.getElementById('wr-confirm-overlay')) return;
    var overlay = document.createElement('div');
    overlay.id = 'wr-confirm-overlay';
    overlay.style.cssText =
      'position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;' +
      'align-items:center;justify-content:center;z-index:9999;';
    var box = document.createElement('div');
    box.id = 'wr-confirm-box';
    box.style.cssText =
      'background:#20242c;color:#e8e8e8;max-width:440px;width:90%;' +
      'padding:20px 22px;border-radius:10px;' +
      'box-shadow:0 12px 40px rgba(0,0,0,.55);' +
      'font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;';
    box.innerHTML =
      '<div id="wr-confirm-msg" style="white-space:pre-wrap;margin-bottom:18px"></div>' +
      '<div style="display:flex;justify-content:flex-end;gap:8px">' +
      '<button type="button" id="wr-confirm-cancel" ' +
      'style="padding:7px 16px;border-radius:6px;border:1px solid #555;' +
      'background:#2a2f3a;color:#e8e8e8;cursor:pointer">Cancel</button>' +
      '<button type="button" id="wr-confirm-ok" ' +
      'style="padding:7px 16px;border-radius:6px;border:none;' +
      'background:#3b82f6;color:#fff;cursor:pointer">OK</button>' +
      '</div>';
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  }

  window.wrConfirm = function (message) {
    ensureModal();
    var overlay = document.getElementById('wr-confirm-overlay');
    var msg = document.getElementById('wr-confirm-msg');
    var okBtn = document.getElementById('wr-confirm-ok');
    var cancelBtn = document.getElementById('wr-confirm-cancel');
    msg.textContent = message;
    overlay.style.display = 'flex';
    return new Promise(function (resolve) {
      function cleanup(result) {
        overlay.style.display = 'none';
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        overlay.removeEventListener('mousedown', onBackdrop);
        document.removeEventListener('keydown', onKey);
        resolve(result);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onBackdrop(e) { if (e.target === overlay) cleanup(false); }
      function onKey(e) {
        if (e.key === 'Escape') cleanup(false);
        else if (e.key === 'Enter') cleanup(true);
      }
      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      overlay.addEventListener('mousedown', onBackdrop);
      document.addEventListener('keydown', onKey);
      okBtn.focus();
    });
  };

  window.wrConfirmSubmit = function (form, message) {
    if (form.dataset.wrConfirmed === '1') {
      delete form.dataset.wrConfirmed;
      return true; // second pass, after the user already said OK
    }
    wrConfirm(message).then(function (ok) {
      if (!ok) return;
      form.dataset.wrConfirmed = '1';
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    });
    return false; // block the native, synchronous submit
  };

  window.wrConfirmClick = function (button, message) {
    wrConfirm(message).then(function (ok) {
      if (!ok) return;
      var form = button.form || button.closest('form');
      if (!form) return;
      if (form.requestSubmit) form.requestSubmit(button); else form.submit();
    });
    return false;
  };
})();
