import json
from unittest import mock

from tests.test_external_prompts import Base
from tests.test_webstages import Fake
from whisperradar import db, human_review as hr
from whisperradar import webchat, webstages
from whisperradar.webapp import create_app, _Job


ORIGINAL = 'You can put money aside each month. Start with an amount you can afford.'
REVISED = 'Put a little money aside each month. Start with an amount you can afford.'


def marked(script):
    return 'BEGIN_FINAL_SCRIPT\n' + script + '\nEND_FINAL_SCRIPT'


def verdict(ready=False, passage='You can put money aside each month.'):
    return json.dumps(dict(decision='READY' if ready else 'NEEDS_REVISION',
                          summary='Natural aloud.' if ready else 'Tighten the phrasing.',
                          checks={'editorial':'PASS' if ready else 'FAIL','accuracy':'PASS'},
                          resolved_ids=['I1'] if ready else [], evidence=[], calculations=[],
                          issues=[] if ready else [dict(id='I1', category='language', passage=passage,
                              problem='Wordy phrasing.', suggestion='Use a direct sentence.')]))


class HumanReviewTests(Base):
    def setUp(self):
        super().setUp()
        (self.pdir/'script.md').write_text(ORIGINAL, encoding='utf-8')

    def run_review(self, transport, **kw):
        kw.setdefault('reference_pass', False)
        return hr.run(self.cfg, self.pid, transport, 'zai', 'deepseek',
                      log=lambda _: None, **kw)

    def script(self):
        return (self.pdir/'script.md').read_text(encoding='utf-8')

    def test_revision_is_reviewed_before_save_and_no_stage_advance(self):
        c = db.connect(self.cfg.db_path)
        before = db.get_production(c,self.pid)['stage']
        c.close()
        t = Fake(deepseek=[verdict(),verdict(True)], zai=[marked(REVISED)])
        out = self.run_review(t)
        self.assertEqual(out['status'],'ready')
        self.assertEqual([x['site'] for x in t.calls],['deepseek','zai','deepseek'])
        self.assertEqual([x['new_chat'] for x in t.calls],[True,True,False])
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
        t = Fake(deepseek=[verdict()]*4, zai=[marked(ORIGINAL)]*3)
        result = self.run_review(t,rounds=3)
        self.assertEqual(result['status'],'limit_reached')
        self.assertEqual(result['reviews'],4)
        self.assertEqual(result['revisions'],3)
        self.assertEqual(self.script(),ORIGINAL)

    def test_approval_at_last_review_is_accepted(self):
        t = Fake(deepseek=[verdict()]*3+[verdict(True)],zai=[marked(ORIGINAL)]*3)
        self.assertEqual(self.run_review(t)['status'],'ready')

    def test_five_round_limit(self):
        t = Fake(deepseek=[verdict()]*6,zai=[marked(ORIGINAL)]*5)
        result = self.run_review(t,rounds=5)
        self.assertEqual((result['status'],result['reviews'],result['revisions']),('limit_reached',6,5))

    def test_stop_after_writer_preserves_revision_but_not_live_script(self):
        t = Fake(deepseek=[verdict()],zai=[marked(REVISED)])
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
            self.run_review(Fake(deepseek=[verdict()],zai=[marked('Done.')]))
        self.assertEqual(self.script(),ORIGINAL)

    def test_repeated_runs_preserve_history(self):
        a = self.run_review(Fake(deepseek=[verdict(True)]))
        b = self.run_review(Fake(deepseek=[verdict(True)]))
        self.assertNotEqual(a['run_id'],b['run_id'])
        self.assertTrue((self.pdir/'human_review'/a['run_id']/'original.txt').exists())

    def test_same_site_roles_keep_separate_persistent_chats(self):
        class RoleChats(Fake):
            current = None
            sequence = 0

            def ask(self, site, *args, new_chat=True, **kw):
                if new_chat:
                    self.sequence += 1
                    self.current = f'https://chat.z.ai/c/role-{self.sequence}'
                reply = super().ask(site,*args,new_chat=new_chat,**kw)
                self.calls[-1]['url'] = self.current
                return reply

            def urls(self):
                return {'zai':self.current}

            def adopt(self,site,url):
                self.current = url

        t = RoleChats(zai=[verdict(),marked(REVISED),verdict(passage=REVISED),marked(ORIGINAL),verdict(True)])
        result = hr.run(self.cfg,self.pid,t,'zai','zai',reference_pass=False,log=lambda _:None)
        self.assertEqual(result['status'],'ready')
        self.assertEqual([c['new_chat'] for c in t.calls],[True,True,False,False,False])
        self.assertEqual([c['url'] for c in t.calls],['https://chat.z.ai/c/role-1','https://chat.z.ai/c/role-2','https://chat.z.ai/c/role-1','https://chat.z.ai/c/role-2','https://chat.z.ai/c/role-1'])

    def test_page_renders_result_and_downloads(self):
        result = self.run_review(Fake(deepseek=[verdict(True)]))
        client = create_app(self.cfg).test_client()
        response = client.get(f'/studio/{self.pid}?stage=script')
        self.assertEqual(response.status_code,200)
        self.assertIn('Start GPT-Claude review',response.get_data(as_text=True))
        self.assertIn('Previous review approved',response.get_data(as_text=True))
        with client.get(f"/studio/file/{self.pid}/human_review/{result['run_id']}/original.txt") as download:
            self.assertEqual(download.status_code,200)

    def test_route_dispatches_selected_roles_and_limit_without_network(self):
        client = create_app(self.cfg).test_client()
        def synchronous(job,fn,kind):
            fn()
            return True
        with mock.patch.dict(webchat.SITES, {'chatgpt':webchat.DEEPSEEK,'claude':webchat.ZAI}), mock.patch.object(_Job,'start',synchronous), mock.patch.object(hr,'job') as runner:
            response = client.post(f'/studio/{self.pid}/human-review', data={'writer':'deepseek','judge':'zai','rounds':'5'})
            self.assertEqual(response.status_code,302)
            self.assertEqual(runner.call_args.args[2:5],('claude','chatgpt',5))
            runner.reset_mock()
            client.post(f'/studio/{self.pid}/human-review',data={'rounds':'7'})
            self.assertFalse(runner.called)
            (self.pdir/'script.md').unlink()
            client.post(f'/studio/{self.pid}/human-review')
            self.assertFalse(runner.called)

    def test_old_language_approval_does_not_get_gpt_claude_badge(self):
        result=self.run_review(Fake(deepseek=[verdict(True)]))
        pointer=self.pdir/'human_review.json'
        saved=json.loads(pointer.read_text(encoding='utf-8'));saved.pop('workflow')
        pointer.write_text(json.dumps(saved),encoding='utf-8')
        text=create_app(self.cfg).test_client().get(f'/studio/{self.pid}?stage=script').get_data(as_text=True)
        self.assertIn('Previous review approved',text)
        self.assertNotIn('<strong>Done with GPT-Claude</strong>',text)

    def test_bad_evidence_quote_requests_full_report_then_reaches_writer(self):
        bad=json.loads(verdict())
        bad['evidence']=[dict(passage='You can ... month.',url='https://example.org/source',
                             status='verified',supports='Source evidence.')]
        t=Fake(deepseek=[json.dumps(bad),verdict(),verdict(True)],zai=[marked(REVISED)])
        result=self.run_review(t)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['revisions'],1)
        self.assertIn('evidence[0].passage',t.calls[1]['prompt'])
        self.assertIn('ALL seven top-level fields',t.calls[1]['prompt'])
        self.assertIn('calculations',t.calls[1]['prompt'])
        self.assertIn('I1',t.calls[2]['prompt'])

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
        t = Fake(deepseek=[reply,verdict(),verdict(True)],zai=[marked(REVISED)])
        self.assertEqual(self.run_review(t)['status'],'ready')
        self.assertIn('Tighten the phrasing.',t.calls[2]['prompt'])

    def test_final_verdict_wins_over_reasoning_example(self):
        self.assertEqual(hr.parse_review(verdict(True)+'\nThinking...\n'+verdict())['decision'],'NEEDS_REVISION')
        with self.assertRaises(ValueError):
            hr.parse_review(verdict(True)+'\n{"decision":"NEEDS_REVISION"')

    def test_one_format_correction_then_pass_to_writer(self):
        t = Fake(deepseek=['Malformed response',verdict(),verdict(True)],zai=[marked(REVISED)])
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
        t = Fake(deepseek=[reply,verdict(True)],zai=[marked(REVISED)])
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

    def test_writer_reasoning_and_ui_are_not_part_of_revision(self):
        reply = 'Thought for 43 seconds\nWe need revise.\n' + marked(REVISED) + '\nDeepThink\nSearch'
        t = Fake(deepseek=[verdict(),verdict(True)],zai=[reply])
        self.run_review(t)
        self.assertEqual(self.script().strip(),REVISED)
        self.assertNotIn('We need revise.',t.calls[2]['prompt'])

    def test_missing_writer_boundaries_get_one_format_retry(self):
        t = Fake(deepseek=[verdict(),verdict(True)],zai=['Thought for 43 seconds\nWe need revise.',marked(REVISED)])
        self.assertEqual(self.run_review(t)['status'],'ready')
        self.assertEqual([c['site'] for c in t.calls],['deepseek','zai','zai','deepseek'])
        self.assertFalse(t.calls[2]['new_chat'])
        with self.assertRaises(ValueError):
            hr.extract_revision('BEGIN_FINAL_SCRIPT\nIncomplete script')

    def test_completion_requires_current_turn_and_complete_answer(self):
        ready = hr.completion_check('reviewer','REPLY_COMPLETE_current')
        self.assertTrue(ready(verdict(True)+'\nREPLY_COMPLETE_current'))
        self.assertFalse(ready(verdict(True)+'\nREPLY_COMPLETE_previous'))
        self.assertFalse(ready('{"decision":"READY"\nREPLY_COMPLETE_current'))
        writer = hr.completion_check('writer','REPLY_COMPLETE_current')
        self.assertTrue(writer(marked(REVISED)+'\nREPLY_COMPLETE_current'))
        self.assertFalse(writer('BEGIN_FINAL_SCRIPT\npartial\nREPLY_COMPLETE_current'))

    def test_ready_in_hidden_block_does_not_wait_for_start_timeout(self):
        from tests.test_webchat import Clock, FakePage
        clk = Clock()
        current = verdict(True)+'\nREPLY_COMPLETE_current'

        class HiddenReply(FakePage):
            def evaluate(self, js, arg=None):
                if js is webchat._STATE_JS:
                    return dict(count=1,text='Claude finished the response',url=self.url)
                return super().evaluate(js,arg)

        page = HiddenReply(clk,webchat.DEEPSEEK,[('',False)]*100)
        page.pre_cands=[verdict(True)+'\nREPLY_COMPLETE_previous']
        page.cands=[current]
        result=webchat.ask(page,webchat.DEEPSEEK,'Review this revised script please',clock=clk,
                           ready=hr.completion_check('reviewer','REPLY_COMPLETE_current'),ready_wait=12)
        self.assertEqual(result,current)
        self.assertLess(clk(),40)

    def test_ready_in_hidden_block_with_stuck_generating_flag_finishes_promptly(self):
        from tests.test_webchat import Clock, FakePage
        clk=Clock()
        current=verdict(True)+'\nREPLY_COMPLETE_current'
        page=FakePage(clk,webchat.DEEPSEEK,[('Footer label',True)]*300)
        page.cands=[current]
        result=webchat.ask(page,webchat.DEEPSEEK,'Review this revised script please',clock=clk,
                           ready=hr.completion_check('reviewer','REPLY_COMPLETE_current'),ready_wait=12)
        self.assertEqual(result,current)
        self.assertLess(clk(),45)

    def test_web_transport_honours_short_completion_wait(self):
        chat=mock.Mock()
        chat.ask.return_value=verdict(True)
        transport=webstages.WebTransport(chat,lambda _:None)
        ready=hr.completion_check('reviewer','REPLY_COMPLETE_current')
        transport.ask('deepseek','Review please',ready=ready)
        self.assertEqual(chat.ask.call_args.kwargs['ready_wait'],12)
        self.assertIs(chat.ask.call_args.kwargs['ready'],ready)


