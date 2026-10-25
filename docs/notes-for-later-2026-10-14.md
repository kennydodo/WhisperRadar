# Notes for later (2026-10-14)

- **Chrome minimized stalls the web chats.** Sometimes, when the Chrome window
  with GLM (z.ai) / DeepSeek is minimized, the chat does not continue until the
  user brings the window into focus. Likely causes to check: Chrome throttling
  background/occluded tabs (timers, rendering, `requestAnimationFrame`), the
  "intensive throttling" of hidden tabs, and window-occlusion detection
  (`--disable-backgrounding-occluded-windows`,
  `--disable-renderer-backgrounding`, `--disable-background-timer-throttling`,
  `--disable-features=CalculateNativeWinOcclusion`). Also check whether the
  automation needs the page `visibilityState == "visible"`. Not investigated yet.
