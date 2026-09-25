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
        with mock.patch.object(studio.urllib.request, "urlopen", fake):
            out = studio.openai_chat(PROVIDER, "prompt", max_tokens=2120)
        self.assertEqual(out, "hi")
        self.assertEqual(seen["payload"]["max_tokens"], 2120)

    def test_max_tokens_is_omitted_when_not_given(self):
        body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
        fake, seen = _capture_urlopen(_FakeResponse(body=body, ctype="application/json"))
        with mock.patch.object(studio.urllib.request, "urlopen", fake):
            studio.openai_chat(PROVIDER, "prompt")
        self.assertNotIn("max_tokens", seen["payload"])

    def test_judge_output_is_capped(self):
        captured = {}

        def fake_llm(cfg, prompt, provider=None, max_tokens=None):
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
        with mock.patch.object(studio.urllib.request, "urlopen",
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

        with mock.patch.object(studio.urllib.request, "urlopen",
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

        def adapter(p, prompt, timeout=600, max_tokens=None):
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
        with mock.patch.object(studio.urllib.request, "urlopen",
                               lambda req, timeout=None: resp), \
                mock.patch.object(studio, "LLM_IDLE_TIMEOUT", 30):
            self.assertEqual(studio.openai_chat(PROVIDER, "prompt"), "Hello world")

    def test_llm_generate_retries_a_stall_on_another_provider(self):
        alt = dict(PROVIDER, name="deepseek", base_url="http://other.test")
        calls = []

        def adapter(p, prompt, timeout=600, max_tokens=None):
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
        def adapter(p, prompt, timeout=600, max_tokens=None):
            raise studio.LLMStalled("stalled")

        with mock.patch.object(studio, "_resolve_provider",
                               lambda cfg, name=None: PROVIDER), \
                mock.patch.object(studio, "CHAT_APIS", {"openai": adapter}), \
                mock.patch.object(studio, "_fallback_provider",
                                  lambda cfg, failed: None):
            with self.assertRaises(studio.LLMStalled):
                studio.llm_generate(object(), "prompt")


if __name__ == "__main__":
    unittest.main()
