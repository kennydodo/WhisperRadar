"""Built-in Auto Run scheduler.

A thin ticker: while the dashboard is running it wakes up every TICK_SECONDS
and, when `scheduler_enabled` is on and `scheduler_interval_minutes` has
elapsed, hands off to the producer.

It deliberately owns no policy of its own - `producer.build_plan()` already
decides whether there is anything to do (Auto Run on, inside the run window,
under the per-day caps, un-produced candidates available), so the scheduler
never duplicates that logic and can never disagree with the Studio button.

Runs happen in the dashboard's studio job slot, so progress shows up in the UI
and a scheduled run and a manual one can never overlap. If the dashboard is
not running, use `wr.py produce` from Windows Task Scheduler instead.
"""

import logging
import threading
from datetime import datetime, timedelta

from . import db, producer, settings

log = logging.getLogger("whisperradar")

TICK_SECONDS = 30
LAST_ATTEMPT_KEY = "scheduler_last_attempt"
LAST_RESULT_KEY = "scheduler_last_result"


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


class Scheduler:
    def __init__(self, cfg, job, run_producer):
        """`job` is the dashboard's studio job slot (has .running); the
        `run_producer(channels, log)` callback starts a run in it."""
        self.cfg = cfg
        self.job = job
        self.run_producer = run_producer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick: dict = {"action": "off"}

    # ------------------------------------------------------------ thread --

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="wr-scheduler",
                                        daemon=True)
        self._thread.start()
        log.info("scheduler: ticker started (every %ds)", TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._last_tick = self.tick()
            except Exception as exc:  # noqa: BLE001 - a tick must never die
                log.warning("scheduler: tick failed: %s", exc)

    # ------------------------------------------------------------- state --

    def _conn(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        return conn

    def status(self) -> dict:
        conn = self._conn()
        try:
            vals = settings.load(conn)
            last = _parse(db.get_setting(conn, LAST_ATTEMPT_KEY))
            result = db.get_setting(conn, LAST_RESULT_KEY)
        finally:
            conn.close()
        interval = max(5, int(vals.get("scheduler_interval_minutes") or 60))
        nxt = last + timedelta(minutes=interval) if last else None
        return {
            "enabled": bool(vals.get("scheduler_enabled")),
            "interval": interval,
            "last_attempt": last.strftime("%Y-%m-%d %H:%M") if last else None,
            "last_result": result,
            "next_run": (nxt.strftime("%Y-%m-%d %H:%M")
                         if nxt and nxt > datetime.now() else
                         "due now" if nxt else None),
            "job_running": bool(getattr(self.job, "running", False)),
            "last_tick": self._last_tick,
        }

    def _record(self, now: datetime, result: str) -> None:
        conn = self._conn()
        try:
            db.set_setting(conn, LAST_ATTEMPT_KEY, now.isoformat(timespec="seconds"))
            db.set_setting(conn, LAST_RESULT_KEY, result[:200])
        finally:
            conn.close()

    # -------------------------------------------------------------- tick --

    def tick(self, now: datetime | None = None) -> dict:
        """One scheduler pass. Safe to call directly (tests drive it with an
        explicit `now`). Returns what it did."""
        now = now or datetime.now()
        conn = self._conn()
        try:
            vals = settings.load(conn)
            last = _parse(db.get_setting(conn, LAST_ATTEMPT_KEY))
        finally:
            conn.close()

        if not vals.get("scheduler_enabled"):
            return {"action": "off", "reason": "scheduler is off in Settings"}
        interval = max(5, int(vals.get("scheduler_interval_minutes") or 60))
        if last and now < last + timedelta(minutes=interval):
            return {"action": "wait",
                    "reason": f"next run at "
                              f"{(last + timedelta(minutes=interval)):%H:%M}"}
        if getattr(self.job, "running", False):
            return {"action": "skip", "reason": "a job is already running"}

        conn = self._conn()
        try:
            plan = producer.build_plan(self.cfg, conn)
        finally:
            conn.close()
        runnable = [p for p in plan if p.get("action") == "run"]
        if not runnable:
            reason = plan[0]["detail"] if plan else "nothing to do"
            self._record(now, f"nothing to do - {reason}")
            return {"action": "skip", "reason": reason}

        self._record(now, f"started for {len(runnable)} channel(s)")
        self.run_producer(len(runnable), log.info)
        return {"action": "run", "channels": len(runnable)}
