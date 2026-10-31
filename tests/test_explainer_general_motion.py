"""General motion layouts (bars, flow, line, grid, split): plan -> compiled Remotion block, checks,
narration anchors inside a subtitle line, key terms and the judge summary."""
import unittest

from whisperradar import explainer_scenes as es
from whisperradar import explainer_terms as et
from whisperradar.video.explainer import recipes, remotion_backend as rb
from whisperradar.video.explainer.validate import ExplainerError


def srt(s):
    return f"00:{int(s // 60):02d}:{int(s % 60):02d},{int(round((s % 1) * 1000)):03d}"


LINES = ["Imagine method A takes 21 seconds.", "Method B takes 1 second.",
         "Your service takes half a second, so 0.5 seconds.",
         "A browser asks the cache first and then the server.", "If the cache misses, the server asks the database.",
         "The page comes back the same way.",
         "Rent takes 1200 of a 3000 dollar salary.", "Food takes 450.", "Savings get 600.", "The rest is left over.",
         "One cell on its own tells you little.", "The whole board of 81 cells tells you everything.",
         "Over 40 years the money keeps rising.", "By 65 it is 213,610 dollars.", "That is compound growth."]
CUES = [{"index": i + 1, "start": srt(i * 4), "end": srt(i * 4 + 3.9), "text": t} for i, t in enumerate(LINES)]


def plan(*scenes):
    return {"scenes": list(scenes)}


BARS = {"id": "t", "start": "Imagine method A", "layout": "bars", "unit": "s", "ratio_to": 1,
        "items": [{"label": "Method A", "value": 21, "at": "method A takes"},
                  {"label": "Method B", "value": 1, "at": "Method B takes"},
                  {"label": "Yours", "value": 0.5, "at": "Your service takes"}]}
FLOW = {"id": "f", "start": "A browser asks", "layout": "flow",
        "nodes": [{"label": "Browser", "icon": "globe", "at": "A browser asks"},
                  {"label": "Cache", "icon": "database", "at": "the cache first"},
                  {"label": "Server", "at": "then the server"}, {"label": "Database", "at": "asks the database"}],
        "sends": [{"label": "request", "back": "page", "at": "The page comes back"}],
        "footer": {"text": "Ask the fast place first", "at": "comes back the same"}}
SPLIT = {"id": "m", "start": "Rent takes 1200", "layout": "split", "prefix": "$", "total": 3000,
         "rest_label": "Left over", "items": [{"label": "Rent", "value": 1200, "at": "Rent takes"},
                                              {"label": "Food", "value": 450, "at": "Food takes"},
                                              {"label": "Savings", "value": 600, "at": "Savings get"}]}
GRID = {"id": "g", "start": "One cell on its", "layout": "grid", "rows": 9, "cols": 9,
        "steps": [{"label": "One cell", "cells": 1, "at": "One cell on"},
                  {"label": "Whole board", "cells": 80, "at": "The whole board"}]}
LINE = {"id": "l", "start": "Over 40 years", "layout": "line", "prefix": "$", "illustrative": True,
        "x": ["25", "35", "45", "55", "65"], "series": [{"label": "Start at 25", "values": [0, 14784, 43865, 101073, 213610]}],
        "motion": {"at": "Over 40 years"}, "marks": [{"x": 5, "text": "$213,610 at 65", "at": "By 65 it"}]}


def compiled(*scenes):
    doc = recipes.build(plan(*scenes), es._cue_objs(CUES))
    return {s["id"]: s["remotion"] for s in doc["scenes"]}


