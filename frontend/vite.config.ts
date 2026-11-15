import { existsSync, readFileSync } from "node:fs";
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

// start-lan.ps1 sets VESTA_HTTPS=1: the phone needs https:// for the microphone and location.
// The certificate is a local, self-signed one in .cert/ (never committed).
const https =
  process.env.VESTA_HTTPS === "1" && existsSync(".cert/cert.pem") && existsSync(".cert/key.pem")
    ? { cert: readFileSync(".cert/cert.pem"), key: readFileSync(".cert/key.pem") }
    : undefined;

export default defineConfig({
  plugins: [react()],
  server: { proxy, https },
  preview: { proxy, https },
});
