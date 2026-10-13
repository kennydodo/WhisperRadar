// After a button on a list page (dashboard, watched channels) posts and the
// page comes back, return to the row the user was on instead of the top.
(function () {
  var KEY = 'wr-scroll:' + location.pathname;
  function save() {
    try { sessionStorage.setItem(KEY, String(window.scrollY || 0)); } catch (e) {}
  }
  document.addEventListener('submit', function (ev) {
    var f = ev.target;
    if (f && f.tagName === 'FORM' && (f.method || '').toLowerCase() === 'post') save();
  }, true);
  var y = null;
  try { y = sessionStorage.getItem(KEY); sessionStorage.removeItem(KEY); } catch (e) {}
  if (y !== null && !location.hash) {
    var target = parseInt(y, 10) || 0;
    var go = function () { window.scrollTo(0, target); };
    go();
    window.addEventListener('load', go);
    setTimeout(go, 150);
  }
})();
