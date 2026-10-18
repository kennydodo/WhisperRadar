"""Local, advisory pre-audio scan. Flags are review prompts, not fact checks."""
from __future__ import annotations
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

VERSION = 1
REPORT = "script_readiness.json"
REVIEW = "script_warning_review.json"
_review_lock = threading.RLock()


def record_review(folder, script_hash, assessments):
    """Store verdicts separately from raw heuristic findings; script edits expire them."""
    with _review_lock:
        path = Path(folder) / REVIEW
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            review = {}
        if review.get("script_hash") != script_hash:
            review = {"script_hash": script_hash, "assessments": {}}
        for item in assessments:
            review["assessments"][str(item["finding"])] = item
        _write(path, review)


def _reviewed(folder, report):
    # Copy so reviewed severity is never persisted into the raw scanner report.
    report = json.loads(json.dumps(report))
    with _review_lock:
        try:
            review = json.loads((Path(folder) / REVIEW).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            review = {}
    cleared = 0
    if review.get("script_hash") == report["script_hash"]:
        for i, finding in enumerate(report["findings"]):
            verdict = review.get("assessments", {}).get(str(i), {})
            if verdict.get("verdict") == "not_an_issue":
                cleared += finding["severity"] == "warning"
                finding["severity"] = "reviewed"
                finding["review_reason"] = verdict.get("reason", "")
    report["reviewed_count"] = cleared
    report["warning_count"] = sum(f["severity"] == "warning" for f in report["findings"])
    return report


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan(text):
    findings = []
    def flag(category, reason, match, severity="warning"):
        start = match.start()
        left = text.rfind("\n", 0, start) + 1
        right = text.find("\n", match.end())
        quote = text[left:right if right >= 0 else len(text)].strip()
        findings.append({"category": category, "severity": severity,
                         "line": text.count("\n", 0, start) + 1,
                         "passage": quote[:650], "reason": reason})
    rules = [
        ("Advertisement", r"\b(?:sponsored by|sponsor(?:ed|ship)?|promo code|discount code|affiliate link|free trial|use (?:my|our) code|link in (?:the )?description)\b", "Possible commercial promotion; check context before narration."),
        ("Promotional segue", r"\b(?:watch (?:this|the) next|next video|another video on (?:the|this) channel|course (?:called|named)|today.s partner)\b", "Check that the promoted video/product exists and that this segue belongs in your script."),
        ("Link", r"https?://[^\s]+|www\.[^\s]+", "A URL may be source material or an advertisement; decide whether it should be spoken."),
        ("Drafting residue", r"\b(?:as an ai|here(?:.s| is) (?:the|your) (?:revised |final )?script|write a message|claude responded|insert (?:sponsor|cta)|TODO|\[placeholder\])\b", "Possible non-narration drafting text; remove or confirm it.", "warning"),
        ("Strong factual claim", r"\b(?:always|never|every single|guaranteed|for the first time|oldest|nothing in the machinery differs|spoilage[^.\n]{0,35}stops)\b", "Absolute or historical priority claim: qualify it or verify the supporting evidence."),
        ("Calculation / assumption", r"\b(?:percent|per cent|compounding|million|billion|times (?:EBITDA|earnings)|minimum payment|\d[\d,.]*\s*%|\d[\d,.]* years (?:old|ago))\b", "Check figures, units, dates and assumptions. This scanner does not validate arithmetic or sources."),
        ("CTA", r"\b(?:subscribe|share this|(?:tell me|name it|let me know)[^.\n]{0,80}comments)\b", "Normal audience call to action; retain or remove according to channel preference.", "info"),
    ]
    for rule in rules:
        cat, pattern, reason, *severity = rule
        for match in re.finditer(pattern, text, re.I):
            flag(cat, reason, match, severity[0] if severity else "warning")
    labels = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    previous = {}
    for match in re.finditer(r"\b(habit|step|weapon)\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", text, re.I):
        kind, raw = match.group(1).lower(), match.group(2).lower()
        number = labels.get(raw, int(raw) if raw.isdigit() else 0)
        prior = previous.get(kind)
        if prior and number > prior + 1:
            flag("Numbering", f"{kind.title()} numbering jumps from {prior} to {number}; check missing items.", match)
        previous[kind] = number
    ordinal = {"first": 1, "second": 2, "third": 3, "fourth": 4,
               "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}
    prev_ordinal = {}
    for match in re.finditer(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth)\s+(?:human\s+)?(weapon|step|habit)\b", text, re.I):
        number, kind = ordinal[match.group(1).lower()], match.group(2).lower()
        prior = prev_ordinal.get(kind)
        if prior and number > prior + 1:
            flag("Numbering", f"{kind.title()} numbering jumps from {prior} to {number}; check missing items.", match)
        prev_ordinal[kind] = number
    # Chinese drafting leftovers were encountered in an earlier production.
    for match in re.finditer(r"[\u3400-\u9fff]{3,}", text):
        flag("Language check", "Non-English passage: confirm this is intentional narration, not drafting residue.", match)
    findings.sort(key=lambda f: (f["line"], f["category"]))
    return {"version": VERSION, "script_hash": digest(text), "findings": findings,
            "warning_count": sum(f["severity"] == "warning" for f in findings),
            "ignored": False}


def _write(path, report):
    # Unique temp file + replace prevents readers seeing partially written JSON.
    fd, temp = tempfile.mkstemp(prefix=".readiness-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def ensure(folder):
    folder = Path(folder)
    script = folder / "script.md"
    if not script.is_file():
        return None
    text = script.read_text(encoding="utf-8-sig")
    path = folder / REPORT
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    if existing.get("version") == VERSION and existing.get("script_hash") == digest(text):
        return _reviewed(folder, existing)
    report = scan(text)
    _write(path, report)
    return _reviewed(folder, report)


def acknowledge(folder, expected_hash):
    report = ensure(folder)
    if not report or report["script_hash"] != expected_hash:
        return False
    # Preserve raw findings, rather than saving the derived review severity.
    path = Path(folder) / REPORT
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["ignored"] = True
    _write(path, raw)
    return True


def pending(folder):
    report = ensure(folder)
    return bool(report and report["warning_count"] and not report["ignored"])
