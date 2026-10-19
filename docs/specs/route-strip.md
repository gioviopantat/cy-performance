# Route strip (header animation)
Status: done · 2026-10-10

## Goal
The strip under the header becomes a small handheld-game route: the athlete rides a pixel bike
along a dirt path, critters wait in the grass and say hello in speech bubbles, a tap catches one
and it follows the bike. Pure fun; it rewards opening the dashboard.

## Non-goals
- No game assets. The look is inspired by 2000s handheld monster games, but every sprite,
  tile and name is drawn for this project (`web/src/sprites.ts`); the repository is public and
  those characters are someone else's.
- No server state, no API: nothing here touches training data.

## Design
- `web/src/route.tsx` (`RouteStrip`) replaces `RoadStrip` and the SVG `Bike` in `bike.tsx`.
- World: a 128 x 32 pixel ground strip (trees, grass, a dirt path, tall grass, flowers) drawn
  once to an SVG data URL and scrolled in whole sprite pixels; scale 3 on desktop, 2 on a phone.
- Trainer: 16 x 16, two pedal frames, fixed at 45 % (phone 55 %) of the width; cruise speed
  follows today's readiness, a tap on the road sprints (as before).
- Critters: eleven original 12 x 12 sprites (one rare, 5 %), at most three on screen, spawning
  every 3.5–9 s on the upper or lower verge. When the bike reaches one, both say hello
  (speech bubbles beside them, a short chirp).
- Catch: a tap throws a capsule; the critter joins the party and hops along behind the bike.
  The party holds at most 10, first in first out (the oldest says goodbye). It is kept per
  viewer and per profile in `localStorage` (`cyp.party.<profile>`).
- Sounds through `sfx.ts` (greet, throw, catch, release). `prefers-reduced-motion`: a still
  scene with the party, no spawning.

## Safety
Presentation only; no writes, no network.

## Acceptance
- Playwright at 1280 and 390 px: critters spawn and greet; a tap catches; with 10 in the party a
  catch drops the oldest; the other profile has its own party; reduced motion shows no critters.
- `uv run poe check` green.

## Docs to update
docs/specs/web-ui.md, docs/architecture.c4 (web description).
