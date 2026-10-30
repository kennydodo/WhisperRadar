"""Explainer theme registry.

finance_dark is the motion-graphics default; whiteboard_light is the light
board + dark marker theme for whiteboard mode (doc 16.3).
"""
from __future__ import annotations

from .schema import ThemeSpec, default_theme, default_whiteboard_theme


def _nature() -> ThemeSpec:
    t = default_theme("nature_parchment")
    t.background = "#F5EFE0"
    t.foreground = "#2B2B26"
    t.muted = "#8A8474"
    t.accent = "#2F6F4E"
    t.positive = "#2F6F4E"
    t.negative = "#A8442A"
    return t


def _living() -> ThemeSpec:
    t = default_theme("living_warm")
    t.background = "#1C1613"
    t.foreground = "#F7EFE6"
    t.muted = "#9A8C7E"
    t.accent = "#E0863C"
    t.positive = "#8FBF6A"
    t.negative = "#D9534F"
    return t


def _whiteboard() -> ThemeSpec:
    return default_whiteboard_theme("whiteboard_light")


def _chalkboard() -> ThemeSpec:
    """Dark board, chalk-white strokes (the look of the faceless whiteboard
    explainer channels). Name it explicitly: "theme": "chalkboard"."""
    t = default_whiteboard_theme("chalkboard")
    t.background = "#12161A"
    t.foreground = "#F2EFE6"
    t.muted = "#8E96A0"
    t.accent = "#7FC8F8"
    t.positive = "#8FD9A8"
    t.negative = "#FF9B8A"
    t.heading_font = t.body_font = t.hand_font = "Gaegu-Bold.ttf"
    return t


THEMES = {
    "finance_dark": lambda: default_theme("finance_dark"),
    "nature_parchment": _nature,
    "living_warm": _living,
    "whiteboard_light": _whiteboard,
    "chalkboard": _chalkboard,
}

DEFAULT_THEME = "finance_dark"
WHITEBOARD_THEME = "whiteboard_light"


def resolve(name) -> ThemeSpec:
    """Theme by name; unknown/absent name falls back to the default."""
    if not isinstance(name, str) or name.strip() == "":
        name = DEFAULT_THEME
    factory = THEMES.get(name.strip().lower())
    if factory is None:
        factory = THEMES[DEFAULT_THEME]
    return factory()
