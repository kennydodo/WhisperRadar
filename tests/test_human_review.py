import json
from unittest import mock

from tests.test_external_prompts import Base
from tests.test_webstages import Fake
from whisperradar import db, human_review as hr
from whisperradar.webapp import create_app, _Job


ORIGINAL = 'You can put money aside each month. Start with an amount you can afford.'
REVISED = 'Put a little money aside each month. Start with an amount you can afford.'


def verdict(ready=False, passage='You can put money aside each month.'):
    return json.dumps(dict(decision='READY' if ready else 'NEEDS_REVISION',
                          summary='Natural aloud.' if ready else 'Tighten the phrasing.',
                          issues=[] if ready else [dict(passage=passage,
                              problem='Wordy phrasing.', suggestion='Use a direct sentence.')]))


class HumanReviewTests(Base):
    def setUp(self):
        super().setUp()
        (self.pdir/'script.md').write_text(ORIGINAL, encoding='utf-8')

    def run_review(self, transport, **kw):
        return hr.run(self.cfg, self.pid, transport, 'zai', 'deepseek',
                      log=lambda _: None, **kw)

    def script(self):
        return (self.pdir/'script.md').read_text(encoding='utf-8')

    def test_revision_is_reviewed_before_save_and_no_stage_advance(self):
        c = db.connect(self.cfg.db_path)
        before = db.get_production(c,self.pid)['stage']
        c.close()
        t = Fake(deepseek=[verdict(),verdict(True)], zai=[REVISED])
        out = self.run_review(t)
        self.assertEqual(out['status'],'ready')
        self.assertEqual([x['site'] for x in t.calls],['deepseek','zai','deepseek'])
        self.assertTrue(all(x['new_chat'] for x in t.calls))
        self.assertIn(ORIGINAL,t.calls[1]['prompt'])
        self.assertIn(REVISED,t.calls[2]['prompt'])
        self.assertEqual(self.script().strip(),REVISED)
        folder = self.pdir/'human_review'/out['run_id']
        self.assertEqual((folder/'original.txt').read_text(),ORIGINAL)
        self.assertTrue((folder/'revision-1.txt').exists())
        c = db.connect(self.cfg.db_path)
        self.assertEqual(db.get_production(c,self.pid)['stage'],before)
        c.close()
        self.assertEqual(hr.load(self.pdir)['status'],'ready')

    def test_ready_immediately_does_not_call_writer(self):
        t = Fake(deepseek=[verdict(True)])
        self.assertEqual(self.run_review(t)['revisions'],0)
        self.assertEqual(len(t.calls),1)

    def test_limit_includes_final_review_and_keeps_original(self):
        t = Fake(deepseek=[verdict()]*4, zai=[ORIGINAL]*3)
        result = self.run_review(t,rounds=3)
        self.assertEqual(result['status'],'limit_reached')
        self.assertEqual(result['reviews'],4)
        self.assertEqual(result['revisions'],3)
        self.assertEqual(self.script(),ORIGINAL)

    def test_approval_at_last_review_is_accepted(self):
        t = Fake(deepseek=[verdict()]*3+[verdict(True)],zai=[ORIGINAL]*3)
        self.assertEqual(self.run_review(t)['status'],'ready')

    def test_five_round_limit(self):
        t = Fake(deepseek=[verdict()]*6,zai=[ORIGINAL]*5)
        result = self.run_review(t,rounds=5)
        self.assertEqual((result['status'],result['reviews'],result['revisions']),('limit_reached',6,5))

    def test_stop_after_writer_preserves_revision_but_not_live_script(self):
        t = Fake(deepseek=[verdict()],zai=[REVISED])
        result = self.run_review(t,should_stop=lambda:len(t.calls)>=2)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['revisions'],1)
        self.assertEqual(self.script(),ORIGINAL)
        self.assertEqual((self.pdir/'human_review'/result['run_id']/'latest.txt').read_text().strip(),REVISED)

    def test_malformed_or_contradictory_reviews_fail_closed(self):
        for reply in ['Looks good', '{"decision":"READY","summary":"OK","issues":[{}]}', verdict(passage='invented quote')]:
            with self.subTest(reply=reply):
                with self.assertRaises(ValueError):
                    self.run_review(Fake(deepseek=[reply,reply]))
                self.assertEqual(self.script(),ORIGINAL)
                self.assertEqual(hr.load(self.pdir)['status'],'failed')

    def test_stop_does_not_replace_script(self):
        t = Fake()
        self.assertEqual(self.run_review(t,should_stop=lambda:True)['status'],'stopped')
        self.assertEqual(t.calls,[])
        self.assertEqual(self.script(),ORIGINAL)

    def test_transport_cancellation_is_stopped_not_failed(self):
        stopped = []
        t = Fake()
        def cancel(*args,**kwargs):
            stopped.append(True)
            raise RuntimeError('Stopped while waiting for capacity')
        t.ask = cancel
        result = self.run_review(t,should_stop=lambda:bool(stopped))
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(self.script(),ORIGINAL)

    def test_concurrent_edit_is_preserved(self):
        t = Fake(deepseek=[verdict(True)])
        ask = t.ask
        def edited(*args,**kwargs):
            (self.pdir/'script.md').write_text('My manual edit.',encoding='utf-8')
            return ask(*args,**kwargs)
        t.ask = edited
        self.assertEqual(self.run_review(t)['status'],'conflict')
        self.assertEqual(self.script(),'My manual edit.')

    def test_approval_expires_after_script_edit(self):
        self.run_review(Fake(deepseek=[verdict(True)]))
        (self.pdir/'script.md').write_text('Edited later.',encoding='utf-8')
        self.assertEqual(hr.load(self.pdir)['status'],'outdated')

    def test_incomplete_writer_fails_without_replacing_script(self):
        with self.assertRaises(ValueError):
            self.run_review(Fake(deepseek=[verdict()],zai=['Done.']))
        self.assertEqual(self.script(),ORIGINAL)

    def test_repeated_runs_preserve_history(self):
        a = self.run_review(Fake(deepseek=[verdict(True)]))
        b = self.run_review(Fake(deepseek=[verdict(True)]))
        self.assertNotEqual(a['run_id'],b['run_id'])
        self.assertTrue((self.pdir/'human_review'/a['run_id']/'original.txt').exists())

    def test_same_site_roles_start_isolated_chats(self):
        t = Fake(zai=[verdict(),REVISED,verdict(True)])
        result = hr.run(self.cfg,self.pid,t,'zai','zai',log=lambda _:None)
        self.assertEqual(result['status'],'ready')
        self.assertTrue(all(c['new_chat'] for c in t.calls))

    def test_page_renders_result_and_downloads(self):
        result = self.run_review(Fake(deepseek=[verdict(True)]))
        client = create_app(self.cfg).test_client()
        response = client.get(f'/studio/{self.pid}?stage=script')
        self.assertEqual(response.status_code,200)
        self.assertIn('Start human-language review',response.get_data(as_text=True))
        self.assertIn('Ready — saved script updated',response.get_data(as_text=True))
        with client.get(f"/studio/file/{self.pid}/human_review/{result['run_id']}/original.txt") as download:
            self.assertEqual(download.status_code,200)

    def test_route_dispatches_selected_roles_and_limit_without_network(self):
        client = create_app(self.cfg).test_client()
        def synchronous(job,fn,kind):
            fn()
            return True
        with mock.patch.object(_Job,'start',synchronous), mock.patch.object(hr,'job') as runner:
            response = client.post(f'/studio/{self.pid}/human-review', data={'writer':'deepseek','judge':'zai','rounds':'5'})
            self.assertEqual(response.status_code,302)
            self.assertEqual(runner.call_args.args[2:5],('deepseek','zai',5))
            runner.reset_mock()
            client.post(f'/studio/{self.pid}/human-review',data={'rounds':'7'})
            client.post(f'/studio/{self.pid}/human-review',data={'writer':'missing'})
            self.assertFalse(runner.called)
            (self.pdir/'script.md').unlink()
            client.post(f'/studio/{self.pid}/human-review')
            self.assertFalse(runner.called)

    def test_files_mode_contains_entire_script_and_style(self):
        files=[]
        p=hr.prompt('reviewer','Title','STYLE',ORIGINAL,files=files)
        self.assertIn('current_script.txt',p)
        self.assertEqual({f['name']:f['text'] for f in files}['current_script.txt'],ORIGINAL)

    def test_reasoning_and_ui_do_not_hide_verdict_with_unescaped_quotes(self):
        reply = ('" Thinking with an unmatched quote.\nNeed final JSON.\n'
                 '{"decision":"NEEDS_REVISION","summary":"Fix phrasing.",'
                 '"issues":[{"passage":"You can put money aside each month.",'
                 '"problem":"The phrase "put money" sounds awkward.",'
                 '"suggestion":"Put a little money aside each month."}]}\n'
                 'DeepThink\nSearch\nAI-generated, for reference only')
        review = hr.parse_review(reply)
        self.assertEqual(review['decision'],'NEEDS_REVISION')
        self.assertIn('"put money"', review['issues'][0]['problem'])
        t = Fake(deepseek=[reply,verdict(True)],zai=[REVISED])
        self.assertEqual(self.run_review(t)['status'],'ready')
        self.assertIn('Fix phrasing.',t.calls[1]['prompt'])

    def test_final_verdict_wins_over_reasoning_example(self):
        self.assertEqual(hr.parse_review(verdict(True)+'\nThinking...\n'+verdict())['decision'],'NEEDS_REVISION')
        with self.assertRaises(ValueError):
            hr.parse_review(verdict(True)+'\n{"decision":"NEEDS_REVISION"')

    def test_one_format_correction_then_pass_to_writer(self):
        t = Fake(deepseek=['Malformed response',verdict(),verdict(True)],zai=[REVISED])
        result = self.run_review(t)
        self.assertEqual(result['status'],'ready')
        self.assertEqual([c['site'] for c in t.calls],['deepseek','deepseek','zai','deepseek'])
        self.assertFalse(t.calls[1]['new_chat'])
        self.assertEqual(result['revisions'],1)

    def test_claude_curly_json_after_truncated_preview(self):
        body = verdict().replace('"','”')
        # Mixed opening/closing delimiter shapes as rendered on Claude's page.
        body = body.replace('{”','{“').replace(',”',',“')
        reply = ('Claude responded: {"decision":"NEEDS_REVISION","summary":"Preview...\n'
                 'Thinking about source sentences.\n'+body+'\nReply\nClaude finished the response')
        self.assertEqual(hr.parse_review(reply)['decision'],'NEEDS_REVISION')
        t = Fake(deepseek=[reply,verdict(True)],zai=[REVISED])
        self.assertEqual(self.run_review(t)['status'],'ready')

    def test_quote_typography_matches_but_paraphrase_does_not(self):
        self.assertTrue(hr._quoted_in_script("It isn't magic.", 'It isn’t magic.'))
        self.assertFalse(hr._quoted_in_script('It is not magic.', 'It isn’t magic.'))

    def test_nested_quoted_examples_separated_by_commas(self):
        reply = ('{"decision":"NEEDS_REVISION","summary":"Review contains "examples", "more examples".",'
                 '"issues":[{"passage":"You can put money aside each month.",'
                 '"problem":"Repeated openers ("Imagine", "Picture", "Suppose") sound stiff.",'
                 '"suggestion":"Use "Put money aside", "Start small" as examples."}]}')
        review = hr.parse_review(reply)
        self.assertEqual(review['decision'],'NEEDS_REVISION')
        self.assertEqual(review['issues'][0]['problem'],'Repeated openers ("Imagine", "Picture", "Suppose") sound stiff.')
        self.assertIn('"more examples"',review['summary'])

    def test_schema_recovery_never_drops_broken_issues_or_fabricates_ready(self):
        for text in [
            '{"decision":"READY","summary":"OK","issues":[{"passage":"bad"}]}',
            '{"decision":"READY","summary":"OK","issues":[]',
            '{"decision":"READY","summary":"OK","issues":[],"unknown":true}',
        ]:
            self.assertIsNone(hr._recover_review_fields(text))
        with self.assertRaises(ValueError):
            hr.parse_review('{"decision":"NEEDS_REVISION","summary":"OK","issues":[]}')
