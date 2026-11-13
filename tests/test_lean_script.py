"""Settings > Lean script: the script stage works from a formula only.

The writer gets the title and the formula; the judge gets the draft, the
formula and the channel's script rules. No original, no overlap check.
"""
from __future__ import annotations

from whisperradar import channel_look, settings, studio

FORMULA = "## THE IDEA\nA formula body.\n## NEVER\nCopy anything."


def test_setting_exists_and_is_in_simple_mode_group():
    spec = settings.SPEC_BY_KEY["lean_script"]
    assert spec["type"] == "bool" and spec["default"] is False
    groups = dict(settings.GROUPS)
    assert "lean_script" in groups["Simple mode"]


def test_lean_writer_prompt_uses_formula_only():
    p = studio.script_prompt("Title", "finance", "", style_guide=FORMULA,
                             lean=True)
    assert "FORMULA" in p and "STYLE GUIDE" not in p
    assert "Follow the FORMULA's structure" in p
    assert "invent every name" in p.lower() or "Invent every name" in p
    plain = studio.script_prompt("Title", "finance", "", style_guide=FORMULA)
    assert "STYLE GUIDE" in plain


def test_lean_judge_prompt_has_no_original_or_overlap():
    p = studio.rating_prompt("Title", "finance", "SCRIPT TEXT", "", FORMULA,
                             0.0, lean=True)
    assert "5-gram" not in p
    assert "ORIGINAL (for tone" not in p
    assert "JUDGE AGAINST THE FORMULA" in p
    assert "CHANNEL FORMULA:" in p
    assert "KEYWORDS from the BRIEF" not in p
    normal = studio.rating_prompt("Title", "finance", "SCRIPT TEXT", "brief",
                                  "STYLE", 0.1, original="an original")
    assert "5-gram" in normal and "ORIGINAL (for tone" in normal


def test_formula_prompt_forbids_quotes_and_fixed_numbers(tmp_path):
    videos = []
    for i in range(5):
        f = tmp_path / f"v{i}.txt"
        f.write_text("word " * 400, encoding="utf-8")
        videos.append({"title": f"t{i}", "transcript_path": str(f)})
    p = channel_look.formula_prompt("Chan", "finance", videos)
    assert "DESCRIBE, NEVER QUOTE" in p
    assert "No fixed numbers" in p
    assert p.count("### Video ") >= 5
    assert channel_look.FORMULA_VIDEOS == 5


def test_formula_ready_needs_a_complete_formula():
    assert not channel_look.formula_ready("## short")
    full = "## a\n" * 9 + "checklist never " + "x" * 3100
    assert channel_look.formula_ready(full)
