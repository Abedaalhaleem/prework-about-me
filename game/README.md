# NEONTOWN 2071

A futuristic first-person arena shooter that runs entirely in the browser. It's inspired by the classic
"two houses across a cul-de-sac" small-map style of fast arcade shooters, reimagined as a neon suburb at dusk.
All the art, models, textures and sounds are generated in code. There are no external game assets.

**Play:** open `game/index.html` through any static web server (for example GitHub Pages, or
`npx http-server game` locally), then click **DEPLOY**. You need a keyboard, a mouse and a WebGL2 browser.

## Features

- **Map:** two prefab houses with walkable second floors facing each other across a street, a hover-bus in the
  middle, a workshop with a fusion core, backyards, side yards, containers and a neon gate at each end of the
  cul-de-sac. The megacity skyline, flying traffic and dusk sky surround the map.
- **Fire everywhere:** burning wrecks, trash-barrel fires and fire pits. Shootable fuel cells and a fusion core
  chain-explode and leave burning ground behind. Fire hurts if you stand in it.
- **Movement:** sprint, power-slide, thrust double-jump, wall-running, wall-jumps and mantling onto ledges.
- **Weapons:** ARC-7 assault rifle, VANTA-9 SMG and LONGBOW-X sniper (with a scope). Each has aim-down-sights,
  recoil, bloom, damage falloff, headshots, reloads, shell ejection and muzzle flash. You also have melee and
  frag grenades.
- **Team Deathmatch, 5 v 5:** four allied bots and five enemy bots with A* navigation across both floors. They
  strafe during firefights, fire in bursts, react to sounds and throw grenades.
- **Scorestreaks:** a UAV at 3 kills shows enemies on the minimap, and an Orbital Strike at 5 kills calls in three
  beam strikes.
- **HUD:** rotating minimap with enemy pings, killfeed, medals (First Blood, Double Kill, Headshot, Longshot,
  Revenge…), hit markers, damage direction arcs, grenade warning, scoreboard (Tab) and a death cam.
- **Match end:** a detonation on the horizon, with a flash, a mushroom cloud and a shockwave, followed by the
  results screen.

## Controls

| Key | Action |
| --- | --- |
| W A S D | Move |
| Mouse / Left click / Right click | Aim / Fire / Aim down sights |
| Shift | Sprint |
| Space | Jump · in the air: thrust jump · on a wall: wall-jump |
| C | Crouch · while sprinting: slide |
| R | Reload |
| 1 2 3 / wheel | Switch weapon |
| G | Frag grenade |
| F or V | Melee |
| 4 / 5 | UAV / Orbital Strike |
| Tab | Scoreboard |
| Esc | Pause |

Settings (sensitivity, FOV, volume, bot difficulty, graphics quality, announcer voice, invert Y) are on the main menu.

## Tech

- [three.js](https://threejs.org) r169, vendored in `vendor/three` (MIT licence), with an UnrealBloom and ACES
  tone-mapping post-processing chain.
- `js/map.js`: box-built level that is merged per material, with world-projected UVs and matching collision boxes.
- `js/collision.js`: AABB world with a grid broadphase and the shared character controller.
- `js/nav.js`: automatic multi-level nav-graph (walk simulation) and A*.
- `js/effects.js`: GPU point-sprite particle pools (flames, fireballs, sparks, smoke), tracers, decals and lights.
- `js/audio.js`: procedural Web Audio for gunshots, explosions, reverb and fire crackle.