class GPTClaudeGuardTests(__import__('unittest').TestCase):
    def test_legacy_approval_cannot_pass_new_checks(self):
        with self.assertRaises(ValueError):
            hr.validate_checks({'decision':'READY','summary':'Fine','issues':[]})

    def test_accuracy_fail_cannot_be_overruled_by_editorial_pass(self):
        review=json.loads(verdict(True))
        review['checks']['accuracy']='FAIL'
        with self.assertRaises(ValueError): hr.validate_checks(review)

    def test_previous_issue_cannot_silently_disappear(self):
        review=json.loads(verdict(True)); review['resolved_ids']=[]
        with self.assertRaises(ValueError): hr.validate_checks(review,json.loads(verdict()))
        review['resolved_ids']=['I1']
        hr.validate_checks(review,json.loads(verdict()))

    def test_independent_math_check_blocks_false_ready(self):
        review=json.loads(verdict(True))
        review['calculations']=[dict(passage=ORIGINAL,expression='14*20+6.5*20',
            claimed_result=400,decimals=0,working='Twenty workdays; dollars.')]
        out=hr.audit_accuracy(review,ORIGINAL)
        self.assertEqual(out['decision'],'NEEDS_REVISION')
        self.assertEqual(out['checks']['accuracy'],'FAIL')
        self.assertEqual(out['calculations'][0]['computed_result'],410)
        self.assertIn('410',out['issues'][0]['problem'])

    def test_rounding_is_allowed_and_code_execution_is_rejected(self):
        self.assertAlmostEqual(hr.calculate('60000*(1.07**20)'),232181.0677,places=3)
        for expression in ['__import__("os").getcwd()','2**1000000','True+1','1/0','1+']:
            with self.subTest(expression=expression),self.assertRaises(ValueError):
                hr.calculate(expression)

    def test_unverified_evidence_blocks_ready(self):
        review=json.loads(verdict(True))
        review['evidence']=[dict(passage=ORIGINAL,status='unverified',url='',supports='No original source available.')]
        out=hr.audit_accuracy(review,ORIGINAL)
        self.assertEqual(out['decision'],'NEEDS_REVISION')
        self.assertEqual(out['checks']['accuracy'],'FAIL')

    def test_verified_evidence_requires_source_and_actual_quote(self):
        review=json.loads(verdict(True))
        review['evidence']=[dict(passage=ORIGINAL,status='verified',url='',supports='A source supports this.')]
        out=hr.audit_accuracy(review,ORIGINAL)
        self.assertEqual(out['decision'],'NEEDS_REVISION')
        self.assertEqual(out['evidence'][0]['status'],'unverified')
        review['evidence'][0].update(url='https://example.org/source',passage='invented')
        with self.assertRaises(ValueError): hr.audit_accuracy(review,ORIGINAL)

    def test_prompt_covers_retention_purpose_accuracy_and_no_fixed_length(self):
        prompt=hr.prompt('reviewer','Title','Style',ORIGINAL,research='Source material')
        for phrase in ['WHOLE current script','purpose immediately after the hook','pacing',
                       'simple spoken English','No fixed word count','resolved_ids','claimed_result','Source material']:
            self.assertIn(phrase,prompt)


