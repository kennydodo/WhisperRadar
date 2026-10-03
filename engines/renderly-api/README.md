# Renderly API (image generation backend)

The FastAPI backend that used to live in the Renderly repo, now part of
WhisperRadar. Same API, same port (8022); WhisperRadar starts it itself.
It keeps its own SQLite database (`renderly.db`) and image storage
(`storage\`) in this folder - separate from WhisperRadar's database.

Run by hand (from this folder):

    .venv\Scripts\python.exe -m uvicorn main:app --port 8022

Needs a `.env` with `GEMINI_API_KEY` (not in git). Tests: `python -m unittest
discover tests` from this folder.
