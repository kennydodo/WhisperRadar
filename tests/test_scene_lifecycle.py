"""Scene plan workflow: controlled restructuring, explicit severity, a usable draft lifecycle,
explicit next actions bound to fingerprints, manual acceptance, and what the judge sees."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from tests.test_explainer_scenes import ScenePlanTests, plan, SRT
from whisperradar import autorun, explainer_scenes as es, scene_plan_state as sps, studio
from whisperradar.video.explainer import recipes

BLOCK = {'score': 7, 'pass': False,
         'faults': [{'scene': 'b', 'severity': 'blocking', 'problem': 'title unclear'}],
         'fixes': [{'scene': 'b', 'severity': 'blocking', 'change': 'clarify title'}]}
PASS = {'score': 9, 'pass': True, 'faults': [], 'fixes': []}


def j(x):
    return "```json\n" + json.dumps(x) + "\n```"


def split_b():
    b = plan()['scenes'][1]
    b1 = {'id': 'b1', 'start': 'the reason is whether', 'layout': 'row', 'title': 'Fit',
          'items': copy.deepcopy(b['items'])}
    b2 = {'id': 'b2', 'start': 'so check the file size', 'layout': 'row', 'title': 'Check',
          'items': [{'label': 'File size', 'icon': 'box', 'at': 'check the file size'},
                    {'label': 'Price', 'icon': 'box', 'at': 'before you buy'}]}
    return b1, b2


class Base(unittest.TestCase):
    setUp = ScenePlanTests.setUp

    def cues(self):
        return studio.parse_srt_cues(SRT)

    def runner(self, writer=(), judge=(), stop=None):
        calls = {'writer': iter(writer), 'judge': iter(judge)}
        self.asked = []

        def ask(role, text, new_chat):
            self.asked.append((role, text))
            if stop and stop(len(self.asked)):
                self.stop_now = True
            v = next(calls[role])
            if isinstance(v, Exception):
                raise v
            return j(v) if role == 'writer' else json.dumps(v)
        return ask

    def run_it(self, writer=(), judge=(), start='auto', rounds=1, stop_after=None):
        self.stop_now = False
        ask = self.runner(writer, judge, (lambda n: n >= stop_after) if stop_after else None)
        return es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=rounds, start=start,
                             should_stop=lambda: self.stop_now)

    def needs_revision(self, **kw):
        with self.assertRaises(es.NeedsRevision):
            self.run_it([plan()], [BLOCK], **kw)

    def life(self, running=False):
        return es.lifecycle_for(self.cfg, self.pid, running)


class RestructureTests(unittest.TestCase):
    def cues(self):
        return studio.parse_srt_cues(SRT)

    def test_a_scene_can_be_split_and_unrelated_scenes_stay_identical(self):
        p = plan()
        b1, b2 = split_b()
        out = es.merge_revision(p, {'scenes': [b1, b2], 'remove': ['b']}, {'b'}, {'a', 'b'}, self.cues())
        self.assertEqual([s['id'] for s in out['scenes']], ['a', 'b1', 'b2'])
        self.assertEqual(out['scenes'][0], p['scenes'][0])

    def test_neighbours_can_be_merged(self):
        p = plan()
        merged = {'id': 'ab', 'start': 'two computers can run', 'layout': 'row', 'title': 'Both',
                  'items': copy.deepcopy(p['scenes'][1]['items'])}
        out = es.merge_revision(p, {'scenes': [merged], 'remove': ['a', 'b']}, {'a'}, {'a', 'b'}, self.cues())
        self.assertEqual([s['id'] for s in out['scenes']], ['ab'])

    def test_narration_must_stay_covered(self):
        b1, b2 = split_b()
        b2['items'] = copy.deepcopy(plan()['scenes'][1]['items'])       # keeps the labels, but starts too late
        with self.assertRaisesRegex(ValueError, 'cue'):
            es.merge_revision(plan(), {'scenes': [b2], 'remove': ['b']}, {'b'}, {'a', 'b'}, self.cues())

    def test_explanatory_labels_cannot_be_dropped_by_a_restructure(self):
        b1, b2 = split_b()
        b1['items'] = [b1['items'][0]]                     # 'Memory' disappears
        with self.assertRaisesRegex(ValueError, 'explanatory text'):
            es.merge_revision(plan(), {'scenes': [b1, b2], 'remove': ['b']}, {'b'}, {'a', 'b'}, self.cues())

    def test_only_the_region_may_change(self):
        p = {'scenes': [{'id': 'a', 'cue_range': [1, 1]}, {'id': 'b', 'cue_range': [2, 2]},
                        {'id': 'c', 'cue_range': [3, 3]}, {'id': 'd', 'cue_range': [4, 4]}]}
        region = es.region_ids(p, {'b'})
        self.assertEqual(region, {'a', 'b', 'c'})                   # the named scene and its neighbours
        with self.assertRaisesRegex(ValueError, 'outside'):
            es.merge_revision(p, {'scenes': [], 'remove': ['d']}, {'b'}, region)
        far = dict(p['scenes'][3], title='changed')
        with self.assertRaisesRegex(ValueError, 'Unrelated'):
            es.merge_revision(p, {'scenes': [far, {'id': 'x', 'cue_range': [2, 2]}], 'remove': ['b']}, {'b'}, region)
        near = dict(p['scenes'][0], title='changed')                # no restructure: a neighbour stays fixed
        with self.assertRaisesRegex(ValueError, 'Unrelated'):
            es.merge_revision(p, {'scenes': [near]}, {'b'}, region)

    def test_removal_is_refused_without_a_region(self):
        with self.assertRaises(ValueError):
            es.merge_revision(plan(), {'scenes': [], 'remove': ['b']}, {'b'})

    def test_the_prompt_offers_restructuring(self):
        text = es.fix_prompt(plan(), ['scene b: too crowded; split it'], 'narration')
        for needle in ('SPLIT', 'MERGE', 'RESTRUCTURE REGION', '"remove"'):
            self.assertIn(needle, text)

    def test_automatic_simplification_removes_decoration_only(self):
        srt = "".join(f"{i}\n00:00:{(i - 1) * 4:02d},000 --> 00:00:{i * 4 - 1:02d},800\nWords number {i} are said here.\n\n"
                      for i in range(1, 7))
        cues = es._cue_objs(studio.parse_srt_cues(srt))
        p = {"scenes": [
            {"id": "a", "start": "words number 1", "layout": "hero", "title": "A", "items": [{"label": "First point", "at": "words number 1"}]},
            {"id": "b", "start": "words number 2", "layout": "row", "title": "B", "items": [
                {"label": n, "at": "words number 2"} for n in ("One", "Two", "Three", "Four")]},
            {"id": "c", "start": "words number 3", "layout": "hero", "title": "C", "items": [{"label": "Third point", "at": "words number 3"}]}]}
        fixed, notes = recipes.simplify_overfull(p, cues, decoration_only=True)
        self.assertEqual([s['id'] for s in fixed['scenes']], ['a', 'b', 'c'])
        self.assertEqual(fixed['scenes'][1]['items'], p['scenes'][1]['items'])
        self.assertFalse([n for n in notes if 'removed (' in n or 'footer' in n or 'last item' in n])


class RestructureRunTests(Base):
    def test_a_merge_revision_is_accepted_in_a_run(self):
        merged = {'id': 'ab', 'start': 'two computers can run', 'layout': 'row', 'title': 'Both',
                  'items': copy.deepcopy(plan()['scenes'][1]['items'])}
        verdict = {'score': 7, 'pass': False,
                   'faults': [{'scene': 'a', 'severity': 'blocking', 'problem': 'too short; merge it with the next scene'}],
                   'fixes': [{'scene': 'a', 'severity': 'blocking', 'change': 'merge a and b'}]}
        out = self.run_it([plan(), {'scenes': [merged], 'remove': ['a', 'b']}], [verdict, PASS], rounds=3)
        self.assertEqual([s['id'] for s in out['scenes']], ['ab'])


class SeverityTests(Base):
    def test_ambiguous_or_contradictory_verdicts_are_listed(self):
        self.assertTrue(es.verdict_problems(dict(PASS, faults=['scene b: fine'])))                   # string
        self.assertTrue(es.verdict_problems(dict(PASS, faults=[{'scene': 'b', 'problem': 'x'}])))    # none
        self.assertTrue(es.verdict_problems(dict(PASS, faults=[{'scene': 'b', 'severity': 'low', 'problem': 'x'}])))
        self.assertTrue(es.verdict_problems(dict(PASS, fixes=[{'scene': 'b', 'change': 'x'}])))
        blocking = {'scene': 'b', 'severity': 'blocking', 'problem': 'x'}
        self.assertTrue(es.verdict_problems(dict(PASS, faults=[blocking])))                           # pass + blocking
        self.assertTrue(es.verdict_problems(dict(PASS, score=7)))                                     # pass + low score
        self.assertTrue(es.verdict_problems({'score': 9, 'pass': False, 'faults': [], 'fixes': []}))  # nothing to change
        self.assertEqual(es.verdict_problems(BLOCK), [])
        self.assertEqual(es.verdict_problems(PASS), [])

    def test_minor_suggestions_do_not_block_but_blocking_and_unmarked_ones_do(self):
        minor = dict(PASS, faults=[{'scene': 'b', 'severity': 'minor', 'problem': 'colour'}],
                     fixes=[{'scene': 'b', 'severity': 'minor', 'change': 'recolour'}])
        self.assertTrue(es.review_passes(minor))
        self.assertFalse(es.review_passes(dict(minor, fixes=[{'scene': 'b', 'severity': 'blocking', 'change': 'x'}])))
        self.assertFalse(es.review_passes(dict(minor, fixes=[{'scene': 'b', 'change': 'x'}])))        # never minor by default
        self.assertFalse(es.review_passes(dict(minor, fixes=['scene b: recolour; minor'])))

    def test_an_unmarked_verdict_is_sent_back_and_the_clarified_one_decides(self):
        unmarked = dict(PASS, fixes=['scene b: nice to have'])
        out = self.run_it([plan()], [unmarked, dict(PASS, fixes=[{'scene': 'b', 'severity': 'minor', 'change': 'nice'}])])
        self.assertTrue(out['scenes'])
        self.assertEqual([r for r, _ in self.asked], ['writer', 'judge', 'judge'])
        self.assertIn('no explicit severity', self.asked[2][1])
        self.assertEqual(json.loads((self.pdir / 'plan_review.json').read_text())['status'], 'approved')

    def test_a_verdict_that_stays_contradictory_never_approves_and_is_not_guessed(self):
        bad = dict(PASS, faults=[{'scene': 'b', 'severity': 'blocking', 'problem': 'x'}])
        with self.assertRaises(es.NeedsRevision):
            self.run_it([plan()], [bad, bad], rounds=3)
        self.assertEqual([r for r, _ in self.asked], ['writer', 'judge', 'judge'])      # the writer was not sent off
        self.assertFalse((self.pdir / 'plan.json').exists())
        review = json.loads((self.pdir / 'plan_candidate_review.json').read_text())
        self.assertTrue(review['verdict_problems'])
        self.assertEqual(review['status'], 'needs_revision')


class LifecycleTests(Base):
    def api(self, replies=None):
        """An API pair: the writer drafts once and answers revisions with a small patch, the judge always blocks."""
        it = iter(replies or [])

        def gen(cfg, text, provider=None):
            if replies is not None:
                v = next(it)
                if isinstance(v, Exception):
                    raise v
                return j(v)
            if 'CURRENT PLAN' in text:
                scene = dict(plan()['scenes'][1], title='Fit %d' % len(text))
                return j({'scenes': [scene]})
            return json.dumps(BLOCK) if '"severity"' in text else j(plan())
        return patch.object(studio, 'llm_generate', gen)

    def test_a_draft_below_the_bar_is_needs_revision_not_a_failure(self):
        with self.api(), self.assertRaises(es.NeedsRevision):
            es.api_job(self.cfg, self.pid, 'p', 'p', lambda _: None, start='fresh')
        life = self.life()
        self.assertEqual((life['state'], life['label']), ('needs_revision', 'Needs revision'))
        self.assertEqual(life['run']['status'], 'needs_revision')
        self.assertTrue(life['can_accept'])
        self.assertFalse((self.pdir / 'plan.json').exists())

    def test_a_connection_problem_is_transport_failed(self):
        with self.api([RuntimeError('connection reset')]), self.assertRaises(RuntimeError) as cm:
            es.api_job(self.cfg, self.pid, 'p', 'p', lambda _: None)
        self.assertNotIsInstance(cm.exception, es.NeedsRevision)
        life = self.life()
        self.assertEqual((life['state'], life['label']), ('transport_failed', 'Transport failed'))

    def test_stopped_is_its_own_state_and_the_draft_survives(self):
        flag = {'stop': False}

        def gen(cfg, text, provider=None):
            flag['stop'] = True                               # the user presses Stop during the writer's reply
            return j(plan())
        with patch.object(studio, 'llm_generate', gen), self.assertRaisesRegex(RuntimeError, 'Stopped by you'):
            es.api_job(self.cfg, self.pid, 'p', 'p', lambda _: None, should_stop=lambda: flag['stop'])
        life = self.life()
        self.assertEqual((life['state'], life['label']), ('stopped', 'Stopped'))
        self.assertTrue(life['can_continue'])
        self.assertFalse((self.pdir / 'plan.json').exists())

    def test_running_and_approved(self):
        self.assertEqual(self.life(True)['state'], 'running')
        self.run_it([plan()], [PASS])
        life = self.life()
        self.assertEqual((life['state'], life['label']), ('approved', 'Approved'))

    def test_the_page_names_the_state_offers_preview_and_accept(self):
        from whisperradar.webapp import create_app
        self.needs_revision()
        (self.pdir / 'explainer.json').write_text('{}', encoding='utf-8')
        html = create_app(self.cfg).test_client().get(f'/studio/{self.pid}?stage=shots').get_data(as_text=True)
        for needle in ('Needs revision', 'Accept candidate', 'Preview the draft', 'Continue latest draft',
                       'Revise active plan', 'Start fresh', 'name="ack"'):
            self.assertIn(needle, html)
        self.assertNotIn('Transport failed', html)


class AcceptTests(Base):
    def test_acceptance_needs_acknowledgement_and_is_never_called_approved(self):
        self.needs_revision()
        with self.assertRaisesRegex(es.AcceptError, 'understand'):
            es.accept_candidate(self.cfg, self.pid, False)
        self.assertFalse((self.pdir / 'plan.json').exists())
        shown = self.life()['candidate']['digest']
        done = es.accept_candidate(self.cfg, self.pid, True, shown)
        self.assertEqual(done['status'], 'manually_accepted')
        self.assertFalse(done['judge_approved'])
        self.assertEqual(sps.read_json(self.pdir / 'plan.json'), sps.read_json(self.pdir / 'plan_candidate.json'))
        review = sps.read_json(self.pdir / 'plan_review.json')
        self.assertEqual(review['status'], 'manually_accepted')
        self.assertTrue(review['acknowledged_findings'])
        row = [json.loads(x) for x in (self.pdir / 'plan_audit.jsonl').read_text().splitlines()][-1]
        self.assertEqual((row['event'], row['judge_approved']), ('manual_accept', False))
        life = self.life()
        self.assertEqual((life['state'], life['label']), ('manually_accepted', 'Manually accepted'))

    def test_the_previous_plan_is_backed_up(self):
        old = {'scenes': [{'id': 'o'}], 'marker': 'old'}
        (self.pdir / 'plan.json').write_text(json.dumps(old), encoding='utf-8')
        self.needs_revision(start='fresh')
        es.accept_candidate(self.cfg, self.pid, True)
        saved = list((self.pdir / 'backups').glob('scene-plan-[0-9]*/plan.json'))
        self.assertEqual([json.loads(p.read_text()) for p in saved], [old])

    def test_a_second_acceptance_changes_nothing(self):
        self.needs_revision()
        es.accept_candidate(self.cfg, self.pid, True)
        audit = (self.pdir / 'plan_audit.jsonl').read_text()
        with self.assertRaisesRegex(es.AcceptError, 'already'):
            es.accept_candidate(self.cfg, self.pid, True)
        self.assertEqual((self.pdir / 'plan_audit.jsonl').read_text(), audit)

    def test_stale_candidates_are_refused(self):
        self.needs_revision()
        (self.pdir / 'subtitles.srt').write_text(SRT.replace('Two computers', 'Three computers'), encoding='utf-8')
        self.assertTrue(self.life()['candidate']['stale'])
        with self.assertRaisesRegex(es.AcceptError, 'narration|subtitles'):
            es.accept_candidate(self.cfg, self.pid, True)

    def test_an_active_plan_that_appeared_later_makes_the_draft_stale(self):
        self.needs_revision()
        (self.pdir / 'plan.json').write_text(json.dumps({'scenes': [{'id': 'x'}]}), encoding='utf-8')
        with self.assertRaisesRegex(es.AcceptError, 'stale'):
            es.accept_candidate(self.cfg, self.pid, True)

    def test_a_hand_edited_draft_is_refused_and_so_is_one_that_changed_since_the_preview(self):
        self.needs_revision()
        shown = self.life()['candidate']['digest']
        with self.assertRaisesRegex(es.AcceptError, 'changed since you previewed'):
            es.accept_candidate(self.cfg, self.pid, True, 'not-the-digest')
        cand = sps.read_json(self.pdir / 'plan_candidate.json')
        cand['scenes'][1]['title'] = 'Edited by hand'
        sps.write_json(self.pdir / 'plan_candidate.json', cand)
        with self.assertRaisesRegex(es.AcceptError, 'edited'):
            es.accept_candidate(self.cfg, self.pid, True, shown)

    def test_technical_validation_is_required(self):
        self.needs_revision()
        cand = sps.read_json(self.pdir / 'plan_candidate.json')
        cand['scenes'][1]['items'][0]['at'] = 'words nobody says'
        sps.write_json(self.pdir / 'plan_candidate.json', cand)
        review = sps.read_json(self.pdir / 'plan_candidate_review.json')
        review['plan_digest'] = sps.plan_digest(cand)
        sps.write_json(self.pdir / 'plan_candidate_review.json', review)
        with self.assertRaisesRegex(es.AcceptError, 'Technical validation'):
            es.accept_candidate(self.cfg, self.pid, True)
        self.assertFalse((self.pdir / 'plan.json').exists())

    def test_route(self):
        from whisperradar.webapp import create_app
        self.needs_revision()
        client = create_app(self.cfg).test_client()
        client.post(f'/studio/{self.pid}/scenes/accept', data={})
        self.assertFalse((self.pdir / 'plan.json').exists())
        client.post(f'/studio/{self.pid}/scenes/accept', data={'ack': 'yes'})
        self.assertTrue((self.pdir / 'plan.json').exists())
        again = client.post(f'/studio/{self.pid}/scenes/accept', data={'ack': 'yes'}, follow_redirects=True)
        self.assertIn(b'already the active plan', again.data)


class StartModeTests(Base):
    def test_continue_resumes_the_draft_without_asking_the_writer(self):
        self.needs_revision()
        self.run_it([], [PASS], start='continue')
        self.assertEqual([r for r, _ in self.asked], ['judge'])
        self.assertEqual(self.life()['state'], 'approved')

    def test_the_draft_is_chosen_by_fingerprint_not_by_file_time(self):
        (self.pdir / 'plan.json').write_text(json.dumps(plan()), encoding='utf-8')
        self.needs_revision(start='revise_active')
        os.utime(self.pdir / 'plan_candidate.json', (1, 1))               # the draft looks ancient
        self.run_it([], [PASS], start='continue')
        self.assertEqual([r for r, _ in self.asked], ['judge'])

    def test_a_stale_draft_cannot_be_continued(self):
        self.needs_revision()
        (self.pdir / 'subtitles.srt').write_text(SRT.replace('quickly', 'slowly'), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.run_it([], [PASS], start='continue')

    def test_a_manual_edit_of_the_active_plan_makes_the_draft_stale(self):
        (self.pdir / 'plan.json').write_text(json.dumps(plan()), encoding='utf-8')
        self.needs_revision(start='revise_active')
        edited = plan()
        edited['scenes'][1]['title'] = 'Manually changed'
        (self.pdir / 'plan.json').write_text(json.dumps(edited), encoding='utf-8')
        self.assertTrue(self.life()['candidate']['stale'])
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.run_it([], [PASS], start='continue')

    def test_revise_active_starts_from_plan_json(self):
        active = plan()
        active['scenes'][1]['title'] = 'Active title'
        (self.pdir / 'plan.json').write_text(json.dumps(active), encoding='utf-8')
        self.run_it([], [PASS], start='revise_active')
        self.assertEqual([r for r, _ in self.asked], ['judge'])
        self.assertIn('Active title', self.asked[0][1])
        (self.pdir / 'plan.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'no active plan'):
            self.run_it([], [PASS], start='revise_active')

    def test_fresh_clears_generated_results_but_keeps_the_active_plan(self):
        active = plan()
        (self.pdir / 'plan.json').write_text(json.dumps(active), encoding='utf-8')
        self.needs_revision(start='revise_active')
        self.assertTrue((self.pdir / 'plan_candidate.json').exists())
        with self.assertRaises(es.NeedsRevision):
            self.run_it([plan()], [BLOCK], start='fresh')
        self.assertEqual([r for r, _ in self.asked][0], 'writer')       # a new draft, not the old one
        self.assertEqual(len(list((self.pdir / 'backups').glob('scene-plan-fresh-*/plan_candidate.json'))), 1)
        self.assertEqual(json.loads((self.pdir / 'plan.json').read_text()), active)
        events = [json.loads(x)['event'] for x in (self.pdir / 'plan_audit.jsonl').read_text().splitlines()]
        self.assertIn('fresh_start', events)


class CancellationAndTransportTests(Base):
    def test_stopping_never_touches_the_active_plan_and_keeps_the_draft(self):
        active = json.dumps(plan())
        (self.pdir / 'plan.json').write_text(active, encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Stopped by you'):
            self.run_it([plan()], [BLOCK], start='fresh', stop_after=1)
        self.assertEqual((self.pdir / 'plan.json').read_text(), active)
        self.assertEqual(sps.read_json(self.pdir / 'plan_candidate_review.json')['status'], 'in_progress')

    def test_a_transport_error_keeps_the_draft_for_continue(self):
        with self.assertRaisesRegex(RuntimeError, 'connection'):
            self.run_it([plan()], [RuntimeError('connection lost')])
        self.assertEqual(sps.read_json(self.pdir / 'plan_candidate_review.json')['status'], 'in_progress')
        self.run_it([], [PASS], start='continue')
        self.assertEqual(self.life()['state'], 'approved')


class AutoRunTests(Base):
    def test_an_unapproved_draft_pauses_auto_run(self):
        with patch.object(autorun, 'explainer_production', return_value=True):
            self.needs_revision()
            act = autorun.stage_action(self.cfg, self.pid, 'shots')
        self.assertEqual(act['action'], 'pause')
        self.assertIn('never advances', act['detail'])

    def test_needs_revision_pauses_the_stage_instead_of_failing_or_advancing(self):
        with patch.object(autorun, 'explainer_production', return_value=True), \
                patch.object(studio, 'judge_provider', return_value='p'), \
                patch.object(es, 'api_job', side_effect=es.NeedsRevision('Scene plan needs revision: x')):
            result = autorun.run_stage(self.cfg, self.pid, 'shots', {})
        self.assertTrue(result.startswith('paused:'), result)


class JudgeSeesTheContentTests(unittest.TestCase):
    """The compiled animation is added to what the judge sees, never a replacement for it."""
    def test_summary_lists_stages_rows_code_highlights_and_the_narration(self):
        text = es.summary_text(
            {"scenes": [
                {"id": "g", "cue_range": [1, 1], "layout": "generation", "title": "Orders", "rows": [
                    {"label": "Left to right", "mode": "sequential", "words": ["Hi", "team", "meeting", "Thursday"]},
                    {"label": "Plan first", "mode": "plan_then_fill", "words": ["Hi", "team", "meeting", "Thursday"],
                     "beats": [{"at": "a broad outline"}]}]},
                {"id": "p", "cue_range": [1, 1], "layout": "pipeline", "title": "Waiting", "tasks": 8, "stages": [
                    {"label": "Stage 1", "workers": 8},
                    {"label": "Stage 2", "workers": 4, "waits_for_all": True, "at": "needs the result"}]},
                {"id": "c", "cue_range": [1, 1], "layout": "code", "title": "Code", "filename": "a.py",
                 "lines": ["x = 1", "y = 2"], "highlights": [{"line": 2, "note": "the batch size"}]}]},
            studio.parse_srt_cues("1\n00:00:00,000 --> 00:00:30,000\nHello there, a broad outline needs the result today. "
                                  "Keep the final important condition.\n\n"))
        for needle in ("Left to right", "one word after another", "a rough outline first", "a broad outline",
                       "Stage 2 (4 workers, locked", "needs the result", "a.py", "the batch size",
                       "COMPILED animation", "start_ms", "Keep the final important condition."):
            self.assertIn(needle, text)


if __name__ == '__main__':
    unittest.main()
