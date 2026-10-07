"""webchat.ask against a fake page (no browser, no network).

Run: python -m unittest tests.test_webchat
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import webchat as wc  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakePage:
    """A scripted chat page. `script` is a list of (reply_text, generating)
    frames, one consumed per poll; `continue_after` makes a Continue button
    appear once the Nth frame is done."""

    def __init__(self, clock, site, script, url="https://chat.z.ai/",
                 has_box=True, paste_keeps=None, bot=False,
                 continue_frames=None, send_ok=True, body_extra=""):
        self.clock, self.site = clock, site
        self.script = list(script)
        self.url = url
        self.has_box, self.paste_keeps, self.bot = has_box, paste_keeps, bot
        self.continue_frames = list(continue_frames or [])
        self.send_ok = send_ok
        self.body_extra = body_extra
        self.sent = False
        self.pasted = None
        self.files = None
        self.clicked_continue = 0
        self.frame = ("", False)
        self.mouse = self
        self.prepared = False

    # Playwright-ish surface
    def goto(self, url):
        self.visited = url

    def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000.0
        if self.sent and self.script:
            self.frame = self.script.pop(0)
        elif self.sent and self.continue_frames and self.frame[1] is False:
            pass

    def click(self, x, y):
        self.prepared = True

    def set_input_files(self, sel, paths):
        self.files = list(paths)

    def evaluate(self, js, arg=None):
        if js is wc._BOT_JS:
            return self.bot
        if js is wc._PASTE_JS:
            self.pasted = arg["text"]
            return (self.paste_keeps if self.paste_keeps is not None
                    else len(arg["text"]))
        if js is wc._CAP_GET_JS:
            return getattr(self, "cap", None)
        if js is wc._TA_LEN_JS:
            return 0 if self.sent else len(self.pasted or "")
        if js is wc._PAGE_TAIL_JS:
            return self.body_extra
        if js is wc._STATE_JS:
            text, _g = self.frame
            return {"count": 1 if (self.sent and text) else 0, "text": text,
                    "url": self.url}
        if js is wc._CONTINUE_JS:
            if self.continue_frames and self.frame[0]:
                self.clicked_continue += 1
                self.script = self.continue_frames.pop(0)
                self.frame = ("", True)
                return "continue generating"
            return ""
        if js is self.site.send_js:
            if self.send_ok:
                self.sent = True
            return self.send_ok
        if js is self.site.generating_js:
            return self.frame[1]
        if isinstance(js, str) and "Deep Think" in js:
            return True                      # z.ai level menu opened
        if isinstance(js, str) and "querySelector(s)" in js:
            return self.has_box
        if isinstance(js, str) and "document.body.innerText" in js:
            return ("attached " + " ".join(
                Path(f).name for f in (self.files or []))) + self.body_extra
        return None


def run(site, script, **kw):
    clk = Clock()
    fk = {k: kw.pop(k) for k in list(kw) if k in (
        "url", "has_box", "paste_keeps", "bot", "continue_frames",
        "send_ok")}
    page = FakePage(clk, site, script, **fk)
    out = wc.ask(page, site, kw.pop("prompt", "hello"), clock=clk, **kw)
    return out, page


class AskTests(unittest.TestCase):
    def test_returns_the_finished_reply(self):
        out, page = run(wc.ZAI, [("Hel", True), ("Hello wor", True),
                                 ("Hello world", False)])
        self.assertEqual(out, "Hello world")
        self.assertEqual(page.pasted, "hello")

    def test_waits_for_text_to_settle_even_if_not_flagged_generating(self):
        # DeepSeek has no reliable "generating" flag: text must stop changing
        out, _ = run(wc.DEEPSEEK, [("a", False), ("ab", False),
                                   ("abc", False)] + [("abc", False)] * 6)
        self.assertEqual(out, "abc")

    def test_does_not_return_while_still_generating(self):
        out, _ = run(wc.ZAI, [("part", True)] * 5 + [("part two", False)])
        self.assertEqual(out, "part two")

    def test_clicks_continue_and_returns_the_longer_reply(self):
        out, page = run(
            wc.DEEPSEEK, [("first half", False)] * 8,
            continue_frames=[[("first half second half", False)] * 8])
        self.assertEqual(page.clicked_continue, 1)
        self.assertEqual(out, "first half second half")

    def test_continue_is_capped(self):
        clk = Clock()
        page = FakePage(clk, wc.DEEPSEEK, [("x", False)] * 8,
                        continue_frames=[[("x", False)] * 8] * 20)
        out = wc.ask(page, wc.DEEPSEEK, "p", clock=clk, continue_max=3)
        self.assertEqual(page.clicked_continue, 3)
        self.assertEqual(out, "x")

    def test_attaches_files_and_waits_for_the_chip(self):
        _out, page = run(wc.ZAI, [("ok", False)] * 8,
                         files=["/tmp/narration.txt", "/tmp/shotlist.txt"])
        self.assertEqual(page.files, ["/tmp/narration.txt",
                                      "/tmp/shotlist.txt"])

    def test_missing_attachment_chip_fails(self):
        clk = Clock()

        class NoChip(FakePage):
            def evaluate(self, js, arg=None):
                if isinstance(js, str) and "document.body.innerText" in js:
                    return "nothing here"
                return super().evaluate(js, arg)
        page = NoChip(clk, wc.ZAI, [("ok", False)] * 8)
        with self.assertRaises(wc.WebChatError):
            wc.ask(page, wc.ZAI, "p", ["/tmp/a.txt"], clock=clk)

    def test_login_page_means_sign_in(self):
        with self.assertRaises(wc.NeedsSignIn):
            run(wc.DEEPSEEK, [], url="https://chat.deepseek.com/sign_in")

    def test_no_chat_box_means_sign_in(self):
        with self.assertRaises(wc.NeedsSignIn):
            run(wc.ZAI, [], has_box=False)

    def test_bot_check_stops_and_is_never_solved(self):
        with self.assertRaises(wc.BotCheck):
            run(wc.ZAI, [("x", False)], bot=True)

    def test_short_paste_is_an_error_not_a_silent_truncation(self):
        with self.assertRaises(wc.WebChatError) as cm:
            run(wc.ZAI, [], paste_keeps=3, prompt="long prompt here")
        self.assertIn("kept 3 of 16", str(cm.exception))

    def test_missing_send_button_is_an_error(self):
        with self.assertRaises(wc.WebChatError):
            run(wc.ZAI, [], send_ok=False)

    def test_never_started_times_out(self):
        with self.assertRaises(wc.WebChatTimeout):
            run(wc.ZAI, [("", False)] * 200, start_wait=10)

    def test_overall_timeout_keeps_the_partial_reply(self):
        with self.assertRaises(wc.WebChatTimeout) as cm:
            run(wc.ZAI, [("still going", True)] * 500, timeout=30)
        self.assertEqual(cm.exception.partial, "still going")

    def test_empty_prompt_rejected(self):
        with self.assertRaises(wc.WebChatError):
            run(wc.ZAI, [], prompt="  ")

    def test_zai_prepare_runs(self):
        _out, page = run(wc.ZAI, [("ok", False)] * 8)
        self.assertTrue(page.prepared)    # level menu closed by a click




class SlowAcceptPage(FakePage):
    """A big prompt: the box only empties 12 seconds after the first click."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.clicks, self.clicked_at = 0, None

    def evaluate(self, js, arg=None):
        if js is self.site.send_js:
            self.clicks += 1
            if self.clicked_at is None:
                self.clicked_at = self.clock.t
            self.sent = True
            return True
        if js is wc._TA_LEN_JS:
            if self.clock.t - (self.clicked_at or 0) >= 12:
                return 0
            return len(self.pasted or "x")
        return super().evaluate(js, arg)


