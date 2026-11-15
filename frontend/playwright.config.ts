import { defineConfig, devices } from "playwright/test";

/*
 * Frontend tests with a mocked backend. The dev server runs in Vite "test" mode (enables the
 * voice test hook and the test-only manual pairing form) and its backend proxy points at a
 * closed port, so an unmocked request fails instead of reaching a real server. No test calls
 * emergency services, sends notifications or asks the OS for microphone/location permission.
 */
const PORT = 5179;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  // Each test drives timers, audio and maps: fewer parallel workers keeps them deterministic.
  workers: 3,
  reporter: [["list"]],
  timeout: 30_000,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], channel: "chrome" }, testIgnore: /companion\.spec/ },
    { name: "android", use: { ...devices["Pixel 7"], channel: "chrome" }, testIgnore: /liveState.spec/ },
  ],
  webServer: {
    command: `npx vite --mode test --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      VITE_VESTA_MANUAL_PAIRING: "true",
      VESTA_BACKEND_URL: "http://127.0.0.1:9",
    },
  },
});
