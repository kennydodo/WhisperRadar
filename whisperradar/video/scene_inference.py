"""Scene windows from subtitle gaps (ImgToVideo.Core.Analysis.SceneInference)."""
from . import models as m
from .options import SceneInferenceOptions

_TERMINAL = (".", "!", "?", "…")


def infer(subtitles, options: SceneInferenceOptions) -> list:
    windows: list = []
    if not subtitles:
        return windows
    group_start = group_end = subtitles[0]
    for block in subtitles[1:]:
        gap = block.start_seconds - group_end.end_seconds
        text = group_end.text
        ends_sentence = len(text) > 0 and len(text.rstrip()) > 0 and text.rstrip()[-1] in _TERMINAL
        split = (gap > options.sentence_gap_seconds
                 or (ends_sentence and gap > options.terminal_punctuation_gap_seconds))
        if split:
            windows.append(m.SceneWindow("", group_start.start_seconds, group_end.end_seconds))
            group_start = block
        group_end = block
    windows.append(m.SceneWindow("", group_start.start_seconds, group_end.end_seconds))
    return windows
