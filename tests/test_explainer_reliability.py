"""Offline regressions for safe scene revisions and honest approval/promotion."""
import copy
import json
import unittest
from unittest.mock import patch

from tests.test_explainer_scenes import ScenePlanTests, plan, SRT
from whisperradar import explainer_scenes as es, studio
from whisperradar.video.explainer import recipes


class RevisionTests(unittest.TestCase):
    def test_patch_retains_other_scenes_exactly(self):
        original = plan()
        changed = copy.deepcopy(original['scenes'][1])
        changed['title'] = 'Will it fit?'
        result = es.merge_revision(original, {'scenes': [changed]}, {'b'})
        self.assertEqual(result['scenes'][0], original['scenes'][0])
        self.assertEqual(result['scenes'][1]['title'], 'Will it fit?')
        self.assertEqual(original['scenes'][1]['title'], 'Fit')

    def test_unrelated_rewrite_unknown_or_duplicate_ids_are_rejected(self):
        p = plan()
        bad = copy.deepcopy(p)
        bad['scenes'][0]['sides'][0]['header'] = 'Changed'
        for reply in [bad, {'scenes': [{'id': 'new'}]}, {'scenes': [p['scenes'][1]] * 2}]:
            with self.assertRaises(ValueError):
                es.merge_revision(p, reply, {'b'})

    def test_scope_names_are_matched_as_whole_ids(self):
        p = {'scenes': [{'id': 'order'}, {'id': 'order-vs'}, {'id': 'last'}]}
        self.assertEqual(es.revision_ids(p, ['scene order-vs: wrong sequence']), {'order-vs'})

    def test_global_guidance_without_a_scene_does_not_open_every_scene(self):
        p = {'scenes': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]}
        self.assertEqual(es.revision_ids(p, ['scene b: unclear', 'overall: reads like a slide deck']), {'b'})

    def test_global_feedback_alone_still_opens_the_plan(self):
        p = {'scenes': [{'id': 'a'}, {'id': 'b'}]}
        self.assertEqual(es.revision_ids(p, ['overall: too many 3-second flashes']), {'a', 'b'})

    def test_self_answered_or_minor_faults_do_not_block_approval(self):
        passing = {'score': 8, 'pass': True, 'faults': [
            'scene hook: expansion is given later, so fine; no fault here — remove',
            'scene tokens: drawn matches with three icons; good',
            'overall: somewhat slide-deck-like, but not a major fault',
            'scene gpu-parallel: label placement could be clearer; minor'],
            'fixes': ['scene gpu-parallel: move the label out of the GPU panel; minor']}
        self.assertTrue(es.review_passes(passing))
        self.assertEqual(es.split_verdict_faults(passing['faults']),
                         ([], ['scene gpu-parallel: label placement could be clearer; minor']))
        self.assertFalse(es.review_passes(dict(passing, faults=['scene b: a spoken item is missing on screen'])))
        self.assertFalse(es.review_passes(dict(passing, fixes=['scene b: add the missing item'])))
        self.assertEqual(es.split_verdict_faults(['scene diffusion: beats exceed run_ms']),
                         (['scene diffusion: beats exceed run_ms'], []))

    def test_praise_words_inside_a_complaint_never_discard_it(self):
        # found in review of prod 115 read-only: the old filter matched 'good'/'fine'
        # anywhere in the last clause and silently dropped these real faults
        complaints = [
            'pipeline-eight: the stage count is wrong, although the colours are good',
            'scene ncm-definition: the explanation of levels is missing; other labels are fine',
            'scene b: the arithmetic is wrong, but the icons look good',
            'scene c: count mismatch, though the wording is fine',
        ]
        self.assertEqual(es.split_verdict_faults(complaints), (complaints, []))
        verdict = {'score': 9, 'pass': True, 'faults': complaints, 'fixes': []}
        self.assertFalse(es.review_passes(verdict))

    def test_minor_only_counts_as_an_explicit_trailing_mark(self):
        blocking, minor = es.split_verdict_faults([
            'scene a: label is long; minor',
            'scene b: the arithmetic is off',
            'scene c: anchor outside the scene',
        ])
        self.assertEqual(blocking, ['scene b: the arithmetic is off', 'scene c: anchor outside the scene'])
        self.assertEqual(minor, ['scene a: label is long; minor'])
        self.assertFalse(es.review_passes({'score': 9, 'pass': True, 'faults': ['scene a: label is long, other labels minor issues aside'], 'fixes': []}))

    def test_fixes_pair_with_their_own_fault_not_by_position(self):
        notes = es._pair_fixes(
            ['scene b: unclear', 'scene a: missing label'],
            ['scene a: add the label', 'scene b: clarify the title'])
        self.assertEqual(notes[0], 'scene b: unclear  FIX: scene b: clarify the title')
        self.assertEqual(notes[1], 'scene a: missing label  FIX: scene a: add the label')

    def test_boundary_correction_can_adjust_neighbour(self):
        self.assertEqual(es.revision_ids(plan(), ['Scene b: anchor outside the scene boundary']), {'a', 'b'})

    def test_high_score_alone_or_with_faults_does_not_mean_approval(self):
        passing = {'score': 9, 'pass': True, 'faults': [], 'fixes': []}
        self.assertTrue(es.review_passes(passing))
        for changes in [{'pass': False}, {'pass': 'true'}, {'score': float('nan')}, {'score': True},
                        {'score': '9'}, {'score': 11}, {'faults': ['Unresolved']}, {'fixes': ['Still change this']}]:
            self.assertFalse(es.review_passes(dict(passing, **changes)))

    def test_duplicate_scene_ids_are_not_passed_to_the_judge(self):
        p = plan()
        p['scenes'][1]['id'] = 'a'
        faults = es.check(p, studio.parse_srt_cues(SRT), 20)
        self.assertIn('unique', faults[0])


