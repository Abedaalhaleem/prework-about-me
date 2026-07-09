# F² — FITNESS FOREVER · site build

Single-file brand site: `f2/index.html`. No JS dependencies; media streams from the Higgsfield CDN.

## Status

- [x] 1. Skeleton — all sections + animations on placeholders
- [x] 2. Higgsfield batches — 46 stills + 6 films, all verified completed (IDs in `assets.json`)
- [x] 3. **Cinema build (v0.2)** — real renders in every card/panel/thumb; six scroll films
      connect the categories (each starts/ends in black so sections melt together).
      Desktop scrubs film time with scroll; phone auto-plays each film on entry (no iOS seek jank).
      Media lazy-loads per section from the Higgsfield CDN.
- [ ] **Phone test** ← current gate
- [ ] 4. Polish pass (easing/timing) + second phone test
- [ ] 5. Ship (optionally re-embed media as data URIs for a fully offline single file)

## How it's built

- Phone-first: `pointer: coarse` detection puts `.touch`/`.fine` on `<html>` before first paint.
  Desktop-only modules (magnetic button, card tilt, cursor glow, backdrop blur) never run on touch.
- All scroll effects are compositor-only (transform/opacity). One rAF loop, no scroll hijacking.
- Legging spin: scroll-stepped fake-3D placeholder — 16 frames on phone, 24 on desktop —
  same cadence the real extracted video frames will use.
- 3D hero logo: layered-SVG extrusion of the brand mark, hero-only, fully disabled once scrolled past.
- Reduced-motion: spins/parallax/marquee disabled, fills render complete.

## Asset slots (Higgsfield)

Product stills (nano_banana_pro, black backdrop, single angle) map to placeholder codes:
`W01–W06` women × Core Black/Sage Mint/Mocha, `M01–M06` men × Core Black/Olive/Graphite,
`A01–A04` accessories (black). Lookbook `L01–L04(+1)` via soul_2.
Legging turntable via kling3_0 → frames extracted to drive the spin section.
Job IDs land in `f2/assets.json` when fired.
