"""No browser/model calls: exercise the actual scene-plan webchat adapter."""
import contextlib
import json
import unittest
from unittest.mock import patch

from tests.test_explainer_scenes import ScenePlanTests, plan
from whisperradar import explainer_scenes as es, webchat, webstages
from whisperradar.scene_chat import SceneChats


class Transport:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.current = {}
        self.calls = []
        self.n = 0
        self.fail = None

    def set_stop(self, stop):
        self.stop = stop

    def adopt(self, site, url):
        self.current[site] = url

    def urls(self):
        return dict(self.current)

    def ask(self, site, text, files, new_chat, ready):
        self.calls.append((site, text, new_chat, self.current.get(site)))
        if self.fail:
            error, self.fail = self.fail, None
            raise error
        if new_chat:
            self.n += 1
            self.current[site] = f'https://example.test/{site}/chat/{self.n}'
        elif not self.current.get(site):
            raise webchat.ChatLost('No established chat')
        result = next(self.replies, '{}')
        if isinstance(result, Exception):
            raise result
        return result


class RoleChatTests(unittest.TestCase):
    def test_judge_first_establishes_writer_then_reuses_writer(self):
        t = Transport()
        chats = SceneChats(t, {'writer': 'claude', 'judge': 'deepseek'}, lambda _: None)
        chats.ask('judge', 'full review context', True)
        chats.ask('writer', 'plan plus narration plus findings', False)
        chats.ask('writer', 'next complete correction', False)
        self.assertEqual([c[2] for c in t.calls], [True, True, False])
        self.assertEqual(t.calls[2][3], chats.role_urls['writer'])

    def test_same_site_restores_the_correct_role_conversation(self):
        t = Transport()
        chats = SceneChats(t, {'writer': 'deepseek', 'judge': 'deepseek'}, lambda _: None)
        chats.ask('judge', 'review', True)
        judge = chats.role_urls['judge']
        chats.ask('writer', 'correct plan', False)
        writer = chats.role_urls['writer']
        self.assertNotEqual(writer, judge)
        chats.ask('judge', 'resend verdict', False)
        self.assertEqual(t.calls[-1][3], judge)
        chats.ask('writer', 'another correction', False)
        self.assertEqual(t.calls[-1][3], writer)

    def test_lost_followup_restarts_once_with_the_original_context(self):
        t = Transport()
        chats = SceneChats(t, {'writer': 'claude', 'judge': 'deepseek'}, lambda _: None)
        chats.ask('judge', 'FULL NARRATION AND COMPILED PLAN', True)
        t.fail = webchat.ChatLost('closed')
        chats.ask('judge', 'resend verdict', False)
        self.assertTrue(t.calls[-1][2])
        self.assertIn('FULL NARRATION AND COMPILED PLAN', t.calls[-1][1])
        self.assertIn('resend verdict', t.calls[-1][1])

    def test_signin_error_is_not_retried_as_a_new_generation(self):
        t = Transport()
        chats = SceneChats(t, {'writer': 'claude', 'judge': 'deepseek'}, lambda _: None)
        t.fail = webchat.NeedsSignIn('Sign in first')
        with self.assertRaises(webchat.NeedsSignIn):
            chats.ask('writer', 'draft', False)
        self.assertEqual(len(t.calls), 1)

    def test_partial_reply_cannot_be_continued_in_a_new_chat(self):
        t = Transport()
        chats = SceneChats(t, {'writer': 'claude', 'judge': 'deepseek'}, lambda _: None)
        chats.ask('writer', 'complete request', True)
        t.fail = webchat.ChatLost('closed during partial reply')
        with self.assertRaises(webchat.ChatLost):
            chats.ask('writer', 'continue', False, continuation=True)
        self.assertEqual(len(t.calls), 2)

    def test_single_conversation_site_is_refused_for_two_roles(self):
        with self.assertRaisesRegex(webchat.WebChatError, 'different sites'):
            SceneChats(Transport(), {'writer': 'qwen', 'judge': 'qwen'}, lambda _: None)


