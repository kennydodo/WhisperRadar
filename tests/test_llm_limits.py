"""LLM length/stall guards.

These run without any network or API key. They cover the two things that made
script generation slow/unbounded:

* the output is now capped with ``max_tokens`` derived from the target length,
  and the target is never planned longer than the source transcript; and
* a provider that opens the stream and then only sends keep-alives (glm-flash
  on a large prompt) is detected as STALLED and retried on a different ready
  provider instead of hanging out the whole timeout.

Run: python -m unittest discover -s tests
"""
import json
import os
import time
import unittest
from unittest import mock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisperradar import studio  # noqa: E402

PROVIDER = {"name": "glm-flash", "api": "openai", "base_url": "http://llm.test",
            "model": "glm-5.3-flash", "api_key": "k", "env_key": "WR_TEST_KEY"}


class _FakeResponse:
    """Minimal stand-in for an http.client.HTTPResponse."""

    def __init__(self, lines=None, body=b"", ctype="text/event-stream"):
        self._lines = lines or []
        self._body = body
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body


def _capture_urlopen(response):
    seen = {}

    def fake(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return response

    return fake, seen


def _sse(*contents):
    lines = []
    for c in contents:
        lines.append(("data: " + json.dumps(
            {"choices": [{"delta": {"content": c}}]})).encode())
    lines.append(b"data: [DONE]")
    return lines


class ScriptLengthTests(unittest.TestCase):
    def test_max_tokens_scales_with_the_target(self):
        self.assertEqual(studio.script_max_tokens(1200), 2120)
        self.assertEqual(studio.script_max_tokens(0), 200)

    def test_target_is_never_longer_than_the_source(self):
        # An over-long explicit setting is capped by the transcript length.
        self.assertEqual(studio.script_target_words(5000, 2000), 2000)
        # A shorter explicit setting still wins.
        self.assertEqual(studio.script_target_words(800, 2000), 800)
        # No setting: match the source.
        self.assertEqual(studio.script_target_words(None, 2000), 2000)
        # No source at all: the documented 1200 default.
        self.assertEqual(studio.script_target_words(None, 0), 1200)
        self.assertEqual(studio.script_target_words(None, None), 1200)


class MaxTokensTests(unittest.TestCase):
    def test_openai_chat_sends_max_tokens(self):
        body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
        fake, seen = _capture_urlopen(_FakeResponse(body=body, ctype="application/json"))
        with mock.patch.object(studio, "_curl_binary", return_value=None), \
                mock.patch.object(studio.urllib.request, "urlopen", fake):
            out = studio.openai_chat(PROVIDER, "prompt", max_tokens=2120)
        self.assertEqual(out, "hi")
        self.assertEqual(seen["payload"]["max_tokens"], 2120)

    def test_max_tokens_is_omitted_when_not_given(self):
        body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
        fake, seen = _capture_urlopen(_FakeResponse(body=body, ctype="application/json"))
        with mock.patch.object(studio, "_curl_binary", return_value=None), \
                mock.patch.object(studio.urllib.request, "urlopen", fake):
            studio.openai_chat(PROVIDER, "prompt")
        self.assertNotIn("max_tokens", seen["payload"])

    def test_judge_output_is_capped(self):
        captured = {}

        def fake_llm(cfg, prompt, provider=None, max_tokens=None,
                    temperature=1.0):
            captured["max_tokens"] = max_tokens
            return '{"score": 8.5, "criteria": {}, "feedback": [], "weak_spans": []}'

        with mock.patch.object(studio, "llm_generate", fake_llm):
            rating = studio.rate_script(object(), "t", "g", "script", "src",
                                        "style", None)
        self.assertEqual(captured["max_tokens"], 900)
        self.assertEqual(rating["score"], 8.5)


class StallTests(unittest.TestCase):
    def _heartbeat(self):
        class _Heartbeat(_FakeResponse):
            def __iter__(self):
                while True:  # keep-alives forever, never any content
                    yield b": ping\n"

        return _Heartbeat()

    def test_a_provider_that_only_sends_keepalives_is_stalled(self):
        # The glm-flash failure: the socket stays busy, so the socket timeout
        # never fires. The first-token guard must break out quickly instead.
        with mock.patch.object(studio, "_curl_binary", return_value=None), \
                mock.patch.object(studio.urllib.request, "urlopen",
                               lambda req, timeout=None: self._heartbeat()), \
                mock.patch.object(studio, "LLM_FIRST_TOKEN_TIMEOUT", 0.05), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 0.05):
            started = time.monotonic()
            with self.assertRaises(studio.LLMStalled):
                studio.openai_chat(PROVIDER, "prompt", timeout=1800)
            self.assertLess(time.monotonic() - started, 5.0)

    def test_a_stall_after_the_first_token_is_detected_too(self):
        class _Half(_FakeResponse):
            def __iter__(self):
                yield ("data: " + json.dumps(
                    {"choices": [{"delta": {"content": "hi"}}]})).encode()
                while True:  # content started, then only keep-alives
                    yield b": ping\n"

        with mock.patch.object(studio, "_curl_binary", return_value=None), \
                mock.patch.object(studio.urllib.request, "urlopen",
                               lambda req, timeout=None: _Half()), \
                mock.patch.object(studio, "LLM_FIRST_TOKEN_TIMEOUT", 30), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 0.05):
            with self.assertRaises(studio.LLMStalled):
                studio.openai_chat(PROVIDER, "prompt", timeout=1800)

    def test_the_first_token_allowance_is_much_longer_than_the_idle_one(self):
        # A 32k-char prompt took deepseek ~118s to start streaming, so a short
        # first-token rule would kill a healthy call.
        self.assertGreater(studio.LLM_FIRST_TOKEN_TIMEOUT,
                           studio.LLM_IDLE_TIMEOUT)

    def test_an_empty_answer_is_retried_on_another_provider(self):
        alt = dict(PROVIDER, name="deepseek", base_url="http://other.test")
        calls = []

        def adapter(p, prompt, timeout=600, max_tokens=None, temperature=1.0):
            calls.append(p["name"])
            if p["name"] == "glm-flash":
                raise studio.LLMEmpty("glm-flash returned an empty response")
            return "fallback-ok"

        with mock.patch.object(studio, "_resolve_provider",
                               lambda cfg, name=None: PROVIDER), \
                mock.patch.object(studio, "CHAT_APIS", {"openai": adapter}), \
                mock.patch.object(studio, "_fallback_provider",
                                  lambda cfg, failed: alt):
            self.assertEqual(studio.llm_generate(object(), "prompt"), "fallback-ok")
        self.assertEqual(calls, ["glm-flash", "deepseek"])

    def test_a_stream_with_content_is_not_stalled(self):
        resp = _FakeResponse(lines=_sse("Hello ", "world"))
        with mock.patch.object(studio, "_curl_binary", return_value=None), \
                mock.patch.object(studio.urllib.request, "urlopen",
                               lambda req, timeout=None: resp), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 30):
            self.assertEqual(studio.openai_chat(PROVIDER, "prompt"), "Hello world")

    def test_llm_generate_retries_a_stall_on_another_provider(self):
        alt = dict(PROVIDER, name="deepseek", base_url="http://other.test")
        calls = []

        def adapter(p, prompt, timeout=600, max_tokens=None, temperature=1.0):
            calls.append(p["name"])
            if p["name"] == "glm-flash":
                raise studio.LLMStalled("glm-flash sent no content for 150s")
            return "fallback-ok"

        with mock.patch.object(studio, "_resolve_provider",
                               lambda cfg, name=None: PROVIDER), \
                mock.patch.object(studio, "CHAT_APIS", {"openai": adapter}), \
                mock.patch.object(studio, "_fallback_provider",
                                  lambda cfg, failed: alt):
            out = studio.llm_generate(object(), "prompt", max_tokens=100)
        self.assertEqual(out, "fallback-ok")
        self.assertEqual(calls, ["glm-flash", "deepseek"])

    def test_a_stall_with_no_fallback_propagates(self):
        def adapter(p, prompt, timeout=600, max_tokens=None, temperature=1.0):
            raise studio.LLMStalled("stalled")

        with mock.patch.object(studio, "_resolve_provider",
                               lambda cfg, name=None: PROVIDER), \
                mock.patch.object(studio, "CHAT_APIS", {"openai": adapter}), \
                mock.patch.object(studio, "_fallback_provider",
                                  lambda cfg, failed: None):
            with self.assertRaises(studio.LLMStalled):
                studio.llm_generate(object(), "prompt")


