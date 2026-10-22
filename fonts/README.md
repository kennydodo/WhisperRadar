# Shared fonts

Drop `.ttf` / `.otf` files here (for example Montserrat-Bold.ttf, Oswald-Bold.ttf,
BebasNeue-Regular.ttf from fonts.google.com). Use only fonts whose licence allows video use.

The renderer draws shotlist text overlays with:
1. the file named in a project's `imgtovideo.json` -> `text.font` (a full path, a file in the
   project's `text.fonts_dir`, or just a file name from this folder);
2. otherwise the first font of the project's `text.fonts_dir`;
3. otherwise the first font in this folder (alphabetical);
4. otherwise a Windows system font (Arial Bold).

Example: `"text": {"font": "Montserrat-Bold.ttf"}`
