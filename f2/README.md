# F² — FITNESS FOREVER · site build

Single-file brand site: `f2/index.html`. No dependencies, no network calls — opens anywhere.

## Status

- [x] **1. Skeleton** — all sections + animations live on gray placeholder boxes
- [ ] **2. Phone scroll test** ← current gate (owner tests on their phone)
- [x] 3. Higgsfield asset batches — fired + verified completed (46 stills; turntable video rendering). IDs in `assets.json`
- [ ] 4. Real images in, color morphs wired to renders
- [ ] 5. Polish pass (easing/timing) + second phone test
- [ ] 6. Ship

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