class ReviewRepairTests(__import__('unittest').TestCase):
    def test_retry_preserves_entire_schema_and_does_not_allow_empty_shortcut(self):
        text=hr.review_correction(ValueError('Bad evidence quote'))
        for name in ('checks','resolved_ids','evidence','calculations','issues',
                     'claimed_result','supports','SAME units','never to bypass validation'):
            self.assertIn(name,text)
        self.assertIn('no thinking',text)

    def test_all_shortened_passages_are_identified_in_one_retry(self):
        v=json.loads(verdict())
        v['evidence']=[{'passage':'You can ... each month.'}]
        v['calculations']=[{'passage':'some other missing quote'}]
        with self.assertRaises(ValueError) as failure:
            hr.validate_quotes(v,ORIGINAL)
        self.assertIn('evidence[0].passage',str(failure.exception))
        self.assertIn('calculations[0].passage',str(failure.exception))
        self.assertIn('no paraphrases or inserted ellipses',str(failure.exception))

    def test_repaired_exact_quotes_keep_existing_findings(self):
        v=json.loads(verdict())
        v['evidence']=[dict(passage=ORIGINAL,status='unverified',url='',supports='Needs evidence.')]
        hr.validate_quotes(v,ORIGINAL)
        hr.validate_checks(v)
        self.assertEqual(v['issues'][0]['id'],'I1')


