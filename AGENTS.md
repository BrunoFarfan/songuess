# Songuess project instructions

## One full visual experience

Every user must receive the full authored 3D models, artwork, animations, transitions,
and timing in both solo and challenge play. This is a product requirement.

- Ignore OS/browser reduced-motion and reduced-data preferences for presentation.
- Do not read Save-Data, connection speed/type, device memory, CPU count, or similar
  signals to skip assets, disable prefetching, simplify scenes, shorten animations,
  freeze motion, or select a lightweight experience.
- Do not introduce a reduced-motion setting, data-saving mode, or automatic quality
  tier. Do not restore reduced-motion media queries or preference listeners.
- Slow downloads must finish loading the full 3D experience. Do not switch to CSS
  or another simplified renderer because an elapsed-time budget was exceeded.
- Preserve shared caches, preloading, responsive geometry, resource cleanup, and
  pausing rendering while the document is hidden. These must preserve the full
  visible experience for every user.
- Handle actual loading/rendering failures explicitly; do not confuse a pending
  download with a failure or use error recovery to introduce preference-based tiers.

Keep this requirement aligned with [the visual experience policy](docs/visual-experience.md),
the README, and Blender asset documentation. Changes to visual loading or animation
must continue passing the unified-experience regression checks.