class PromotionTests(unittest.TestCase):
    setUp = ScenePlanTests.setUp
    _ask = ScenePlanTests._ask

    def test_verdict_noise_is_dropped_from_the_saved_review(self):
        ask = self._ask([plan()], [{'score': 7, 'pass': False, 'faults': [
            'scene b: unclear', 'scene a: fine; no fault here — remove', 'overall: not a major fault'],
            'fixes': ['scene b: clarify title']}])
        with self.assertRaisesRegex(RuntimeError, 'needs revision'):
            es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=1)
        review = json.loads((self.pdir / 'plan_review.json').read_text())
        self.assertEqual(review['faults'], ['scene b: unclear'])
        self.assertFalse((self.pdir / 'plan.json').exists())

    def test_seed_plan_continues_from_the_saved_plan_instead_of_redrafting(self):
        saved = plan()
        (self.pdir / 'plan.json').write_text(json.dumps(saved), encoding='utf-8')
        judged = []

        def ask(role, text, new_chat):
            judged.append(role)
            return json.dumps({'score': 9, 'pass': True, 'faults': [], 'fixes': []})
        es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=2)
        self.assertEqual(judged, ['judge'])                     # the writer was never asked
        review = json.loads((self.pdir / 'plan_review.json').read_text())
        self.assertEqual(review['status'], 'approved')

    def test_a_scene_revised_twice_and_flagged_again_is_frozen_not_looped(self):
        changed = copy.deepcopy(plan()['scenes'][1])
        changed['title'] = 'Still vague'
        verdict = {'score': 7, 'pass': False, 'faults': ['scene b: too thin'], 'fixes': ['scene b: add a point']}
        ask = self._ask([plan(), {'scenes': [changed]}, {'scenes': [changed]}, {'scenes': [changed]}],
                        [verdict, verdict, verdict])
        logs = []
        with self.assertRaisesRegex(RuntimeError, 'needs revision'):
            es.run_scenes(self.cfg, self.pid, ask, log=logs.append, rounds=6)
        self.assertTrue(any('frozen' in m or 'already-revised' in m for m in logs))

    def test_run_window_covers_generation_beats(self):
        cues = studio.parse_srt_cues('1\n00:00:00,000 --> 00:00:14,000\nFirst the outline appears.\n\n'
                                     '2\n00:00:14,000 --> 00:00:28,000\nThen the gaps are filled.\n\n'
                                     '3\n00:00:28,000 --> 00:00:40,000\nA later part can take shape.\n')
        p = {'scenes': [{'id': 'g', 'cue_range': [1, 3], 'layout': 'generation', 'motion': {'cue': 1},
                        'rows': [{'label': 'Diffusion', 'mode': 'plan_then_fill', 'words': list('abcdefgh'),
                                  'beats': [{'cue': 1}, {'cue': 2}, {'cue': 3}]}]}]}
        params = recipes.build(p, es._cue_objs(cues))['scenes'][0]['remotion']['params']
        self.assertGreaterEqual(params['start_ms'] + params['run_ms'],
                                max(params['rows'][0]['beats_ms']) + 1000)

    def test_below_threshold_saves_candidate_without_replacing_active_plan(self):
        active = self.pdir/'plan.json'
        active.write_text('{"previous":"keep exact bytes"}', encoding='utf-8')
        before = active.read_bytes()
        ask = self._ask([plan()], [{'score': 7, 'pass': False, 'faults': ['b: unclear'], 'fixes': ['b: explain']}])
        with self.assertRaisesRegex(RuntimeError, 'needs revision'):
            es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=1)
        self.assertEqual(active.read_bytes(), before)
        self.assertTrue((self.pdir/'plan_candidate.json').exists())
        review = json.loads((self.pdir/'plan_review.json').read_text())
        self.assertEqual(review['status'], 'needs_revision')
        self.assertTrue(review['active_plan_preserved'])

    def test_approved_revision_is_merged_and_backed_up_before_promotion(self):
        active = self.pdir/'plan.json'
        active.write_text('{"previous":true}', encoding='utf-8')
        changed = copy.deepcopy(plan()['scenes'][1])
        changed['title'] = 'Check fit'
        ask = self._ask([plan(), {'scenes': [changed]}], [
            {'score': 7, 'pass': False, 'faults': ['b: unclear title'], 'fixes': ['b: clarify title']},
            {'score': 9, 'pass': True, 'faults': [], 'fixes': []}])
        result = es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=2)
        self.assertEqual(result['scenes'][0], plan()['scenes'][0])
        self.assertEqual(result['scenes'][1]['title'], 'Check fit')
        backups = list((self.pdir/'backups').glob('*/plan.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{"previous":true}')
        review = json.loads((self.pdir/'plan_review.json').read_text())
        self.assertEqual(review['status'], 'approved')
        self.assertEqual(review['visual_review'], 'not_performed')

    def test_no_automatic_content_deletion_is_used_to_pass_the_checker(self):
        ask = self._ask([plan()], [{'score': 9, 'pass': True, 'faults': [], 'fixes': []}])
        with patch.object(recipes, 'simplify_overfull', side_effect=AssertionError('content removal')):
            es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=1)

    def test_manual_edit_during_judging_is_preserved(self):
        base = self._ask([plan()], [{'score': 9, 'pass': True, 'faults': [], 'fixes': []}])
        def ask(role, text, new_chat):
            if role == 'judge':
                (self.pdir/'plan.json').write_text('manual edit', encoding='utf-8')
            return base(role, text, new_chat)
        with self.assertRaisesRegex(RuntimeError, 'changed during this run'):
            es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=1)
        self.assertEqual((self.pdir/'plan.json').read_text(), 'manual edit')

    def test_unusable_verdict_never_promotes_a_plan(self):
        ask = self._ask([plan()], [{'score': 9}])
        with self.assertRaisesRegex(RuntimeError, 'needs revision'):
            es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=1)
        self.assertFalse((self.pdir/'plan.json').exists())

    def test_approval_is_invalidated_when_the_plan_is_edited(self):
        ask = self._ask([plan()], [{'score': 9, 'pass': True, 'faults': [], 'fixes': []}])
        es.run_scenes(self.cfg, self.pid, ask, log=lambda _: None, rounds=1)
        self.assertEqual(es.load_review(self.pdir)['status'], 'approved')
        changed = json.loads((self.pdir/'plan.json').read_text())
        changed['scenes'][1]['title'] = 'Manual change'
        (self.pdir/'plan.json').write_text(json.dumps(changed), encoding='utf-8')
        self.assertEqual(es.load_review(self.pdir)['status'], 'needs_revision')


class AnimationWindowTests(unittest.TestCase):
    def test_late_stage_extends_default_window_and_reaches_judge(self):
        cues = studio.parse_srt_cues('1\n00:00:00,000 --> 00:00:10,000\nFirst work.\n\n2\n00:00:10,000 --> 00:00:20,000\nSecond stage waits.\n\n3\n00:00:20,000 --> 00:00:30,000\nThen complete it.\n')
        p = {'scenes': [{'id': 'p', 'cue_range': [1,3], 'layout': 'pipeline', 'tasks': 8,
                        'motion': {'cue': 1}, 'stages': [{'label': 'First', 'workers': 8},
                        {'label': 'Second', 'workers': 8, 'waits_for_all': True, 'cue': 3}]}]}
        block = recipes.build(p, es._cue_objs(cues))['scenes'][0]['remotion']
        self.assertGreater(block['params']['run_ms'], 12000)
        self.assertLessEqual(block['params']['start_ms'] + block['params']['run_ms'], 29000)
        summary = es.summary_text(p, cues)
        self.assertIn('20000', summary)
        self.assertIn('COMPILED animation', summary)


if __name__ == '__main__':
    unittest.main()
