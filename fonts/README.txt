Drop .ttf / .otf font files here and reference them by file name only.

Example (themes.py or a custom theme):

    heading_font="fonts/Caveat-Bold.ttf"      # repo fonts folder
    body_font="Caveat-Regular.ttf"            # bare name also finds this folder

Or keep per-production fonts inside the production folder and reference them
project-relative ("fonts/Caveat-Bold.ttf" inside the production folder).

Where to get fonts: https://fonts.google.com (all open license — SIL OFL or
Apache 2.0). Keep the license file with the font when you download one.

Good handwriting-style pairings for whiteboard mode (all OFL):
  Caveat (Bold), Shadows Into Light, Patrick Hand, Kalam, Gochi Hand.

A font file that cannot be found is a validation error naming every location
that was checked — nothing falls back silently.