class _FakeCurlProc:
    """Stands in for subprocess.Popen for the curl transport tests."""

    def __init__(self, lines, returncode=0, stderr=""):
        self.stdout = iter(lines)
        self.stderr = _StrReader(stderr)
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class _StrReader:
    def __init__(self, s):
        self._s = s

    def read(self):
        return self._s


class CurlTransportTests(unittest.TestCase):
    """openai_chat() now prefers shelling out to curl (see studio.py's
    _openai_chat_curl docstring: some networks' HTTPS-inspecting security
    software silently stalls Python's OpenSSL-based streaming reads while
    curl's own TLS stack - Schannel on Windows - handles the same connection
    fine). These cover that transport directly; StallTests above covers the
    urllib fallback with _curl_binary forced off."""

    def test_openai_chat_uses_curl_when_available(self):
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_openai_chat_curl",
                                  return_value="via-curl") as curl_fn, \
                mock.patch.object(studio, "_openai_chat_urllib",
                                  return_value="via-urllib") as urllib_fn:
            self.assertEqual(studio.openai_chat(PROVIDER, "prompt"), "via-curl")
        curl_fn.assert_called_once()
        urllib_fn.assert_not_called()

    def test_openai_chat_falls_back_to_urllib_without_curl(self):
        with mock.patch.object(studio, "_curl_binary", return_value=None), \
                mock.patch.object(studio, "_openai_chat_curl",
                                  return_value="via-curl") as curl_fn, \
                mock.patch.object(studio, "_openai_chat_urllib",
                                  return_value="via-urllib") as urllib_fn:
            self.assertEqual(studio.openai_chat(PROVIDER, "prompt"), "via-urllib")
        curl_fn.assert_not_called()
        urllib_fn.assert_called_once()

    def test_wr_llm_transport_env_forces_urllib(self):
        with mock.patch.dict(os.environ, {"WR_LLM_TRANSPORT": "urllib"}), \
                mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_openai_chat_curl",
                                  return_value="via-curl") as curl_fn, \
                mock.patch.object(studio, "_openai_chat_urllib",
                                  return_value="via-urllib") as urllib_fn:
            self.assertEqual(studio.openai_chat(PROVIDER, "prompt"), "via-urllib")
        curl_fn.assert_not_called()
        urllib_fn.assert_called_once()

    def test_curl_stream_returns_the_full_answer(self):
        lines = [
            'data: ' + json.dumps({"choices": [{"delta": {"content": "Hello "}}]}) + "\n",
            'data: ' + json.dumps({"choices": [{"delta": {"content": "world"}}]}) + "\n",
            "data: [DONE]\n",
        ]
        proc = _FakeCurlProc(lines)
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc):
            out = studio.openai_chat(PROVIDER, "prompt")
        self.assertEqual(out, "Hello world")

    def test_curl_stream_with_only_keepalives_is_stalled(self):
        def endless_pings():
            # bounded so a leaked background reader thread (the pump thread
            # outlives the assertRaises block since a fake proc can't really
            # be killed) can't spin the CPU forever - 2s is far more than the
            # 0.05s stall timeouts below need to fire.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                yield ": ping\n"

        proc = _FakeCurlProc(endless_pings())
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc), \
                mock.patch.object(studio, "LLM_FIRST_TOKEN_TIMEOUT", 0.05), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 0.05):
            started = time.monotonic()
            with self.assertRaises(studio.LLMStalled):
                studio.openai_chat(PROVIDER, "prompt", timeout=1800)
            self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(proc.killed)

    def test_curl_reasoning_content_resets_the_first_token_clock(self):
        # mimo/deepseek-r1-style models stream their chain-of-thought as
        # reasoning_content BEFORE any real content, which can legitimately
        # take a long time on a big planning prompt. That must NOT be
        # mistaken for a stall - only the final real content is missing.
        lines = [
            "data: " + json.dumps(
                {"choices": [{"delta": {"content": None,
                                        "reasoning_content": "thinking..."}}]}) + "\n",
            "data: " + json.dumps(
                {"choices": [{"delta": {"content": "answer"}}]}) + "\n",
            "data: [DONE]\n",
        ]
        proc = _FakeCurlProc(lines)
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc), \
                mock.patch.object(studio, "LLM_FIRST_TOKEN_TIMEOUT", 0.05), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 30):
            out = studio.openai_chat(PROVIDER, "prompt", timeout=1800)
        self.assertEqual(out, "answer")

    def test_reasoning_then_true_silence_still_stalls(self):
        # reasoning_content resets the clock (proof the request is alive),
        # but if the stream then goes genuinely silent - no more reasoning
        # OR content - for longer than the idle window, that is still a
        # real stall and must still be caught.
        def one_reasoning_chunk_then_silence():
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": None,
                                        "reasoning_content": "start"}}]}) + "\n"
            time.sleep(2.0)  # idle gap, well past the 0.05s IDLE_TIMEOUT below
            yield "data: [DONE]\n"  # only reached if not killed first

        proc = _FakeCurlProc(one_reasoning_chunk_then_silence())
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc), \
                mock.patch.object(studio, "LLM_FIRST_TOKEN_TIMEOUT", 30), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 0.05):
            started = time.monotonic()
            with self.assertRaises(studio.LLMStalled):
                studio.openai_chat(PROVIDER, "prompt", timeout=1800)
            # caught well before the 2s fake silence even finishes
            self.assertLess(time.monotonic() - started, 1.5)

    def test_curl_non_zero_exit_raises_with_the_response_body(self):
        lines = ['{"error": {"message": "Invalid api_key format"}}\n']
        proc = _FakeCurlProc(lines, returncode=22)
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                studio.openai_chat(PROVIDER, "prompt")
        self.assertIn("Invalid api_key format", str(ctx.exception))

    def test_curl_config_never_puts_the_key_on_argv(self):
        # the whole point of -K over a plain curl command line: the key
        # travels as config text, never as a command-line argument.
        cfg = studio._curl_config(
            "https://api.test/v1/chat/completions",
            {"Authorization": "Bearer super-secret-key"}, {"model": "m"})
        self.assertIn("super-secret-key", cfg)
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch("subprocess.Popen") as popen:
            popen.return_value.stdin = mock.Mock()
            studio._spawn_curl(cfg, 60)
        argv = popen.call_args[0][0]
        self.assertTrue(all("super-secret-key" not in str(a) for a in argv))

    def test_curl_subprocess_is_opened_as_explicit_utf8(self):
        # text=True with no encoding falls back to locale.getpreferredencoding(),
        # which on Windows is the system ANSI codepage (e.g. cp1252), not
        # UTF-8. The API's SSE response is UTF-8, so a curly apostrophe (E2 80
        # 99) got decoded one byte at a time as cp1252 and came out as the
        # mojibake seen throughout real judge reviews and generated scripts -
        # encoding="utf-8" must be explicit.
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch("subprocess.Popen") as popen:
            popen.return_value.stdin = mock.Mock()
            studio._spawn_curl("cfg", 60)
        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")

    def test_a_real_utf8_curl_response_decodes_correctly(self):
        # a smoke test with the actual bug's byte pattern, run through a
        # REAL subprocess (a tiny python one-liner standing in for curl) so
        # it exercises the real decoding path end to end -
        # _curl_read_stream reads whatever Popen handed it, so a real
        # UTF-8-emitting process plus an explicit encoding="utf-8" Popen
        # call (exactly what _spawn_curl now does) proves the fix works,
        # without needing to fight _spawn_curl's hardcoded curl-only argv.
        import subprocess as sp
        # sys.executable is the interpreter actually running the tests -
        # guaranteed real, unlike "python3", which on Windows is often the
        # Microsoft Store App Execution Alias stub that prints an error to
        # stderr and exits non-zero (which _curl_read_stream then correctly
        # reports as a failed request).
        content = json.dumps(
            {"choices": [{"delta": {"content": "a cat’s coat"}}]})
        script = (
            "import sys; "
            f"sys.stdout.buffer.write('data: {content}\\n'.encode('utf-8')); "
            "sys.stdout.buffer.write(b'data: [DONE]\\n')"
        )
        real_proc = sp.Popen([sys.executable, "-c", script], stdout=sp.PIPE,
                             stderr=sp.PIPE, text=True, encoding="utf-8")
        out = studio._curl_read_stream(real_proc, "test-provider", 10)
        self.assertEqual(out, "a cat’s coat")
        self.assertNotIn("â", out)