class HandoffIntegrationTests(unittest.TestCase):
    setUp = ScenePlanTests.setUp

    def _transport(self, transport):
        @contextlib.contextmanager
        def fake(*args, **kwargs):
            yield transport
        return patch.object(webstages, 'web_transport', fake)

    def test_saved_plan_judge_writer_judge_handoff_promotes_only_approved_revision(self):
        saved = plan()
        (self.pdir/'plan.json').write_text(json.dumps(saved), encoding='utf-8')
        change = dict(saved['scenes'][1], title='Check model fit')
        replies = [json.dumps({'score': 7, 'pass': False, 'faults': ['scene b: title unclear'],
                               'fixes': ['scene b: improve title']}),
                   json.dumps({'scenes': [change]}),
                   json.dumps({'score': 9, 'pass': True, 'faults': [], 'fixes': []})]
        t = Transport(replies)
        with self._transport(t), patch.object(webstages, '_dump'):
            result = es.webchat_job(self.cfg, self.pid, 'deepseek', 'deepseek', lambda _: None)
        self.assertEqual(result['scenes'][1]['title'], 'Check model fit')
        self.assertEqual([c[2] for c in t.calls], [True, True, True])
        self.assertIn('CURRENT PLAN', t.calls[1][1])
        self.assertIn('TIMED NARRATION', t.calls[1][1])
        status = json.loads((self.pdir/'scene_plan_run.json').read_text())
        self.assertEqual(status['status'], 'approved')
        roles = json.loads((self.pdir/'scene_plan_chats.json').read_text())
        self.assertNotEqual(roles['writer']['url'], roles['judge']['url'])

    def test_transport_failure_surfaces_without_overwriting_saved_plan_or_review(self):
        before = json.dumps(plan())
        (self.pdir/'plan.json').write_text(before, encoding='utf-8')
        (self.pdir/'plan_review.json').write_text('{"score":8}', encoding='utf-8')
        t = Transport()
        t.fail = webchat.NeedsSignIn('Claude needs sign-in')
        with self._transport(t), self.assertRaises(webchat.NeedsSignIn):
            es.webchat_job(self.cfg, self.pid, 'claude', 'deepseek', lambda _: None)
        self.assertEqual((self.pdir/'plan.json').read_text(), before)
        self.assertEqual((self.pdir/'plan_review.json').read_text(), '{"score":8}')
        result = es.load_review(self.pdir)
        self.assertEqual(result['latest_run']['status'], 'failed')
        self.assertEqual(result['latest_run']['role'], 'judge')
        self.assertEqual(result['score'], 8)
        from whisperradar.webapp import create_app
        response = create_app(self.cfg).test_client().get(f'/studio/{self.pid}?stage=shots')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Latest scene-plan run failed', response.data)
        self.assertIn(b'earlier completed attempt', response.data)

    def test_failure_before_browser_start_is_recorded(self):
        @contextlib.contextmanager
        def failed(*args, **kwargs):
            raise webchat.WebChatError('Cannot open browser')
            yield
        with patch.object(webstages, 'web_transport', failed), self.assertRaises(webchat.WebChatError):
            es.webchat_job(self.cfg, self.pid, 'claude', 'deepseek', lambda _: None)
        self.assertEqual(es.load_review(self.pdir)['latest_run']['status'], 'failed')

    def test_writer_signin_failure_after_judging_preserves_the_plan(self):
        before = json.dumps(plan())
        (self.pdir/'plan.json').write_text(before, encoding='utf-8')
        t = Transport([
            json.dumps({'score': 7, 'pass': False, 'faults': ['scene b: unclear'], 'fixes': ['scene b: clarify']}),
            webchat.NeedsSignIn('Writer needs sign-in')])
        with self._transport(t), patch.object(webstages, '_dump'), self.assertRaises(webchat.NeedsSignIn):
            es.webchat_job(self.cfg, self.pid, 'claude', 'deepseek', lambda _: None)
        self.assertEqual([c[2] for c in t.calls], [True, True])
        self.assertEqual((self.pdir/'plan.json').read_text(), before)
        self.assertEqual(es.load_review(self.pdir)['latest_run']['role'], 'writer')


if __name__ == '__main__':
    unittest.main()