class MissingSourceHandoffTests(__import__('unittest').TestCase):
    def test_missing_url_becomes_actionable_finding_instead_of_exception(self):
        review=json.loads(verdict())
        review['evidence']=[dict(passage=ORIGINAL,status='verified',url='',supports='Model explanation.')]
        out=hr.audit_accuracy(review,ORIGINAL)
        self.assertEqual(out['decision'],'NEEDS_REVISION')
        self.assertEqual(out['checks']['accuracy'],'FAIL')
        self.assertEqual(out['issues'][0]['id'],'I1')
        self.assertIn('without a source URL',out['issues'][-1]['problem'])

    def test_false_ready_with_missing_source_cannot_be_saved_as_done(self):
        review=json.loads(verdict(True))
        review['evidence']=[dict(passage=ORIGINAL,status='verified',url='',supports='Unsupported factual claim.')]
        out=hr.audit_accuracy(review,ORIGINAL)
        self.assertEqual(out['decision'],'NEEDS_REVISION')
        self.assertEqual(out['checks']['accuracy'],'FAIL')


class ComplexCalculationHandoffTests(__import__('unittest').TestCase):
    def test_complex_expression_becomes_fix_request_and_blocks_completion(self):
        review=json.loads(verdict(True))
        review['calculations']=[dict(passage=ORIGINAL,expression='+'.join(['1']*60),
                                    claimed_result=60,decimals=0,working='Sum of sixty units.')]
        out=hr.audit_accuracy(review,ORIGINAL)
        self.assertEqual(out['decision'],'NEEDS_REVISION')
        self.assertEqual(out['calculations'][0]['local_check'],'not_checked')
        self.assertNotIn('computed_result',out['calculations'][0])
        self.assertIn('Split the calculation',out['issues'][0]['suggestion'])


