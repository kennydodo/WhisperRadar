"""Where a scene plan stands: fingerprints, candidate freshness, lifecycle label, audit trail.

Files in the production folder:
  plan.json                   the ACTIVE plan (what renders)
  plan_review.json            the review of the active plan (approved / manually_accepted)
  plan_candidate.json         the latest machine draft
  plan_candidate_review.json  its review, bound to the narration, the plan inputs and the active
                              plan it was started from
  scene_plan_run.json         how the last run ended (running / approved / needs_revision /
                              stopped / failed = transport failure / manually_accepted)
  plan_audit.jsonl            one line per approval, manual acceptance or fresh start
"""
import hashlib
import json
import os
import time
from pathlib import Path

LIFECYCLE_LABELS = {
    "running": "Running",
    "needs_revision": "Needs revision",
    "approved": "Approved",
    "manually_accepted": "Manually accepted",
    "stopped": "Stopped",
    "transport_failed": "Transport failed",
    "legacy": "Plan present (no review recorded)",
    "none": "No scene plan yet",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_digest(plan) -> str:
    return _sha(json.dumps(plan, sort_keys=True, ensure_ascii=False))


def narration_fingerprint(narration_text: str) -> str:
    return _sha(narration_text)


def inputs_fingerprint(direction: str, look_text: str, title: str = "") -> str:
    return _sha(json.dumps([direction or "", look_text or "", title or ""], ensure_ascii=False))


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def active_digest(pdir):
    plan = read_json(Path(pdir) / "plan.json")
    return plan_digest(plan) if plan is not None else None


def audit(pdir, event: str, **fields) -> dict:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    with open(Path(pdir) / "plan_audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def candidate_state(pdir, narration_fp: str, inputs_fp: str) -> dict:
    """-> {exists, status, stale, reason, review, plan, digest}.
    status: needs_revision | in_progress | approved | accepted | none.
    A candidate is stale when the narration or the plan inputs changed since it was drafted,
    when the file was edited after its review, or when the active plan changed since it began."""
    pdir = Path(pdir)
    plan = read_json(pdir / "plan_candidate.json")
    review = read_json(pdir / "plan_candidate_review.json")
    if plan is None or review is None:
        return {"exists": False, "status": "none", "stale": False, "reason": "", "review": review or {},
                "plan": None, "digest": None}
    digest = plan_digest(plan)
    reasons = []
    if review.get("narration_fp") != narration_fp:
        reasons.append("the narration/subtitles changed since this draft")
    if review.get("input_fp") != inputs_fp:
        reasons.append("the writer's inputs (direction or channel look) changed since this draft")
    if review.get("plan_digest") != digest:
        reasons.append("the draft file was edited after it was reviewed")
    if review.get("base_digest") != active_digest(pdir):
        reasons.append("the active plan.json changed since this draft was started")
    return {"exists": True, "status": review.get("status") or "needs_revision", "stale": bool(reasons),
            "reason": "; ".join(reasons), "review": review, "plan": plan, "digest": digest}


def lifecycle(pdir, narration_fp: str, inputs_fp: str, job_running: bool = False) -> dict:
    """The one state the page shows."""
    pdir = Path(pdir)
    run = read_json(pdir / "scene_plan_run.json") or {}
    cand = candidate_state(pdir, narration_fp, inputs_fp)
    active = read_json(pdir / "plan.json")
    review = read_json(pdir / "plan_review.json") or {}
    active_ok = bool(active is not None and review.get("status") in ("approved", "manually_accepted")
                     and review.get("plan_digest") == plan_digest(active))
    rs = run.get("status")
    if job_running:
        state = "running"
    elif rs in ("stopped", "needs_revision", "manually_accepted"):
        state = rs
    elif rs == "failed":
        state = "transport_failed"
    elif rs == "running":
        state = "stopped"                       # the app died mid-run
    elif rs is None and cand["exists"] and cand["status"] == "needs_revision" and not cand["stale"]:
        state = "needs_revision"
    elif active_ok:
        state = review["status"]
    elif active is not None:
        state = "legacy"
    else:
        state = "none"
    if state in ("approved", "manually_accepted") and not active_ok:
        state = "legacy" if active is not None else "none"
    if state == "needs_revision" and not cand["exists"]:
        state = ("approved" if review.get("status") == "approved" else
                 "manually_accepted" if review.get("status") == "manually_accepted" else
                 "legacy") if active is not None else "none"
        if state in ("approved", "manually_accepted") and not active_ok:
            state = "legacy"
    findings = []
    if cand["exists"]:
        r = cand["review"]
        findings = list(r.get("checker_faults_left") or []) + list(r.get("faults") or [])
    return {
        "state": state, "label": LIFECYCLE_LABELS.get(state, state), "candidate": cand,
        "active_review": review if active is not None else {}, "run": run, "findings": findings,
        "can_accept": bool(cand["exists"] and cand["status"] == "needs_revision"
                           and not cand["stale"] and not job_running),
        "can_continue": bool(cand["exists"] and not cand["stale"]
                             and cand["status"] in ("needs_revision", "in_progress")),
        "can_revise_active": active is not None,
    }
