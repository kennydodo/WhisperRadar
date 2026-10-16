"""Find tests that leave a SQLite connection open.

On Windows an open connection keeps its file locked, so a test's
TemporaryDirectory cleanup fails with `PermissionError [WinError 32]` on
`x.db` / `wr.db`; on Linux the same leak is silent. This runs the tests and,
after each one, reports every connection that is still open together with the
stack of the code that opened it.

    python scripts/find_db_leaks.py                      # whole suite (slow)
    python scripts/find_db_leaks.py test_handover        # chosen modules
    python scripts/find_db_leaks.py -k Routes test_handover

A connection opened by a background thread that is mid-query when a test ends
shows up once and is not a bug; a leak reported on every run is.
"""
import argparse
import collections
import sqlite3
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

_real_connect = sqlite3.connect
OPEN: dict = {}


class _Tracked(sqlite3.Connection):
    def close(self):
        OPEN.pop(id(self), None)
        return super().close()


def _connect(*args, **kwargs):
    kwargs["factory"] = _Tracked
    conn = _real_connect(*args, **kwargs)
    OPEN[id(conn)] = (conn, "".join(traceback.format_stack(limit=14)[:-1]))
    return conn


sqlite3.connect = _connect
LEAKS: "collections.OrderedDict[str, list[str]]" = collections.OrderedDict()


class _Result(unittest.TextTestResult):
    def stopTest(self, test):
        super().stopTest(test)
        if OPEN:
            LEAKS[str(test)] = [stack for _, stack in OPEN.values()]
            for conn, _ in list(OPEN.values()):
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            OPEN.clear()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("modules", nargs="*", help="test modules (default: all)")
    ap.add_argument("-k", dest="pattern", help="only tests whose name contains this")
    args = ap.parse_args()
    sys.path[:0] = [str(ROOT), str(TESTS)]
    loader = unittest.defaultTestLoader
    if args.pattern:
        loader.testNamePatterns = [f"*{args.pattern}*"]
    suite = (loader.loadTestsFromNames(args.modules) if args.modules
             else loader.discover(str(TESTS)))
    unittest.TextTestRunner(resultclass=_Result, verbosity=0).run(suite)
    print(f"\nTESTS THAT LEFT A CONNECTION OPEN: {len(LEAKS)}")
    by_site = collections.Counter()
    for stacks in LEAKS.values():
        for stack in stacks:
            frames = [l.strip() for l in stack.splitlines()
                      if l.strip().startswith("File")
                      and "unittest" not in l and "/tests/" not in l
                      and "\\tests\\" not in l and "find_db_leaks" not in l]
            by_site["\n    ".join(frames[-3:])] += 1
    for site, n in by_site.most_common():
        print(f"\n{n}x opened at:\n    {site}")
    for name in list(LEAKS)[:20]:
        print("  test:", name)
    return 1 if LEAKS else 0


if __name__ == "__main__":
    raise SystemExit(main())
