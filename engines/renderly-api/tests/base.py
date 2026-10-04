"""Shared setup for the backend test suite.

Puts the backend package on sys.path and points DATABASE_URL at a throwaway
database BEFORE anything imports config/db, so tests never touch renderly.db.
"""
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

_TEST_DB = os.path.join(tempfile.gettempdir(), "renderly-tests.sqlite3")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TEST_DB.replace("\\", "/")
