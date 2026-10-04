/* Character counters for the visual style.
 *
 * Flow takes the style and the image prompt in ONE box (the style is put in
 * front of every prompt), so what the style uses is what the prompt loses:
 *   room for a prompt = limit - style - 1
 * Most prompts are well under 500 characters, so the counter turns red once a
 * 500-character prompt would no longer fit next to the style.
 */
(function () {
  var TYPICAL_PROMPT = 500;
  function n(v) { return Number(v).toLocaleString('en-US'); }

  // channel / production style text -> "1,710 chars - 709 left for a prompt"
  window.wrStyleCount = function (textarea, out, limit) {
    function update() {
      var len = textarea.value.trim().length;
      if (!len) { out.textContent = ''; return; }
      var left = limit - len - 1;
      var bad = left < TYPICAL_PROMPT;
      out.textContent = n(len) + ' chars - ' + (left >= 0 ? n(left) : '0') +
        ' left for each image prompt (Flow takes ' + n(limit) + ' for style + prompt)' +
        (left < 0 ? ' - the style alone is over the limit and cannot be attached'
         : bad ? ' - a ' + TYPICAL_PROMPT + '-character prompt will not fit; shorten the style' : '');
      out.style.color = bad ? 'var(--red)' : 'var(--muted)';
      out.style.fontWeight = bad ? '600' : '400';
    }
    textarea.addEventListener('input', update);
    update();
  };

  // shotlist.json editor -> the style the Flow job will send vs the prompts
  window.wrShotlistCount = function (textarea, out, limit) {
    function update() {
      var d;
      try { d = JSON.parse(textarea.value); } catch (e) { out.textContent = ''; return; }
      if (!d || typeof d !== 'object') { out.textContent = ''; return; }
      var style = String(d.style || '').trim().length;
      var longest = 0, over = 0, count = 0;
      (d.images || []).forEach(function (i) {
        var l = String((i && i.prompt) || '').length;
        if (!l) return;
        count++;
        if (l > longest) longest = l;
        if (style && l + style + 1 > limit) over++;
      });
      if (!style && !count) { out.textContent = ''; return; }
      var total = style + longest + (style ? 1 : 0);
      var bad = style && over > 0;
      out.textContent = 'Style ' + n(style) + ' chars + longest prompt ' + n(longest) +
        ' = ' + n(total) + ' of ' + n(limit) + ' (' + n(Math.max(0, limit - total)) + ' free)' +
        (bad ? ' - ' + over + ' prompt(s) too long: the style will NOT be attached to the Flow images'
         : style ? ' - fits' : ' - no style in this shotlist');
      out.style.color = bad ? 'var(--red)' : 'var(--muted)';
      out.style.fontWeight = bad ? '600' : '400';
    }
    textarea.addEventListener('input', update);
    update();
  };
})();