class BuildTests(unittest.TestCase):
    def test_all_five_compile_and_pass_the_component_checks(self):
        out = compiled(BARS, FLOW, SPLIT, GRID, LINE)
        self.assertEqual({k: v["component"] for k, v in out.items()},
                         {"t": "bars", "f": "flow", "m": "split", "g": "grid", "l": "line"})
        for block in out.values():
            self.assertEqual(rb.check_block(block, "x"), [])

    def test_beats_follow_the_narration(self):
        p = compiled(BARS)["t"]["params"]
        self.assertEqual([i["at_ms"] for i in p["items"]], sorted(i["at_ms"] for i in p["items"]))
        self.assertGreaterEqual(p["items"][1]["at_ms"], 3900)      # "Method B takes" is the second line

    def test_words_inside_one_line_land_on_their_words(self):
        nodes = compiled(FLOW)["f"]["params"]["nodes"]
        # "A browser asks" and "the cache first" are in the same subtitle line: not on one frame
        self.assertGreater(nodes[1]["at_ms"] - nodes[0]["at_ms"], 400)
        self.assertNotIn("cue_frac", nodes[1])

    def test_flow_icons_become_paths_and_tokens_wait_for_boxes(self):
        p = compiled(FLOW)["f"]["params"]
        self.assertTrue(p["nodes"][0]["icon_paths"])
        self.assertNotIn("icon_paths", p["nodes"][2])
        self.assertGreaterEqual(p["sends"][0]["at_ms"], p["nodes"][-1]["at_ms"])
        self.assertEqual(p["footer"]["text"], "Ask the fast place first")

    def test_unknown_icon_is_just_dropped(self):
        f = dict(FLOW, nodes=[dict(FLOW["nodes"][0], icon="no-such-icon")] + FLOW["nodes"][1:])
        self.assertNotIn("icon_paths", compiled(f)["f"]["params"]["nodes"][0])

    def test_split_over_total_is_rejected(self):
        bad = dict(SPLIT, total=1000)
        with self.assertRaises(ExplainerError) as cm:
            compiled(bad)
        self.assertIn("more than the total", " ".join(cm.exception.errors))

    def test_grid_overfull_board_is_rejected(self):
        bad = dict(GRID, rows=3, cols=3)
        with self.assertRaises(ExplainerError):
            compiled(bad)

    def test_too_many_beats_for_a_short_scene(self):
        short = dict(BARS, items=BARS["items"] + [{"label": f"x{i}", "value": i, "at": "so 0.5 seconds"} for i in range(1, 4)])
        nxt = {"id": "n", "start": "Your service takes half", "layout": "hero", "items": [{"label": "x"}]}
        with self.assertRaises(ExplainerError):
            compiled(dict(short), nxt)

    def test_simplify_never_drops_a_bar(self):
        p = plan(dict(BARS, footer={"text": "a footer"}))
        out, notes = recipes.simplify_overfull(p, es._cue_objs(CUES))
        self.assertEqual(len(out["scenes"][0]["items"]), 3)


class CheckTests(unittest.TestCase):
    def test_bad_params(self):
        self.assertTrue(rb.check_block({"component": "bars", "params": {"items": [{"label": "a", "value": 1, "at_ms": 0}]}}, "s"))
        self.assertTrue(rb.check_block({"component": "flow", "params": {"nodes": [{"label": "a", "at_ms": 0},
                                                                                  {"label": "b", "at_ms": 0}],
                                                                        "sends": [{"label": "x", "from": 2, "to": 2, "at_ms": 0}]}}, "s"))
        self.assertTrue(rb.check_block({"component": "line", "params": {"x": ["a", "b"], "series": [{"values": [1]}],
                                                                        "start_ms": 0, "run_ms": 3000}}, "s"))

    def test_planner_check_accepts_a_good_plan(self):
        faults = es.check(plan(BARS, FLOW, SPLIT, GRID, LINE), CUES, 60.0)
        self.assertEqual(faults, [])

    def test_planner_check_flags_an_invented_number(self):
        bad = dict(BARS, items=BARS["items"][:2] + [{"label": "Yours", "value": 7, "at": "Your service takes"}])
        faults = es.check(plan(bad, FLOW), CUES, 60.0)
        self.assertTrue(any("7 (Yours)" in f for f in faults), faults)


class ReviewTests(unittest.TestCase):
    def test_terms_see_labels_inside_motion_layouts(self):
        r = recipes.resolve_anchors(plan(FLOW), es._cue_objs(CUES))
        self.assertIn("Database", et.scene_text(r["scenes"][0]))

    def test_judge_summary_describes_the_motion_without_icon_paths(self):
        text = es.summary_text(plan(BARS, FLOW, SPLIT, GRID, LINE), CUES)
        self.assertIn("animated flow of boxes: Browser -> Cache -> Server -> Database", text)
        self.assertIn("returns as 'page'", text)
        self.assertIn("animated bars", text)
        self.assertIn("tagged illustrative", text)
        self.assertNotIn("M 91.7", text)
        self.assertIn("MEASURED MIX: 100%", text)


if __name__ == "__main__":
    unittest.main()


