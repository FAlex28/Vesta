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
    await expect(page.getByText("Locația apare la declanșarea unei alerte")).toBeVisible();
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
    await expect(page.getByTestId("signal-voice")).toContainText("Activ");
    backend.publish("client.status_changed", {
      page_visibility: "HIDDEN", connectivity_status: "CONNECTED", audio_status: "CONNECTED", gps_status: "GPS_STALE",
      power_status: "POWER_STATUS_UNKNOWN", health_status: "DEGRADED", state: "ACTIVE", version: 9, source: "android",
    });
    await expect(page.getByTestId("signal-gps")).toContainText("Poziție veche");
    await expect(page.getByTestId("signal-phone")).toContainText("ecran în fundal");
    // Phone status changes update the signals; the plain-language journal does not list them.
    await expect(page.getByTestId("log-row")).toHaveCount(0);
    await page.waitForTimeout(600);
    expect(backend.calls("/api/v1/status").length).toBe(statusCalls);
  });

  test("Raspberry Pi power comes from technical.power; no battery percentage", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("signal-power")).toContainText("Necunoscută");
    backend.publish("technical.power", { state: "POWER_UNSTABLE", detail: "undervoltage flag" }, null);
    await expect(page.getByTestId("signal-power")).toContainText("Instabilă");
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
    await expect(page.getByTestId("alert-row")).toContainText("Nu a răspuns");
    await expect(page.getByTestId("alert-location")).toContainText("Locație primită");
    await expect(page.getByTestId("alert-location")).toContainText("veche");
    await expect(page.getByText(/45\.7612|21\.2298/)).toHaveCount(0);
  });

  test("the dispatcher hands out no codes and sees the real location only during an active alert", async ({ page }) => {
    const backend = new MockBackend(page);
    const now = new Date().toISOString();
    const session = makeSession();
    backend.sessions = [session];
    backend.locations.set(SESSION_ID, { latitude: 45.761234, longitude: 21.229876, captured_at: now, accuracy_meters: 2000 });
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");
    await expect(page.getByRole("button", { name: /Cod pentru/ })).toHaveCount(0);
    await expect(page.getByTestId("dashboard-location-real")).toHaveCount(0);
    await expect(page.getByText(/45\.7612/)).toHaveCount(0);

    session.state = "ALERT";
    session.alert_reason = "SOS_EXPLICIT";
    session.alert = { id: "alert-5", incident_type: "EXPLICIT_SOS", status: "ACTIVE", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: null, resolution_action: null, version: 1 };
    backend.publish("safety.alerted", { from_state: "ACTIVE", state: "ALERT", reason: "SOS_EXPLICIT" });
    await expect(page.getByTestId("dashboard-location-real")).toContainText("45.76123, 21.22988");
    await expect(page.getByTestId("dashboard-location-imprecise")).toContainText("Poziție imprecisă (± 2000 m).");
    await expect(page.getByText("Locația persoanei · în direct")).toBeVisible();

    // Presentation mode hides the coordinates on a projected screen.
    await page.getByTestId("presentation-toggle").click();
    await expect(page.getByTestId("dashboard-location-real")).toHaveCount(0);
    await expect(page.getByTestId("alert-location")).toHaveText("Coordonatele sunt ascunse în modul prezentare.");
    await page.getByTestId("presentation-toggle").click();

    session.state = "CANCELLED";
    session.alert = { ...session.alert, status: "RESOLVED", resolved_at: now, resolution_action: "USER_CONFIRMED_SAFE" };
    backend.publish("safety.alert_resolved", { from_state: "ALERT", state: "CANCELLED", alert_id: "alert-5" });
    await expect(page.getByTestId("dashboard-location-real")).toHaveCount(0);
  });
  test("dispatcher can take over a live session before an alert", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("live-call")).toContainText("Sesiune în curs");
    await page.getByTestId("take-call").click();
    await expect(page.getByTestId("live-call")).toContainText("Apel preluat");
    await expect(page.getByTestId("ptt-button")).toBeVisible();
    expect(backend.calls(/\/takeover$/)).toHaveLength(1);
    backend.addTurn(SESSION_ID, "operator", "Mesaj vocal — transcriere indisponibilă.");
    await expect(page.getByTestId("call-transcript")).toContainText("Dispecer");
    await expect(page.getByTestId("call-transcript").locator("time")).toHaveCount(1);
  });

  test("an alert arrives as an emergency call: ringing, transcript, location from the device GPS", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");
    await expect(page.getByTestId("live-call")).toContainText("Sesiune în curs");
    const session = backend.session()!;
    const now = new Date().toISOString();
    session.alert_reason = "SOS_EXPLICIT";
    session.alert = { id: "alert-7", incident_type: "EXPLICIT_SOS", status: "ACTIVE", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: null, resolution_action: null, version: 1 };
    backend.locations.set(SESSION_ID, { latitude: 45.74891, longitude: 21.20872, captured_at: now, accuracy_meters: 4, source: "device_gps" });
    backend.addTurn(SESSION_ID, "assistant", "Bună, sunt Vesta. A fost o apăsare din greșeală?");
    backend.addTurn(SESSION_ID, "person", "Nu, am nevoie de ajutor!", "ESCALATE");
    backend.transition(session, "ALERT", "safety.alerted", { reason: "SOS_EXPLICIT", source: "voice_agent" });

    await expect(page.getByTestId("live-call")).toContainText("Apel de urgență");
    await expect(page.getByTestId("call-transcript")).toContainText("Nu, am nevoie de ajutor!");
    await expect(page.getByTestId("call-transcript")).toContainText("Vesta");
    await expect(page.getByTestId("call-transcript")).toContainText("Persoana");
    await expect(page.getByTestId("call-transcript").locator("time")).toHaveCount(2);
    await expect(page.getByTestId("location-source")).toHaveText("GPS dispozitiv");
    await page.getByTestId("take-call").click();
    await expect(page.getByTestId("take-call")).toHaveCount(0);
    await expect(page.getByTestId("live-call")).toContainText("Apel preluat");
    await expect(page.getByTestId("ptt-button")).toBeVisible();
    expect(backend.calls(/\/takeover$/)).toHaveLength(1);
    await expect(page.getByTestId("call-audio-status")).toHaveAttribute("data-status", /unavailable|connecting|error/);

    // Presentation mode hides the transcript as well as the coordinates.
    await page.getByTestId("presentation-toggle").click();
    await expect(page.getByTestId("call-transcript")).toContainText("Ascunsă în modul prezentare");
    await expect(page.getByTestId("call-transcript")).not.toContainText("am nevoie de ajutor");
  });

  test("a false alarm leaves no call, no transcript and no location", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ state: "CANCELLED", cancellation_reason: "FALSE_ALARM" })];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("false-alarm-notice")).toContainText("Alarmă falsă");
    await expect(page.getByTestId("live-call")).toHaveCount(0);
    expect(backend.calls(/\/conversation$/)).toHaveLength(0);
  });

  test("the journal speaks plainly: what happened, not technical states", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/");
    await expect(page.getByTestId("session-title")).toHaveAttribute("data-state", "ACTIVE");
    const session = backend.session()!;
    backend.publish("safety.check_in_requested", { from_state: "ACTIVE", state: "CHECK_IN", attempt: 1 }, session.id);
    backend.publish("location.updated", { alert_id: "a" }, session.id);
    backend.publish("conversation.updated", { role: "assistant", action: null }, session.id);
    backend.publish("conversation.updated", { role: "person", action: "ESCALATE" }, session.id);
    backend.publish("safety.alerted", { from_state: "CHECK_IN", state: "ALERT", reason: "SOS_EXPLICIT", source: "voice_agent" }, session.id);
    const log = page.locator("#events");
    await expect(log).toContainText("ALERTĂ: a cerut ajutor vocal");
    await expect(log).toContainText("Ea i-a răspuns lui Vesta");
    await expect(log).toContainText("Vesta o întreabă dacă e bine");
    await expect(log).not.toContainText("conversation.updated");
    await expect(log).not.toContainText("CHECK_IN →");
    await expect(page.getByTestId("log-row")).toHaveCount(3);
  });

  test("recordings of past alerts are listed with their transcript", async ({ page }) => {
    const backend = new MockBackend(page);
    const now = new Date().toISOString();
    backend.sessions = [
      makeSession({
        state: "CANCELLED",
        alert_reason: "MISSED_CHECK_INS",
        alert: { id: "alert-old", incident_type: "MISSED_CHECK_INS", status: "RESOLVED", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: now, resolution_action: "USER_CONFIRMED_SAFE", version: 2 },
      }),
    ];
    backend.chunks.set(SESSION_ID, [0, 1, 2]);
    backend.turns.set(SESSION_ID, [
      { id: "t1", role: "assistant", text: "Ești bine?", at: now, action: null },
      { id: "t2", role: "person", text: "Da.", at: now, action: null },
      { id: "t3", role: "operator", text: "Mesaj vocal — transcriere indisponibilă.", at: now, action: null },
    ]);
    await backend.install();
    await page.goto("/");
    const row = page.getByTestId("rec-row");
    await expect(row).toHaveCount(1);
    await expect(row).toContainText("Nu a răspuns");
    await row.getByRole("button", { name: /Transcriere/ }).click();
    await expect(row).toContainText("Ești bine?");
    await expect(row).toContainText("Vesta");
    await expect(row).toContainText("Persoana");
    await expect(row).toContainText("Dispecer");
    await expect(row.locator("time")).toHaveCount(3);
  });
});
