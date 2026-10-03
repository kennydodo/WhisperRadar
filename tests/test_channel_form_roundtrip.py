"""Channel settings form: what the browser submits is what gets saved.

The test renders /my-channels, reads the channel edit form the way a browser
does (unticked checkboxes and disabled controls are not sent), changes a
checkbox, posts it back and checks the database. It guards the watched-channels
checkboxes in Basics, and that a rejected save is never silent.
"""
import json
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser

from werkzeug.datastructures import MultiDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class _Form(HTMLParser):
    """Collect the controls of the edit form whose hidden id is `oc_id`."""

    def __init__(self, oc_id):
        super().__init__()
        self.oc_id = str(oc_id)
        self.forms = []          # [[(name, kind, value, checked, disabled)...]]
        self._cur = None
        self._select = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = [] if a.get("action") == "/my-channels/edit" else None
            if self._cur is not None:
                self.forms.append(self._cur)
        elif self._cur is None:
            return
        elif tag == "input":
            kind = (a.get("type") or "text").lower()
            if a.get("name") and kind not in ("submit", "button", "file"):
                self._cur.append([a["name"], kind, a.get("value", "on")
                                  if kind in ("checkbox", "radio")
                                  else a.get("value", ""),
                                  "checked" in a, "disabled" in a])
        elif tag == "select" and a.get("name"):
            self._select = [a["name"], "select", "", True, "disabled" in a]
            self._cur.append(self._select)
            self._first = None
        elif tag == "option" and self._select is not None:
            val = a.get("value", "")
            if self._first is None:
                self._first = val
                self._select[2] = val
            if "selected" in a:
                self._select[2] = val
        elif tag == "textarea" and a.get("name"):
            self._textarea = [a["name"], "textarea", "", True, "disabled" in a]
            self._cur.append(self._textarea)

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[2] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def _submission(html, oc_id, tick=(), untick=()):
    """The (name, value) pairs a browser would post for channel `oc_id`."""
    parser = _Form(oc_id)
    parser.feed(html)
    for controls in parser.forms:
        ident = [c for c in controls if c[0] == "id"]
        if ident and ident[0][2] == str(oc_id):
            break
    else:
        raise AssertionError(f"no edit form for channel {oc_id}")
    pairs = []
    for name, kind, value, checked, disabled in controls:
        if disabled:
            continue
        if kind == "checkbox":
            if name == "watched":
                checked = ((checked or value in tick) and value not in untick)
            if not checked:
                continue
        pairs.append((name, value))
    return pairs


class ChannelFormRoundTrip(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        for name, cid in (("Alpha", "UCalpha"), ("Beta", "UCbeta"),
                          ("Gamma", "UCgamma")):
            db.add_channel(conn, name, cid)
        self.plain = db.create_own_channel(conn, "Plain")
        self.custom = db.create_own_channel(conn, "Custom")
        db.update_own_channel(
            conn, self.custom, brief_motion="custom",
            brief_custom=json.dumps({
                "allowed": ["ST", "ZI", "ZO", "PL", "PR"],
                "st_max_share": 0.15, "pan_max_share": 0.1}))
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def _watched(self, oc_id):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            return db.own_channel_watched(db.get_own_channel(conn, oc_id))
        finally:
            conn.close()

    def _page(self):
        resp = self.client.get("/my-channels")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _save(self, oc_id, tick=(), untick=()):
        pairs = _submission(self._page(), oc_id, tick, untick)
        return self.client.post("/my-channels/edit", data=MultiDict(pairs))

    def test_ticked_watched_channels_are_saved(self):
        for oc in (self.plain, self.custom):
            resp = self._save(oc, tick=("UCbeta",))
            self.assertEqual(resp.status_code, 302)
            self.assertIn("msg=", resp.headers["Location"], resp.headers)
            self.assertEqual(self._watched(oc), ["UCbeta"])

    def test_two_ticks_then_one_removed(self):
        self._save(self.custom, tick=("UCalpha", "UCgamma"))
        self.assertEqual(sorted(self._watched(self.custom)),
                         ["UCalpha", "UCgamma"])
        self._save(self.custom, untick=("UCalpha",))
        self.assertEqual(self._watched(self.custom), ["UCgamma"])

    def test_untick_all_means_all_watched_channels(self):
        self._save(self.plain, tick=("UCalpha",))
        self._save(self.plain, untick=("UCalpha",))
        self.assertEqual(self._watched(self.plain), [])

    def test_saved_ticks_come_back_ticked_and_survive_a_resave(self):
        self._save(self.custom, tick=("UCbeta",))
        again = _submission(self._page(), self.custom)
        self.assertIn(("watched", "UCbeta"), again)
        self.client.post("/my-channels/edit", data=MultiDict(again))
        self.assertEqual(self._watched(self.custom), ["UCbeta"])

    def test_saving_one_channel_leaves_the_others_alone(self):
        self._save(self.plain, tick=("UCalpha",))
        self._save(self.custom, tick=("UCbeta",))
        self.assertEqual(self._watched(self.plain), ["UCalpha"])

    def test_a_rejected_save_says_so_and_keeps_the_old_value(self):
        self._save(self.custom, tick=("UCalpha",))
        pairs = [p for p in _submission(self._page(), self.custom,
                                        tick=("UCbeta",))
                 if p[0] != "custom_pan_share"]
        pairs.append(("custom_pan_share", ""))
        resp = self.client.post("/my-channels/edit", data=MultiDict(pairs))
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["Location"]
        self.assertIn("error=", loc)
        self.assertEqual(self._watched(self.custom), ["UCalpha"])
        page = self.client.get(loc.split("#")[0]).get_data(as_text=True)
        self.assertRegex(page, re.compile(r"max share", re.I))


if __name__ == "__main__":
    unittest.main()