class ShapeOnlyLine(unittest.TestCase):
    def test_shape_flag_reaches_the_component_and_is_checked(self):
        p = compiled(dict(LINE, shape=True))["l"]["params"]
        self.assertIs(p["shape"], True)
        self.assertTrue(rb.check_block({"component": "line", "params": dict(p, shape="yes")}, "s"))
        self.assertIn("shape only", es.summary_text(plan(dict(LINE, shape=True)), CUES))


class AnalogyRows(unittest.TestCase):
    def test_analogy_must_be_a_boolean(self):
        row = {"label": "Broad first", "mode": "plan_then_fill", "words": ["a", "b", "c", "d"]}
        good = {"rows": [dict(row, analogy=True)], "start_ms": 0, "run_ms": 3000}
        self.assertEqual(rb.check_block({"component": "generation", "params": good}, "s"), [])
        bad = {"rows": [dict(row, analogy="yes")], "start_ms": 0, "run_ms": 3000}
        self.assertTrue(rb.check_block({"component": "generation", "params": bad}, "s"))


class NeverAnEmptyScreen(unittest.TestCase):
    CUE_MS = {c.index: (int(round(c.start_seconds * 1000)), int(round(c.end_seconds * 1000))) for c in es._cue_objs(CUES)}

    def test_untitled_pen_scene_draws_from_its_first_moment(self):
        hero = {"id": "h", "start": "Rent takes 1200", "layout": "hero",
                "items": [{"label": "The rest is what is left", "icon": "wallet", "at": "The rest is left"}]}
        doc = recipes.build(plan(hero), es._cue_objs(CUES))
        self.assertLessEqual(recipes.first_visible_ms(doc["scenes"][0], self.CUE_MS), recipes.LEAD_MS)

    def test_titled_scene_is_left_alone(self):
        hero = {"id": "h", "start": "Rent takes 1200", "layout": "hero", "title": "Budget",
                "items": [{"label": "Left over", "icon": "wallet", "at": "The rest is left"}]}
        doc = recipes.build(plan(hero), es._cue_objs(CUES))
        item_ev = [e for e in doc["scenes"][0]["events"] if "title" not in e["target"] and "tline" not in e["target"]][0]
        self.assertEqual(item_ev["at"]["cue"], 10)          # still on its own words

    def test_untitled_motion_scene_with_a_late_start_is_a_fault(self):
        late = {"id": "late", "start": "Rent takes 1200", "layout": "bars", "illustrative": True,
                "items": [{"label": "Food", "value": 450, "at": "Food takes"},
                          {"label": "Savings", "value": 600, "at": "Savings get"}]}
        faults = es.empty_start_faults(recipes.build(plan(late), es._cue_objs(CUES)), CUES)
        self.assertTrue(faults and "nothing is on screen" in faults[0])
        self.assertEqual(es.empty_start_faults(recipes.build(plan(dict(late, title="Budget")), es._cue_objs(CUES)), CUES), [])

    def test_counter_keeps_its_title_and_footer(self):
        c = {"id": "c", "start": "Imagine method A", "layout": "counter", "title": "NCM: 21x faster",
             "counter": {"from": 0, "to": 21, "unit": "x", "label": "faster than what?", "at": "method A takes"},
             "footer": {"text": "Called an LLM killer", "at": "Method B takes"}}
        p = recipes.build(plan(c), es._cue_objs(CUES))["scenes"][0]["remotion"]["params"]
        self.assertEqual(p["title"], "NCM: 21x faster")
        self.assertEqual(p["footer"]["text"], "Called an LLM killer")
        self.assertGreaterEqual(p["footer"]["start_ms"], 3900)
        self.assertEqual(rb.check_block({"component": "counter", "params": p}, "s"), [])


class RebuildAfterBuilderChanges(unittest.TestCase):
    def test_older_builder_output_is_rebuilt(self):
        import json as _json, os, tempfile, time
        from whisperradar.video.explainer import render
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "plan.json"), "w") as fh:
            fh.write("{}")
        time.sleep(0.01)
        out = os.path.join(d, "explainer.json")
        with open(out, "w") as fh:
            _json.dump({"scenes": [], "builder": recipes.BUILDER_VERSION}, fh)
        self.assertFalse(render.needs_build(d))                 # current builder, newer than the plan
        with open(out, "w") as fh:
            _json.dump({"scenes": [], "builder": "an-old-builder"}, fh)
        self.assertTrue(render.needs_build(d))                  # built by an older builder
        with open(out, "w") as fh:
            _json.dump({"scenes": []}, fh)                      # no marker (hand-made/imported): timestamps decide
        self.assertFalse(render.needs_build(d))
