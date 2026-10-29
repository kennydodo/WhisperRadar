"""Routes for Research > Exploding: small channels whose recent videos beat
their own norm (see exploding.py). New file so webapp.py needs two lines."""
from __future__ import annotations

from urllib.parse import quote

from flask import jsonify, redirect, request

from . import db, demand, exploding, outliers, youtube_api


def register(app, cfg, research_job) -> None:
    def _watched() -> set[str]:
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            return {c["channel_id"] for c in db.list_channels(conn)}
        finally:
            conn.close()

    def _num(src, name, default, cast=float, lo=None, hi=None):
        try:
            v = cast(src.get(name, default))
        except (TypeError, ValueError):
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def _opts(src) -> dict:
        d = exploding.DEFAULTS
        return {
            "days": _num(src, "days", d["days"], int, 1, 60),
            "max_subs": _num(src, "max_subs", d["max_subs"], int, 0),
            "min_mult": _num(src, "min_mult", d["min_mult"], float, 1.5),
            "min_views": _num(src, "min_views", d["min_views"], int, 0),
            "pages": _num(src, "pages", d["pages"], int, 1, 3),
            "check_limit": _num(src, "check_limit", d["check_limit"], int,
                                5, 200),
            "english_only": src.get("english_only", "1") != "0",
        }

    def _back(**kw):
        key, val = next(iter(kw.items()))
        return redirect(f"/research?tab=exploding&{key}=" + quote(val))

    def _sopts(src) -> dict:
        d = exploding.SNOWBALL
        return {
            "rounds": _num(src, "rounds", d["rounds"], int, 1, 3),
            "phrases": _num(src, "phrases", d["phrases"], int, 0, 5),
            "min_seed_score": _num(src, "min_seed_score", d["min_seed_score"],
                                   int, 0, 100),
            "max_seeds": d["max_seeds"],
        }

    def _last_opts(data) -> dict:
        """Thresholds of the latest search, so a snowball judges channels the
        same way the search did."""
        for r in reversed(data["runs"]):
            if r.get("options"):
                return {**exploding.DEFAULTS, **r["options"]}
        return dict(exploding.DEFAULTS)

    def view() -> dict:
        """Everything the _exploding.html partial shows."""
        args = request.args
        data = exploding.load(cfg)
        watched = _watched()
        opts = _opts(args)
        sort = args.get("xsort") if args.get("xsort") in exploding.SORTS \
            else "score"
        shown = exploding.ranked(
            data,
            min_score=_num(args, "xscore", 0, int, 0),
            max_subs=_num(args, "xsubs", 0, int, 0) or None,
            sort=sort, watched=watched,
            new_only=args.get("xnew") == "1")
        n_q = len(exploding.clean_queries(args.get("queries") or ""))
        sopts = _sopts(args)
        last_opts = _last_opts(data)
        seeds = exploding.seeds_for(data, watched, sopts)
        return {
            "sb": {"opts": sopts, "seeds": [s["name"] for s in seeds],
                   "check_limit": last_opts["check_limit"],
                   "estimate": exploding.estimate_snowball(
                       len(seeds), sopts["rounds"], sopts["phrases"],
                       last_opts["check_limit"])},
            "saved": exploding.Cache(cfg).counts(),
            "last_opts": last_opts,
            "rows": shown, "total": len(data["channels"]),
            "runs": list(reversed(data["runs"]))[:5],
            "last": data["runs"][-1] if data["runs"] else None,
            "opts": opts, "sort": sort, "sorts": exploding.SORTS,
            "queries": args.get("queries") or "",
            "estimate": exploding.estimate_units(
                max(n_q, 1), opts["pages"], opts["check_limit"]),
            "left": youtube_api.quota_left(cfg),
            "key": bool(youtube_api.key_source(cfg)),
            "fmt": outliers.fmt_views,
            "f": {"xscore": args.get("xscore", ""),
                  "xsubs": args.get("xsubs", ""),
                  "xnew": args.get("xnew") == "1"},
        }

    app.jinja_env.globals["exploding_view"] = view

    @app.post("/research/exploding/run")
    def exploding_run():
        if research_job.running:
            return _back(error="A research job is already running")
        queries = exploding.clean_queries(request.form.get("queries") or "")
        if not queries:
            return _back(error="Enter at least one topic or phrase")
        opts = _opts(request.form)
        need = exploding.estimate_units(len(queries), opts["pages"],
                                        opts["check_limit"])
        left = youtube_api.quota_left(cfg)
        if need > left and request.form.get("force") != "1":
            return _back(error=f"This run could use up to {need} API units "
                               f"but only {left} are left today. Lower the "
                               "channels to check or the pages, or run "
                               "anyway to get a partial result.")
        try:
            client = youtube_api.Client(cfg)
        except youtube_api.ApiError as exc:
            return _back(error=str(exc))
        watched = _watched()
        job = research_job
        log = job.log.append

        def worker():
            res = exploding.run(cfg, client, queries, opts, watched=watched,
                                log=log, cancel=lambda: job.cancel)
            log(f"{res['exploding']} exploding channel(s) from "
                f"{res['checked']} checked ({res['units']} API units)")

        research_job.start(worker, "exploding channels")
        return _back(msg="Search started - results appear when it finishes")

    @app.post("/research/exploding/snowball")
    def exploding_snowball():
        if research_job.running:
            return _back(error="A research job is already running")
        data = exploding.load(cfg)
        watched = _watched()
        sopts = _sopts(request.form)
        opts = _last_opts(data)
        opts["check_limit"] = _num(request.form, "check_limit",
                                   opts["check_limit"], int, 5, 200)
        seeds = exploding.seeds_for(data, watched, sopts)
        if not seeds:
            return _back(error="No strong channel left to expand from. "
                               "Lower the minimum score, or run a search first.")
        need = exploding.estimate_snowball(
            len(seeds), sopts["rounds"], sopts["phrases"], opts["check_limit"])
        left = youtube_api.quota_left(cfg)
        if need > left and request.form.get("force") != "1":
            return _back(error=f"A snowball could use up to {need} API units "
                               f"but only {left} are left today. Use fewer "
                               "rounds or searches, or fewer channels to check.")
        try:
            client = youtube_api.Client(cfg)
        except youtube_api.ApiError as exc:
            return _back(error=str(exc))
        job = research_job
        log = job.log.append

        def worker():
            res = exploding.snowball(cfg, client, opts, sopts, watched=watched,
                                     log=log, cancel=lambda: job.cancel)
            log(f"snowball: {res['exploding']} exploding channel(s) from "
                f"{res['checked']} checked ({res['units']} API units)")

        research_job.start(worker, "exploding snowball")
        return _back(msg="Snowball started - results appear when it finishes")

    @app.post("/research/exploding/rescore")
    def exploding_rescore():
        """Judge the saved upload histories again with new thresholds: no API."""
        opts = _opts(request.form)
        res = exploding.rescore(cfg, opts, watched=_watched())
        return _back(msg=f"Re-scored {res['channels']} saved channel(s) with "
                         f"no API units: {res['exploding']} exploding")

    @app.post("/research/exploding/dismiss")
    def exploding_dismiss():
        cid = (request.form.get("channel_id") or "").strip()
        if cid:
            exploding.dismiss(cfg, cid)
        return redirect("/research?tab=exploding")

    @app.get("/research/exploding/suggest")
    def exploding_suggest():
        """Autocomplete phrases to widen the search (no API units)."""
        seeds = exploding.clean_queries(request.args.get("q") or "", 4)
        out = exploding.expand_queries(seeds, demand.suggestions, per_seed=3)
        return jsonify(queries=out)