class SendOnceTests(unittest.TestCase):
    def test_a_slow_to_accept_send_is_not_clicked_twice(self):
        # a second click on a send button that has become a stop button
        # would cancel the answer
        clk = Clock()
        page = SlowAcceptPage(clk, wc.DEEPSEEK, [("done", False)] * 12,
                              url="https://chat.deepseek.com/")
        out = wc.ask(page, wc.DEEPSEEK, "big prompt", clock=clk)
        self.assertEqual(out, "done")
        self.assertEqual(page.clicks, 1)


class TogglePage:
    """DeepSeek's two switches, as aria-pressed state."""

    def __init__(self, deepthink=False, search=True):
        self.state = {"DeepThink": deepthink, "Search": search}
        self.clicks = []

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        assert js is wc._DS_TOGGLE_JS
        label, want = arg["label"], arg["want"]
        if self.state[label] == want:
            return "ok"
        self.state[label] = want
        self.clicks.append(label)
        return "clicked"


class ModeTests(unittest.TestCase):
    def test_deepseek_defaults_are_deepthink_on_search_off(self):
        page = TogglePage(deepthink=False, search=True)
        wc.DEEPSEEK.prepare(page)
        self.assertEqual(page.state, {"DeepThink": True, "Search": False})
        self.assertEqual(sorted(page.clicks), ["DeepThink", "Search"])

    def test_deepseek_switches_already_right_are_left_alone(self):
        page = TogglePage(deepthink=True, search=False)
        wc.DEEPSEEK.prepare(page)
        self.assertEqual(page.clicks, [])

    def test_deepseek_options_can_turn_search_on_and_deepthink_off(self):
        page = TogglePage(deepthink=True, search=False)
        wc.DEEPSEEK.prepare(page, deepthink=False, search=True)
        self.assertEqual(page.state, {"DeepThink": False, "Search": True})

    def test_ask_hands_the_options_to_the_sites_prepare(self):
        seen = {}
        site = wc.Site(key="x", name="X", url="https://x/", box="textarea",
                       reply=".r", send_js="() => true",
                       generating_js="() => false",
                       prepare=lambda page, **kw: seen.update(kw))
        run(site, [("ok", False)] * 8, options={"thinking": "High"})
        self.assertEqual(seen, {"thinking": "High"})