BRIEF = {k: 'Specific reference observation about ' + k for k in hr.REFERENCE_FIELDS}


def delivery_verdict(ready=True):
    v=json.loads(verdict(ready));v['checks']['delivery']='PASS' if ready else 'FAIL'
    return json.dumps(v)


class ReferenceStyleWorkflowTests(Base):
    def setUp(self):
        super().setUp()
        (self.pdir/'script.md').write_text(ORIGINAL,encoding='utf-8')

    def run_new(self, t):
        return hr.run(self.cfg,self.pid,t,'zai','deepseek',log=lambda _:None)

    def transport(self):
        return Fake(deepseek=[json.dumps(BRIEF),delivery_verdict()],zai=[marked(REVISED)])

    def test_all_channels_require_analysis_then_creative_pass_then_review(self):
        t=self.transport();out=self.run_new(t)
        self.assertEqual([c['site'] for c in t.calls],['deepseek','zai','deepseek'])
        self.assertEqual(out['workflow'],'reference-style-v2')
        self.assertEqual(out['status'],'ready')
        self.assertEqual(out['style_rewrites'],1)
        self.assertEqual(out['revisions'],0)
        self.assertEqual((self.pdir/'script.md').read_text().strip(),REVISED)
        self.assertIn('original_reference.txt',t.calls[1]['prompt'])
        self.assertIn('reference_brief.json',t.calls[1]['prompt'])
        self.assertIn('current_script.txt',t.calls[1]['prompt'])
        folder=self.pdir/'human_review'/out['run_id']
        for name in ('reference.txt','reference_brief.json','style-rewrite.txt','ready.txt','original.txt'):
            self.assertTrue((folder/name).exists(),name)
        for name in ('opening-sample.txt','body-sample.txt'):
            self.assertFalse((folder/name).exists(),name)

    def test_correction_prompt_for_claude_has_no_reviewer_instructions(self):
        bad=json.loads(delivery_verdict(False));bad['issues'][0]['passage']='Put a little money aside each month.'
        t=Fake(deepseek=[json.dumps(BRIEF),json.dumps(bad),delivery_verdict()],zai=[marked(REVISED),marked(REVISED+' Again.')])
        self.run_new(t)
        writer=t.calls[3]['prompt'];reviewer=t.calls[2]['prompt']
        self.assertEqual(t.calls[3]['site'],'zai')
        self.assertIn('review.json',writer)
        self.assertNotIn('include delivery as PASS or FAIL',writer)
        self.assertNotIn('Compare the current script against the original',writer)
        self.assertIn('include delivery as PASS or FAIL',reviewer)

    def test_missing_original_stops_before_any_model_call(self):
        (self.pdir/'source_transcript.txt').unlink();t=self.transport()
        with self.assertRaisesRegex(ValueError,'original reference'):self.run_new(t)
        self.assertEqual(t.calls,[])

    def test_missing_style_stops_before_any_model_call(self):
        (self.pdir/'writing_style.md').unlink();t=self.transport()
        with self.assertRaises(ValueError):self.run_new(t)
        self.assertEqual(t.calls,[])

    def test_malformed_analysis_cannot_skip_creative_brief(self):
        t=Fake(deepseek=['Generic style advice'])
        with self.assertRaises(ValueError):self.run_new(t)
        self.assertEqual(len(t.calls),1)
        self.assertEqual(hr.load(self.pdir)['status'],'failed')

    def test_passing_review_saves_directly_and_restore_puts_original_back(self):
        out=self.run_new(self.transport())
        self.assertEqual(hr.load(self.pdir)['status'],'ready')
        state=hr.restore_original(self.cfg,self.pid)
        self.assertEqual(state['status'],'restored')
        self.assertEqual((self.pdir/'script.md').read_text().strip(),ORIGINAL)
        self.assertEqual(hr.load(self.pdir)['status'],'restored')
        self.assertTrue((self.pdir/'human_review'/out['run_id']/'ready.txt').exists())
        with self.assertRaises(ValueError):hr.restore_original(self.cfg,self.pid)

    def test_changed_inputs_or_script_mark_review_outdated(self):
        for filename in ('script.md','source_transcript.txt','writing_style.md'):
            with self.subTest(filename=filename):
                self.run_new(self.transport())
                path=self.pdir/filename
                original=path.read_text(encoding='utf-8');path.write_text(original+' changed',encoding='utf-8')
                self.assertEqual(hr.load(self.pdir)['status'],'outdated')
                path.write_text(original,encoding='utf-8')
                (self.pdir/'script.md').write_text(ORIGINAL,encoding='utf-8')

    def test_promo_rule_in_analyst_writer_and_reviewer_prompts(self):
        t=self.transport();self.run_new(t)
        for call in t.calls:
            self.assertIn('sponsor reads',call['prompt'])

    def test_delivery_must_pass_independently(self):
        v=json.loads(delivery_verdict());v['checks']['delivery']='FAIL'
        t=Fake(deepseek=[json.dumps(BRIEF),json.dumps(v),json.dumps(v)],zai=[marked(REVISED)])
        with self.assertRaises(ValueError):self.run_new(t)
        self.assertEqual(hr.load(self.pdir)['status'],'failed')
        self.assertEqual((self.pdir/'script.md').read_text(),ORIGINAL)

    def test_page_shows_done_and_restore_route(self):
        self.run_new(self.transport());client=create_app(self.cfg).test_client()
        text=client.get(f'/studio/{self.pid}?stage=script').get_data(as_text=True)
        self.assertIn('Done with GPT-Claude',text)
        self.assertIn('Restore original script',text)
        self.assertNotIn('listening',text.lower().replace('addeventlistener',''))
        self.assertIn('Reference analysis brief',text)
        response=client.post(f'/studio/{self.pid}/human-review/restore')
        self.assertEqual(response.status_code,302)
        self.assertEqual(hr.load(self.pdir)['status'],'restored')
        self.assertEqual((self.pdir/'script.md').read_text().strip(),ORIGINAL)


class RoundingRegressionTests(__import__('unittest').TestCase):
    def test_approximate_thousands_do_not_trigger_new_revision(self):
        v=json.loads(verdict(True));quote='It grows to roughly $745,000.'
        v['calculations']=[dict(passage=quote,expression='500*((1+.08/12)**360-1)/(.08/12)',
                                claimed_result=745000,decimals=0,working='Nominal annual rate, monthly deposits.')]
        self.assertEqual(hr.audit_accuracy(v,quote)['decision'],'READY')

    def test_large_timing_error_still_fails(self):
        v=json.loads(verdict(True));quote='It grows to roughly $665,000.'
        v['calculations']=[dict(passage=quote,expression='643192',claimed_result=665000,decimals=0,working='129 months.')]
        self.assertEqual(hr.audit_accuracy(v,quote)['decision'],'NEEDS_REVISION')
