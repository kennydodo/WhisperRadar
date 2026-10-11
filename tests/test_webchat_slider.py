"""The effort slider (ChatGPT) is read and set with the keyboard.
Runs against small local pages; skipped when no Chromium is available."""
import os
import unittest

from whisperradar import webchat

PAGE = """<!doctype html><body>
<button id=pill>Thinking effort</button>
<div id=pop style="display:none"><div id=head>Instant</div>
<div role=slider tabindex=0 aria-valuemin=0 aria-valuemax=2 aria-valuenow=0 id=sl style="width:200px;height:20px;background:#ccc"></div></div>
<script>
const names=['Instant','Thinking','Pro'];
const pill=document.getElementById('pill'), pop=document.getElementById('pop'),
      sl=document.getElementById('sl'), head=document.getElementById('head');
let v=0;
pill.onclick=()=>{pop.style.display=pop.style.display==='none'?'block':'none'};
sl.onkeydown=e=>{ if(e.key==='Home')v=0; if(e.key==='ArrowRight')v=Math.min(2,v+1);
  sl.setAttribute('aria-valuenow',v); head.textContent=names[v]; };
document.onkeydown=e=>{ if(e.key==='Escape')pop.style.display='none' };
</script></body>"""


def _browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    exe = "/opt/pw-browsers/chromium"
    kw = {"executable_path": exe} if os.path.exists(exe) else {}
    return pw, pw.chromium.launch(**kw)


class Slider(unittest.TestCase):
    def setUp(self):
        try:
            self.pw, self.browser = _browser()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"no browser: {exc}")
        self.page = self.browser.new_page()
        self.page.set_content(PAGE)

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def test_read_stops(self):
        res = webchat.read_slider(self.page)
        self.assertEqual(res["stops"], ["Instant", "Thinking", "Pro"])
        self.assertEqual(res["current"], "Instant")

    def test_set_stop(self):
        self.assertEqual(webchat.set_slider(self.page, 2, "Pro"), "Pro")
        self.assertEqual(webchat.set_slider(self.page, 1, "Thinking"), "Thinking")

    def test_no_button_is_reported(self):
        self.page.set_content("<body>nothing</body>")
        res = webchat.read_slider(self.page)
        self.assertEqual(res["stops"], [])
        self.assertTrue(res["why"])

    def test_sanitize_keeps_slider_index(self):
        defs, _ = webchat.sanitize_sites([{
            "name": "X", "url": "https://x.example",
            "levels": [{"label": "Instant", "slider": 0},
                       {"label": "Pro", "slider": 2}]}])
        self.assertEqual([l.get("slider") for l in defs[0]["levels"]], [0, 2])


if __name__ == "__main__":
    unittest.main()