class PasteToleranceTests(unittest.TestCase):
    def test_a_few_dropped_characters_are_tolerated(self):
        prompt = "x" * 58373
        out, page = run(wc.ZAI, [("ok", False)] * 8, prompt=prompt,
                        paste_keeps=58359)
        self.assertEqual(out, "ok")

    def test_crlf_is_compared_after_normalising(self):
        prompt = "a\r\nb\r\n" * 500
        out, _ = run(wc.ZAI, [("ok", False)] * 8, prompt=prompt,
                     paste_keeps=len(prompt) - 500)
        self.assertEqual(out, "ok")

    def test_a_real_truncation_still_fails(self):
        with self.assertRaises(wc.WebChatError):
            run(wc.ZAI, [("ok", False)] * 8, prompt="x" * 1000,
                paste_keeps=500)



def _sse(**d):
    import json as _j
    return "data: " + _j.dumps({"type": "chat:completion", "data": d}) + "\n\n"


class StreamTests(unittest.TestCase):
    def rec(self, *parts, done=True, url="/api/v2/chat/completions?x=1"):
        return [{"url": url, "done": done, "text": "".join(parts)}]

    def test_the_answer_phase_is_joined_and_thinking_is_left_out(self):
        page = FakePage(Clock(), wc.ZAI, [])
        page.cap = self.rec(_sse(delta_content="hmm", phase="thinking"),
                            _sse(delta_content='```json\n{"a":', phase="answer"),
                            _sse(delta_content=' 1}\n```', phase="answer"),
                            _sse(phase="done", done=True))
        self.assertEqual(wc.stream_text(page, wc.ZAI),
                         '```json\n{"a": 1}\n```')

    def test_a_chunk_split_across_reads_still_parses(self):
        line = _sse(delta_content="abc", phase="answer")
        page = FakePage(Clock(), wc.ZAI, [])
        page.cap = self.rec(line[:20], line[20:])
        self.assertEqual(wc.stream_text(page, wc.ZAI), "abc")

    def test_unfinished_missing_or_unrelated_streams_give_nothing(self):
        page = FakePage(Clock(), wc.ZAI, [])
        page.cap = self.rec(_sse(delta_content="x", phase="answer"),
                            done=False)
        self.assertEqual(wc.stream_text(page, wc.ZAI), "")
        page.cap = self.rec(_sse(delta_content="x", phase="answer"),
                            url="/api/other")
        self.assertEqual(wc.stream_text(page, wc.ZAI), "")
        page.cap = None
        self.assertEqual(wc.stream_text(page, wc.ZAI), "")
        self.assertEqual(wc.stream_text(page, wc.DEEPSEEK), "")

    def test_ask_prefers_the_stream_over_the_truncated_page_text(self):
        full = "FULL " * 400
        clk = Clock()
        page = FakePage(clk, wc.ZAI, [("tail only", False)] * 8)
        page.cap = self.rec(_sse(delta_content=full, phase="answer"))
        out = wc.ask(page, wc.ZAI, "hi", clock=clk)
        self.assertEqual(out, full.strip())

class StatusAndReadyTests(unittest.TestCase):
    def test_clean_reply_drops_status_lines(self):
        self.assertEqual(wc.clean_reply("Thought Process\n\nThe answer."),
                         "The answer.")
        self.assertEqual(wc.clean_reply("Thinking...\nHi"), "Hi")
        self.assertEqual(wc.clean_reply("Thinking..."), "")
        self.assertEqual(wc.clean_reply("A thought process\nis fine"),
                         "A thought process\nis fine")

    def test_a_thinking_placeholder_is_not_taken_as_the_answer(self):
        # the real z.ai failure: "Thinking..." sat unchanged long enough
        frames = [("Thinking...", False)] * 6 + [
            ("Thought Process\n\nThe real answer.", False)] * 8
        out, _ = run(wc.ZAI, frames)
        self.assertEqual(out, "The real answer.")

    def test_ready_keeps_waiting_until_the_reply_is_complete(self):
        frames = [("short", False)] * 8 + [("a complete long reply", False)] * 8
        out, _ = run(wc.ZAI, frames, ready=lambda t: len(t) > 10)
        self.assertEqual(out, "a complete long reply")

    def test_ready_gives_up_after_ready_wait_and_returns_what_there_is(self):
        frames = [("short", False)] * 400
        out, _ = run(wc.ZAI, frames, ready=lambda t: False, ready_wait=30)
        self.assertEqual(out, "short")

