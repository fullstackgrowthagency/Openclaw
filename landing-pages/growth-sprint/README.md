# 30-Day Growth Sprint — Scrollytelling Landing Page

Static, dependency-free landing page (`index.html` + `styles.css` + `script.js`). No build step — open `index.html` directly or serve the folder with any static host (Hostinger Horizons, Netlify, GH Pages, `python3 -m http.server`, etc).

## How it works

- **Living background** — a canvas starfield plus five blurred, drifting CSS orbs stay fixed behind the entire scroll (`#starfield`, `.orb-field`), matching the brand's dark navy / teal aesthetic.
- **Scrollytelling engine** (`script.js`) — a single `position: sticky` stage holds all ten scenes absolutely stacked on top of each other. A scroll-driven `sceneFloat` value controls each scene's opacity/scale/blur, with neighboring scenes' fade windows overlapping so one dissolves and zooms into the next rather than using reveal-on-scroll cards.
- **3D dashboard visuals** — every scene has a bespoke animated SVG/CSS panel (dashboard chart, radar scan, funnel, pipeline graph, analytics dashboard, orbiting tool ring, card fan, price flip) that tilts toward the cursor via `perspective`/`rotateX`/`rotateY`, with an idle auto-tilt fallback for touch devices.
- **Accessibility** — `prefers-reduced-motion: reduce` disables the pin/zoom mechanics entirely and falls back to a normal static, readable document flow.

## Content

Copy and pricing are sourced directly from the live `fullstackgrowth.studio/landing/30-day-growth-sprint` page (hero, the "why growth can be bad for business" section, the 10 sprint deliverables, platform tools, 4 bonuses, and the $1,997-value-FREE offer). The CTA links to `https://fullstackgrowth.studio/checkout/30-day-growth-sprint`.

## Assets

`assets/logo-mark.png` (and smaller `-128`/`favicon-64` variants) is the real Full Stack Growth Studio circuit-tree logo, pulled from the brand's CDN.
