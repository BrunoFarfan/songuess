# Unified visual experience

Songuess delivers the same full visual experience to every user in solo and challenge
play: shared Blender models, album artwork, 3D carousel movement, vinyl spin, needle
movement, reveal and return sequences, interface transitions, and result effects.
The authored motion and timing do not change with browser or operating-system
preferences.

## Loading and preferences

Reduced-motion preferences are intentionally ignored for presentation. Reduced-data
preferences, Save-Data, connection speed, memory, and CPU heuristics must not select
a cheaper or static version of the experience. There is no opt-in or automatic
reduced-animation or data-saving mode.

Users on slower connections wait for the same assets. A model that is still loading
must not be replaced by a simplified renderer after a timeout. Start the authored
3D transition when its assets are ready. Actual import, model, artwork, or WebGL
failures remain explicit error-recovery cases, rather than alternate preference
profiles.

## Optimizations that preserve the experience

Continue caching and preloading shared assets, warming upcoming songs, fetching both
ends of the song carousel, disposing unused GPU resources, and pausing rendering in
hidden tabs. Responsive sizing and cameras must fit the same visual experience to
the available viewport. These optimizations must not remove visible effects or
change the authored animation timing based on user/device preferences.

## Maintaining the policy

This requirement applies to new features and future refactors. Do not reintroduce
preference-based visual tiers as a performance or accessibility default. Maintain
the matching instructions in [AGENTS.md](../AGENTS.md), [README.md](../README.md), and
the [Blender documentation](../blender/README.md). Run the frontend regression tests
and check delayed model loading when changing scene startup or animation behavior.
