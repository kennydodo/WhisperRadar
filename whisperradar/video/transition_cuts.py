"""Where a transition sits around its cut (ImgToVideo.Core.Planning.TransitionCuts)."""
from . import models as m


def tail_trim(transition_frames: int, options) -> int:
    return 0 if options.transitions.alignment == m.LATE else transition_frames // 2


def head_trim(transition_frames: int, options) -> int:
    return transition_frames if options.transitions.alignment == m.LATE else (transition_frames + 1) // 2


def compute(clips, options) -> list:
    """Effective transition per cut as [(frames, kind)]; (0, 'none') for a hard cut."""
    cuts = [(0, "none")] * max(0, len(clips) - 1)
    for c in range(len(cuts)):
        tr = clips[c + 1].transition
        if tr is None or tr.kind == "none" or tr.duration_frames < 2:
            continue
        tail = tail_trim(tr.duration_frames, options)
        head = head_trim(tr.duration_frames, options)
        if clips[c].duration_frames - tail < 1 or clips[c + 1].duration_frames - head < 1:
            continue
        cuts[c] = (tr.duration_frames, tr.kind)
    for i in range(len(clips)):
        trim = ((head_trim(cuts[i - 1][0], options) if i > 0 else 0)
                + (tail_trim(cuts[i][0], options) if i < len(cuts) else 0))
        if clips[i].duration_frames - trim < 1:
            if i > 0:
                cuts[i - 1] = (0, "none")
            if i < len(cuts):
                cuts[i] = (0, "none")
    return cuts
