"""Saved opening approaches and immutable per-production instruction snapshots."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from . import db

CATALOG_KEY = "opening_styles_catalog"
STANDARD = [
    dict(id="purpose", name="Hook → clear purpose", instructions="Open with a relatable problem or a supported surprising point. Immediately afterwards, briefly make clear what the viewer will learn or gain and why it matters. If the hook already establishes this, do not repeat it or force an 'In this video' sentence.", example="A smaller payment can hide a more expensive car. Compare the total you will pay before choosing the loan."),
    dict(id="explain", name="Direct explanation", instructions="Start directly with the subject, a specific technical question or a useful finding. Begin explaining promptly. The purpose can emerge through the explanation; a separate purpose statement is optional. Avoid unrelated lifestyle setups and invented personal testing.", example="A model can fit in memory and still run slowly. Capacity decides whether it loads; bandwidth affects how quickly it can work."),
    dict(id="demo", name="Demonstration first", instructions="Open with a real documented result, observable example or demonstration. Explain what the viewer is seeing and develop the question it raises. Do not invent a test or claim a visual demonstration exists unless supplied. A separate purpose statement is optional.", example="Show a supplied example of the same task on two configurations, then explain the difference."),
    dict(id="compare", name="Comparison first", instructions="Introduce two concrete options and a meaningful difference or tradeoff. Develop the comparison into the central question. State assumptions for numbers. The comparison may establish the purpose without a separate announcement.", example="Compare paying for a service each month with buying the equipment, then examine what each option actually provides."),
    dict(id="story", name="Story opening", instructions="Begin inside a relevant event or decision. Establish stakes through action and consequences, then develop the story naturally. Do not interrupt a working scene with a mandatory purpose paragraph. Disclose illustrative fiction naturally; never invent personal experience as evidence.", example="The manager who refused your raise is waiting outside your office for an interview."),
    dict(id="discovery", name="Discovery opening", instructions="Introduce an unusual object, observation or historical puzzle. Develop a question and begin investigating it. Keep historical claims supported and uncertainty clear. A separate purpose statement is optional.", example="A green stone near a fire becomes the starting point for a question about discovering copper."),
    dict(id="reflect", name="Reflective opening", instructions="Begin with a recognisable behaviour or thoughtful question. Develop its meaning through a concrete example. Avoid vague inspirational slogans and universal diagnoses. Make relevance clear naturally rather than requiring a separate purpose paragraph.", example="You keep changing the plan before you have practised it long enough to learn from it."),
    dict(id="channel", name="Follow channel / reference approach", instructions="Choose an opening suited to this topic and the channel's voice. Learn from the reference's opening function and pacing using original wording and examples. Make the relevance understandable; no fixed hook sequence or separate purpose sentence is required.", example="Use the channel's established approach while adapting the opening to this video's subject."),
]


def catalog(conn):
    raw = db.get_setting(conn, CATALOG_KEY)
    try:
        custom = json.loads(raw or "[]")
    except (ValueError, TypeError):
        custom = []
    return [dict(x, builtin=True) for x in STANDARD] + [
        dict(x, builtin=False) for x in custom if isinstance(x, dict) and x.get("id")]


def save_custom(conn, name, instructions, example="", style_id=""):
    name, instructions, example = name.strip(), instructions.strip(), example.strip()
    if not name or len(name) > 100 or not instructions or len(instructions) > 12000 or len(example) > 2000:
        raise ValueError("Enter a name (up to 100 characters), opening instructions (up to 12,000), and an optional example (up to 2,000).")
    custom = [x for x in catalog(conn) if not x["builtin"]]
    if style_id and not any(x["id"] == style_id for x in custom):
        raise ValueError("Unknown custom opening style. Duplicate a standard style to customise it.")
    style_id = style_id or "custom-" + uuid4().hex[:12]
    item = dict(id=style_id, name=name, instructions=instructions, example=example)
    custom = [item if x["id"] == style_id else x for x in custom]
    if not any(x["id"] == style_id for x in custom):
        custom.append(item)
    db.set_setting(conn, CATALOG_KEY, json.dumps(custom, ensure_ascii=False))
    return style_id


def defaults(name):
    name = (name or "").lower()
    if "alicia" in name or "to live and more" in name:
        return ["purpose"]
    if "ai guy" in name:
        return ["explain", "demo", "compare"]
    if "pov" in name:
        return ["story"]
    if "nature made us" in name:
        return ["discovery"]
    if "presence" in name or "steadier mind" in name:
        return ["reflect"]
    return ["channel"]


def channel_ids(channel):
    raw = channel["opening_styles"] if channel is not None else None
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list) and values:
                return values
        except (ValueError, TypeError):
            pass
    return defaults(channel["name"] if channel is not None else "")


def snapshot(conn, ids, custom="", chosen="", saved_styles=()):
    by_id = {x["id"]: x for x in catalog(conn)}
    by_id.update({x["id"]: x for x in saved_styles})
    ids = list(dict.fromkeys(ids))
    if not ids or any(x not in by_id for x in ids):
        raise ValueError("Select at least one available opening style.")
    if chosen and chosen not in ids:
        raise ValueError("The chosen opening must be one of the selected styles.")
    if len(custom) > 12000:
        raise ValueError("Custom opening instructions must be at most 12,000 characters.")
    return dict(styles=[by_id[x] for x in ids], custom=custom.strip(), chosen=chosen or (ids[0] if len(ids) == 1 else ""))


def resolve(conn, prod, *, freeze=False):
    raw = prod["opening_config"]
    if raw:
        return json.loads(raw)
    channel = db.get_own_channel(conn, prod["own_channel_id"]) if prod["own_channel_id"] else None
    value = snapshot(conn, channel_ids(channel))
    if freeze:
        conn.execute("UPDATE productions SET opening_config=? WHERE id=? AND opening_config IS NULL",
                     (json.dumps(value, ensure_ascii=False), prod["id"]))
        conn.commit()
        return json.loads(db.get_production(conn, prod["id"])["opening_config"])
    return value


def instruction(cfg, prod, *, writer=False):
    conn = db.connect(cfg.db_path)
    try:
        current = db.get_production(conn, prod["id"])
        value = resolve(conn, current, freeze=True)
    finally:
        conn.close()
    selected = value.get("chosen")
    styles = [x for x in value["styles"] if not selected or x["id"] == selected]
    text = ("SELECTED OPENING APPROACH — this takes priority over conflicting opening instructions "
            "in the general defaults, channel writing style or reference. Other voice and accuracy rules still apply. "
            "Multiple allowed approaches are alternatives: choose ONE that suits this topic; do not combine them all.\n")
    text += "\n".join(f"[{x['id']}] {x['name']}: {x['instructions']}" for x in styles)
    if value.get("custom"):
        text += "\nCreator's custom opening instructions (take priority): " + value["custom"]
    if writer and not selected:
        text += ("\nBefore the narration, include one metadata comment <!-- opening-style:ID --> "
                 "using the chosen allowed ID. This is metadata, never spoken narration.")
    elif writer:
        text += "\nFollow this chosen approach. Do not add a separate purpose sentence unless this approach requires it."
    else:
        text += "\nAssess this approach on relevance, interest and progression. Do not demand a separate purpose sentence unless this approach requires it."
    return text


MARKER = re.compile(r"<!--\s*opening-style:([\w-]+)\s*-->", re.I)


def capture(cfg, pid, text):
    matches = MARKER.findall(text or "")
    if not matches:
        return text
    conn = db.connect(cfg.db_path)
    try:
        prod = db.get_production(conn, pid)
        value = resolve(conn, prod, freeze=True)
        chosen = matches[0]
        if len(set(matches)) != 1 or chosen not in {x["id"] for x in value["styles"]}:
            raise ValueError("The writer returned an opening style outside this production's allowed choices.")
        if value.get("chosen") and value["chosen"] != chosen:
            raise ValueError("The writer changed the production's chosen opening style. Change the opening override before rewriting.")
        value["chosen"] = chosen
        db.update_production(conn, pid, opening_config=json.dumps(value, ensure_ascii=False))
    finally:
        conn.close()
    return MARKER.sub("", text).strip()
