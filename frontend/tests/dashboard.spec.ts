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
    await expect(page.getByTestId("signal-gps")).toContainText("Indisponibil");
    // Read-only: no session controls on the dashboard.
    await expect(page.getByRole("button", { name: /SOS|Sunt bine|Anulează|Pornește/ })).toHaveCount(0);
    expect(backend.requests.every((request) => request.method === "GET")).toBe(true);
  });

  test("phone status from the v2 stream updates the signals without refetching /status each heartbeat", async ({ page }) => {
    const backend = new MockBackend(page);
    const seen = new Date().toISOString();
    const session = makeSession({ client_last_seen_at: seen, connection_lease_expires_at: new Date(Date.now() + 40_000).toISOString() });
    backend.sessions = [session];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("signal-phone")).toContainText("Conectat");
    await expect(page.getByTestId("signal-gps")).toContainText("Indisponibil");
    const statusCalls = backend.calls("/api/v1/status").length;

    for (let beat = 0; beat < 3; beat += 1) {
      backend.publish("client.status_changed", {
        page_visibility: "VISIBLE", connectivity_status: "CONNECTED", audio_status: "CONNECTED", gps_status: "GPS_AVAILABLE",
        power_status: "POWER_STATUS_UNKNOWN", health_status: "HEALTHY", state: "ACTIVE", version: 2 + beat, source: "client_heartbeat",
      });
    }
    await expect(page.getByTestId("signal-gps")).toContainText("Disponibil");
    await expect(page.getByTestId("signal-voice")).toContainText("Audio conectat");
    backend.publish("client.status_changed", {
      page_visibility: "HIDDEN", connectivity_status: "CONNECTED", audio_status: "CONNECTED", gps_status: "GPS_STALE",
      power_status: "POWER_STATUS_UNKNOWN", health_status: "DEGRADED", state: "ACTIVE", version: 9, source: "android",
    });
    await expect(page.getByTestId("signal-gps")).toContainText("Poziție veche");
    await expect(page.getByTestId("signal-phone")).toContainText("pagină în fundal");
    // Routine heartbeats are applied but not logged; the explicit change is.
    await expect(page.getByTestId("log-row")).toHaveCount(1);
    await page.waitForTimeout(600);
    expect(backend.calls("/api/v1/status").length).toBe(statusCalls);
  });

  test("Raspberry Pi power comes from technical.power; no battery percentage", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("signal-power")).toContainText("Neraportată");
    backend.publish("technical.power", { state: "POWER_UNSTABLE", detail: "undervoltage flag" }, null);
    await expect(page.getByTestId("signal-power")).toContainText("POWER_UNSTABLE");
    await expect(page.getByTestId("signal-power")).not.toContainText("%");
  });

  test("v2 alert: reason and notification are readable; coordinates are never displayed or kept", async ({ page }) => {
    const backend = new MockBackend(page);
    const session = makeSession();
    backend.sessions = [session];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");

    const now = new Date().toISOString();
    session.state = "ALERT";
    session.alert_reason = "MISSED_CHECK_INS";
    session.alert = { id: "alert-7", incident_type: "MISSED_CHECK_INS", status: "ACTIVE", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: null, resolution_action: null, version: 1 };
    backend.publish("safety.alerted", { from_state: "UNCONFIRMED", state: "ALERT", reason: "MISSED_CHECK_INS", missed_attempts: 2 });
    backend.publish("alert.notification_requested", {
      alert_id: "alert-7", incident_type: "MISSED_CHECK_INS", gps_status: "GPS_STALE", location_available: true,
      location: { latitude: 45.761234, longitude: 21.229876, captured_at: now, age_seconds: 60, is_stale: true, accuracy_meters: 18, source: "android" },
    });
    await expect(page.getByTestId("alert-row")).toContainText("Verificări fără răspuns");
    await expect(page.getByTestId("alert-location")).toContainText("Atașată alertei");
    await expect(page.getByTestId("alert-location")).toContainText("veche");
    await expect(page.getByText(/45\.7612|21\.2298/)).toHaveCount(0);
  });

  test("the operator creates one-time pairing codes for the phone and the contact", async ({ page }) => {
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/");
    await page.getByTestId("pair-code-companion").click();
    await expect(page.getByTestId("pair-code-value-companion")).toContainText("/companion");
    await page.getByTestId("pair-code-contact").click();
    await expect(page.getByTestId("pair-code-value-contact")).toContainText("/contact");
    expect(backend.calls("/api/v1/pairing-codes").map((call) => call.body?.scope)).toEqual(["companion", "contact"]);
  });
});
