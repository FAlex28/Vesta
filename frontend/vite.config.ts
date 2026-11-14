import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

// The phone, dashboard and contact pages reach the backend through this server, so a phone on
// the local network needs only one address. The backend still checks the page Origin for
// WebSockets: add the address the phone uses (e.g. http://192.168.1.20:5173) to
// VESTA_CORS_ORIGINS on the backend.
const backend = process.env.VESTA_BACKEND_URL ?? "http://127.0.0.1:8000";

const proxy: Record<string, ProxyOptions> = {
  "/api": { target: backend, changeOrigin: true },
  "/health": { target: backend, changeOrigin: true },
  "/ws": { target: backend, changeOrigin: true, ws: true },
};

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
});
