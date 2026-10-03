import os

# the suite must never edit a real Chrome profile (chrome_profile.apply)
os.environ.setdefault("WHISPERRADAR_NO_CHROME_PREFS", "1")
