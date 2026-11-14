---
name: vesta-dashboard-design
description: Design, redesign, or critique the Vesta personal-safety dashboard in the existing React application. Use for dashboard layout, typography, responsive behavior, charts, sensor status, location presentation, and visual polish.
---

# Vesta Dashboard Design

Use this skill when changing the Vesta dashboard. Treat the repository's `CLAUDE.md` as the persistent product context and read it before editing.

## First inspect the actual screen

Read `frontend/src/App.tsx`, `frontend/src/styles.css`, `frontend/src/data/dashboardData.ts`, `frontend/src/components/CityMap.tsx`, and `frontend/src/components/DistanceChart.tsx`. Check for existing components and behavior before proposing replacements. Keep the current React + TypeScript + Vite stack and avoid broad dependency changes for visual work.

## Design for personal safety

Make the interface calm, credible, and legible at a glance. Use a clear hierarchy for:

1. Session state and whether the person is safe, waiting for confirmation, or unavailable.
2. Location and its source, with a clear distinction between fixed test coordinates and live GPS.
3. Sensor readings, units, connection state, and data age/source.
4. Distance history and recent session events.
5. Connectivity for the mobile app/device, backend, and WebSocket.
6. Voice assistant availability and its authorization state.

Use large readable type, deliberate spacing, restrained color, consistent alignment, and responsive layouts. Avoid a generic grid of oversized KPI cards, decorative gradients, fake percentage deltas, and overly dense microcopy. Keep chart labels and sensor units understandable without hover.

## Keep the product truthful

- Current values are sample data; ESP32, backend, WebSocket, and GPS are not connected.
- The map currently marks a fixed point in Piața Victoriei, Timișoara. It is not the protected person's current location.
- Do not label sample values as live, imply a real alert was sent, or claim that the microphone is active.
- Do not add browser controls that start a safety session, trigger SOS, or start voice interaction. These controls are intentionally disabled/removed for now.
- Do not show “Demo” or “UniHack” in the product UI.
- Keep Vesta branding only in the upper-left brand lockup. Do not add a close X beside it or an account avatar in the upper-right corner.
- A future real location must come from the authorized mobile GPS flow and the agreed API/WebSocket contract. Do not invent the contract in frontend design work.

## Work sequence

1. Summarize the current hierarchy and identify the three highest-impact design problems.
2. Propose a compact layout and visual direction that preserves all required information.
3. Implement the design in the existing React components and CSS, keeping behavior and data boundaries intact.
4. Review desktop and narrow Android/mobile widths; fix clipping, overflow, weak contrast, and tiny labels.
5. Run `npm run build` from `frontend/` and report the result. Do not commit or push.

When asked only for a design plan, stop after step 2. When asked to implement, continue through the full sequence.