class TemperatureTests(unittest.TestCase):
    """Every LLM call shares one transport with a single hardcoded
    temperature=1.0 - fine for the writer/planner (a retry must genuinely
    differ) but wrong for a judge, which should grade the same input the
    same way twice. `temperature` is now a real parameter threaded through
    the whole chain, defaulting to 1.0 everywhere so no existing call site
    changes behavior unless it explicitly passes a lower value."""

    def test_default_temperature_is_1_in_the_curl_payload(self):
        seen = {}

        def fake_config(url, headers, payload):
            seen["temperature"] = payload["temperature"]
            return "cfg"

        lines = ['data: ' + json.dumps(
            {"choices": [{"delta": {"content": "hi"}}]}) + "\n", "data: [DONE]\n"]
        proc = _FakeCurlProc(lines)
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_curl_config", fake_config), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc):
            studio.openai_chat(PROVIDER, "prompt")
        self.assertEqual(seen["temperature"], 1.0)

    def test_a_lower_temperature_reaches_the_curl_payload(self):
        seen = {}

        def fake_config(url, headers, payload):
            seen["temperature"] = payload["temperature"]
            return "cfg"

        lines = ['data: ' + json.dumps(
            {"choices": [{"delta": {"content": "hi"}}]}) + "\n", "data: [DONE]\n"]
        proc = _FakeCurlProc(lines)
        with mock.patch.object(studio, "_curl_binary", return_value="/usr/bin/curl"), \
                mock.patch.object(studio, "_curl_config", fake_config), \
                mock.patch.object(studio, "_spawn_curl", return_value=proc):
            studio.openai_chat(PROVIDER, "prompt", temperature=0.2)
        self.assertEqual(seen["temperature"], 0.2)

    def test_llm_generate_passes_temperature_through(self):
        seen = {}

        def adapter(p, prompt, timeout=600, max_tokens=None, temperature=1.0):
            seen["temperature"] = temperature
            return "ok"

        with mock.patch.object(studio, "_resolve_provider",
                               lambda cfg, name=None: PROVIDER), \
                mock.patch.object(studio, "CHAT_APIS", {"openai": adapter}):
            studio.llm_generate(object(), "prompt", temperature=0.3)
        self.assertEqual(seen["temperature"], 0.3)

    def test_llm_generate_defaults_temperature_to_1(self):
        seen = {}

        def adapter(p, prompt, timeout=600, max_tokens=None, temperature=1.0):
            seen["temperature"] = temperature
            return "ok"

        with mock.patch.object(studio, "_resolve_provider",
                               lambda cfg, name=None: PROVIDER), \
                mock.patch.object(studio, "CHAT_APIS", {"openai": adapter}):
            studio.llm_generate(object(), "prompt")
        self.assertEqual(seen["temperature"], 1.0)

    def test_rate_script_forwards_its_temperature_to_llm_generate(self):
        seen = {}

        def fake_llm(cfg, prompt, provider=None, max_tokens=None, temperature=1.0):
            seen["temperature"] = temperature
            return '{"score": 8.0, "criteria": {}, "feedback": [], "weak_spans": []}'

        with mock.patch.object(studio, "llm_generate", fake_llm):
            studio.rate_script(object(), "t", "g", "script", "src", "style",
                               None, temperature=0.1)
        self.assertEqual(seen["temperature"], 0.1)

    def test_review_shotlist_forwards_its_temperature_to_llm_generate(self):
        seen = {}

        def fake_llm(cfg, prompt, provider=None, temperature=1.0, max_tokens=None):
            seen["temperature"] = temperature
            return '{"shots": []}'

        data = {"shots": [{"cues": "1-1", "asset": "a.png"}],
               "images": [{"file": "a.png", "prompt": "p"}]}
        cues = [{"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
                "text": "hi"}]
        with mock.patch.object(studio, "llm_generate", fake_llm):
            studio.review_shotlist(object(), data, cues, None,
                                   temperature=0.4)
        self.assertEqual(seen["temperature"], 0.4)


if __name__ == "__main__":
    unittest.main()