class ExtractJsonTests(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(wc.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_with_ui_chrome(self):
        t = 'json\nCopy\nDownload\n{"shots": [{"asset": "a"}]}\nAI-generated'
        self.assertEqual(wc.extract_json(t), {"shots": [{"asset": "a"}]})

    def test_picks_the_last_complete_object_after_template_echo(self):
        t = ('Reply with ONLY {"shots": [{"asset": "<asset>"}]} ... '
             '{"shots": [{"asset": "S01"}]}')
        self.assertEqual(wc.extract_json(t), {"shots": [{"asset": "S01"}]})

    def test_braces_inside_strings(self):
        self.assertEqual(wc.extract_json('{"a": "x } y"}'), {"a": "x } y"})

    def test_cut_off_json_is_none(self):
        self.assertIsNone(wc.extract_json('{"shots": [{"asset": "a"'))

    def test_no_json(self):
        self.assertIsNone(wc.extract_json("sorry, I can't"))


class SitesTests(unittest.TestCase):
    def test_known_sites(self):
        self.assertEqual(set(wc.SITES), {"zai", "deepseek"})

    def test_unknown_site_is_a_clear_error(self):
        chat = wc.WebChat("/tmp/none")
        with self.assertRaises(wc.WebChatError):
            chat._page("nope")


if __name__ == "__main__":
    unittest.main()


class ZaiModelTests(unittest.TestCase):
    def _page(self, current):
        calls = []

        class P:
            def evaluate(self, js, arg=None):
                calls.append(arg)
                if isinstance(arg, dict) and "label" in arg:
                    return "ok" if arg["label"] == current else "opened"
                return None

            def wait_for_timeout(self, ms):
                pass
        return P(), calls

    def test_already_on_the_wanted_model_is_left_alone(self):
        p, calls = self._page("GLM-5.3-Flash")
        wc._zai_pick_model(p, "flash")
        self.assertEqual(len(calls), 1)

    def test_another_model_is_picked_from_the_menu(self):
        p, calls = self._page("GLM-5.3-Flash")
        wc._zai_pick_model(p, "5.3")
        self.assertEqual(calls[1], "glm-5.3")

    def test_a_redirect_to_sign_in_is_a_clear_error(self):
        p, calls = self._page("GLM-5.3-Flash")
        p.url = "https://chat.z.ai/auth?redirect=/"
        with self.assertRaises(wc.NeedsSignIn):
            wc._zai_pick_model(p, "5.3")


class AttachTests(unittest.TestCase):
    def test_ports_and_endpoint(self):
        self.assertEqual(wc.cdp_endpoint("zai"), "http://127.0.0.1:9222")
        self.assertEqual(wc.cdp_endpoint("deepseek"), "http://127.0.0.1:9223")

    def test_nothing_listening_is_not_alive(self):
        from unittest import mock
        with mock.patch.object(wc.urllib.request, "urlopen",
                               side_effect=OSError("refused")):
            self.assertFalse(wc.cdp_alive("zai"))

    def test_a_running_chrome_is_reused_not_started_twice(self):
        from unittest import mock
        with mock.patch.object(wc, "cdp_alive", return_value=True), \
                mock.patch.object(wc.subprocess, "Popen") as popen:
            self.assertTrue(wc.start_chrome("zai", "/tmp/x"))
            popen.assert_not_called()

    def test_chrome_is_started_with_the_port_and_profile(self):
        from unittest import mock
        alive = iter([False, True])
        with mock.patch.object(wc, "cdp_alive", side_effect=lambda k: next(alive)), \
                mock.patch.object(wc, "find_chrome", return_value="chrome"), \
                mock.patch.object(wc.subprocess, "Popen") as popen:
            self.assertTrue(wc.start_chrome("zai", "/tmp/x", wait=5))
        args = popen.call_args.args[0]
        self.assertIn("--remote-debugging-port=9222", args)
        self.assertTrue(any(a.startswith("--user-data-dir=") and "zai" in a
                            for a in args))
