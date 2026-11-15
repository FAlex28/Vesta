# Vesta project context

## Dashboard stack

- The dashboard lives in `frontend/` and uses React, TypeScript, and Vite.
- Keep the existing React application. Do not migrate it to Next.js, Tailwind, shadcn/ui, or another framework unless the owner asks.
- Before changing the UI, read `frontend/src/App.tsx`, `frontend/src/styles.css`, `frontend/src/data/dashboardData.ts`, and `frontend/src/components/`.
- Use the `vesta-dashboard-design` skill for dashboard design and visual review.

## Current product state

- The screen is a local frontend with sample values. ESP32, backend, WebSocket, and real GPS are not connected.
- The map pin is a fixed test point in Piața Victoriei, Timișoara. Never present it as a person's live location.
- Sensor values, distance history, and initial activity events are local sample data. Clearly distinguish sample/simulated data from live data.
- Browser controls for starting a session, triggering SOS, or starting voice interactions have intentionally been removed. Do not restore them unless the owner explicitly asks.
- Do not show the words “Demo” or “UniHack” in the product UI.
- Keep the Vesta brand lockup at the upper left. Do not add a close X beside it or an account avatar in the top right.
- Do not request microphone or location permissions from the browser. Voice and live location belong to the mobile app flow and require explicit authorization.

## Integration boundary

- Do not invent API endpoints, WebSocket events, escalation rules, or live status values.
- Coordinate future API/WebSocket work with the shared contract at `shared/API_CONTRACT.md` when that file exists.
- Keep sample data clearly labeled and avoid fabricated trend percentages or success metrics.

## Verification

- Preserve readable Romanian copy, keyboard focus, accessible contrast, and responsive behavior for desktop and Android-sized screens.
- After implementation, run `npm run build` from `frontend/` and report any limitations. Do not commit or push unless requested.
