import { expect, test } from "playwright/test";
import { makeSession, MockBackend, SESSION_ID } from "./mockBackend";

test.describe("dashboard", () => {
  test("removed hardware and tutorial copy are absent; IDLE waits for the backend", async ({ page }) => {
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/");

    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "IDLE");
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "CONNECTED");
    for (const word of ["ESP32", "Arduino", "Senzori", "Umiditate", "Lumină", "proximitate", "buzzer", "Demo", "UniHack"]) {
      await expect(page.getByText(word, { exact: false })).toHaveCount(0);
    }
    await expect(page.getByRole("heading", { name: "Cum pornește Vesta" })).toHaveCount(0);

    // Longer than the 5 s hold: without a backend event the page must stay IDLE.
    await page.waitForTimeout(6_000);
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "IDLE");

    // Backend confirms the activation: a new session appears via event + status refresh.
    backend.sessions = [makeSession()];
    backend.publish("session.activated", { from_state: "IDLE", state: "ACTIVE" });
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");
    await expect(page.getByTestId("log-row")).toHaveCount(1);
  });

  test("repeated activation events and a second press keep one session and one log entry", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");

    const confirmed = backend.event("safety.check_in_confirmed", { from_state: "CHECK_IN", state: "ACTIVE" });
    backend.send(confirmed);
    backend.send(confirmed); // same event_id
    backend.send({ ...confirmed, event_id: "replayed-old-sequence" }); // same sequence
    await expect(page.getByTestId("log-row")).toHaveCount(1);
    // Re-pressing the joystick yields 409 SESSION_ALREADY_ACTIVE on the backend: no event, one session.
    await expect(page.getByText("mai multe sesiuni deschise")).toHaveCount(0);
  });

  test("duplicate alert events show one alert; cancellation turns it RESOLVED with a closing time", async ({ page }) => {
    const backend = new MockBackend(page);
    const session = makeSession();
    backend.sessions = [session];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");

    session.state = "ALERT";
    session.alert_reason = "explicit_sos";
    const alerted = backend.event("safety.alerted", { from_state: "ACTIVE", state: "ALERT", reason: "explicit_sos" });
    backend.send(alerted);
    backend.send(alerted);
    await expect(page.getByTestId("alert-row")).toHaveCount(1);
    await expect(page.getByTestId("alert-row")).toContainText("ALERT");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ALERT");

    session.state = "CANCELLED";
    backend.publish("session.cancelled", { from_state: "ALERT", state: "CANCELLED" });
    await expect(page.getByTestId("alert-row")).toHaveCount(1);
    await expect(page.getByTestId("alert-row")).toContainText("RESOLVED");
    await expect(page.getByTestId("alert-row").locator("dd").nth(1)).not.toHaveText("—");
  });

  test("lost stream shows the last known state, reconnects and refreshes from /status", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "CONNECTED");
    const statusCallsBefore = backend.calls("/api/v1/status").length;

    backend.acceptSockets = false;
    await backend.dropSockets();
    await expect(page.getByTestId("link-badge")).not.toHaveAttribute("data-state", "CONNECTED");
    await expect(page.getByTestId("stale-banner")).toBeVisible();

    backend.acceptSockets = true;
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "CONNECTED", { timeout: 15_000 });
    await expect(page.getByTestId("stale-banner")).toHaveCount(0);
    expect(backend.calls("/api/v1/status").length).toBeGreaterThan(statusCallsBefore);
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");
  });

  test("backend restart: the old session is reported as interrupted, not restored", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    backend.sequence = 7;
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");

    backend.sessions = [];
    backend.sequence = 0;
    await backend.dropSockets();
    await expect(page.getByTestId("restart-banner")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("restart-banner")).toContainText("întreruptă");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "IDLE");
  });

  test("power and phone warnings come from the backend; no battery percentage", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ active_technical_issues: ["low_voltage", "network_disconnected"], client_last_seen_at: new Date().toISOString() })];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("signal-power")).toContainText("LOW_VOLTAGE");
    await expect(page.getByTestId("signal-phone")).toContainText("Deconectat");
    await expect(page.getByTestId("signal-power")).not.toContainText("%");
  });

  test("the public dashboard shows only the labelled test point, never live coordinates", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByText("Punct de referință simulat, nu este locația unei persoane")).toBeVisible();
    await expect(page.getByTestId("signal-gps")).toContainText("UNAVAILABLE");
    // Read-only: no session controls on the dashboard.
    await expect(page.getByRole("button", { name: /SOS|Sunt bine|Anulează|Pornește/ })).toHaveCount(0);
    expect(backend.requests.every((request) => request.method === "GET")).toBe(true);
  });

  test("contact page reflects ALERT then RESOLVED from backend events, without coordinates", async ({ page }) => {
    const backend = new MockBackend(page);
    const session = makeSession({ state: "ALERT", alert_reason: "missed_check_ins" });
    backend.sessions = [session];
    await backend.install();
    await page.goto("/contact");
    await expect(page.getByTestId("contact-state")).toHaveText(/Alertă activă/);
    await expect(page.getByTestId("alert-row")).toContainText("Verificări fără răspuns");

    session.state = "CANCELLED";
    backend.publish("session.cancelled", { from_state: "ALERT", state: "CANCELLED" }, SESSION_ID);
    await expect(page.getByTestId("contact-state")).toHaveText(/Nicio alertă activă/);
    await expect(page.getByTestId("alert-row")).toContainText("RESOLVED");
    await expect(page.getByText(/\d+\.\d{4,}, \d+\.\d{4,}/)).toHaveCount(0);
  });
});
