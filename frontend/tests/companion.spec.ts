import { expect, test, type Page } from "playwright/test";
import { bodyProblem, COMPANION_TOKEN, installDeviceFakes, makeSession, MockBackend, SESSION_ID, TOKEN } from "./mockBackend";

async function openPaired(page: Page, backend: MockBackend) {
  await backend.install();
  await page.goto("/companion");
  await expect(page.getByTestId("c-state")).not.toHaveAttribute("data-state", "UNKNOWN");
  await page.getByLabel("Cod de control al sesiunii").fill(TOKEN);
  await page.getByRole("button", { name: "Asociază telefonul" }).click();
  await expect(page.getByText("Sesiunea ta")).toBeVisible();
}

test.describe("companion", () => {
  test("pairing with a dashboard code: the phone waits, then takes over the joystick session by itself", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/companion");
    const code = backend.issueCode("companion");
    await page.getByLabel("Codul de asociere din panoul Vesta").fill(`${code.slice(0, 4).toLowerCase()} ${code.slice(4)}`);
    await page.getByRole("button", { name: "Asociază cu codul" }).click();
    await expect(page.getByTestId("pair-waiting")).toContainText("vesta-01");
    await expect(page.getByTestId("sos")).toBeDisabled();

    // The joystick is held for 5 s and the backend confirms a session.
    backend.sessions = [makeSession()];
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("sos")).toBeEnabled();
    expect(backend.calls("/api/v1/companion/session").every((call) => call.auth === `Bearer ${COMPANION_TOKEN}`)).toBe(true);
    expect(backend.calls(/\/heartbeat$/).every((call) => call.auth === `Bearer ${TOKEN}`)).toBe(true);
    expect(backend.requests.some((request) => request.url.includes(COMPANION_TOKEN) || request.url.includes(TOKEN))).toBe(false);
    expect(backend.requests.some((request) => request.path.includes("device/events"))).toBe(false);

    // The grant survives a reload (the control token does not: it is fetched again).
    await page.reload();
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    expect(await page.evaluate(() => JSON.stringify(window.localStorage))).not.toContain(TOKEN);
  });

  test("a wrong or reused code is refused and nothing is stored", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/companion");
    await page.getByLabel("Codul de asociere din panoul Vesta").fill("ZZZZ9999");
    await page.getByRole("button", { name: "Asociază cu codul" }).click();
    await expect(page.getByTestId("claim-problem")).toContainText("nu este valid");
    expect(await page.evaluate(() => window.localStorage.length)).toBe(0);
  });

  test("a check-in shown on the open phone page is reported as delivered once", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    const session = makeSession();
    backend.sessions = [session];
    await openPaired(page, backend);
    session.state = "CHECK_IN";
    session.checkin_id = "chk-77";
    session.checkin_status = "REQUESTED";
    session.response_deadline = new Date(Date.now() + 15_000).toISOString();
    backend.publish("safety.check_in_requested", { from_state: "ACTIVE", state: "CHECK_IN", attempt: 1, checkin_id: "chk-77", response_deadline: session.response_deadline });
    await expect(page.getByTestId("checkin-prompt")).toBeVisible();
    await expect.poll(() => backend.calls(/\/checkins\/chk-77\/delivery$/).map((call) => call.body?.channel)).toEqual(["android"]);
    await page.waitForTimeout(500);
    expect(backend.calls(/\/delivery$/)).toHaveLength(1);
  });

  test("no permission is requested on load; controls wait for pairing", async ({ page }) => {
    await installDeviceFakes(page);
    await page.addInitScript(() => {
      const w = window as unknown as Record<string, number>;
      w.__permissionCalls = 0;
      const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = (constraints) => {
        w.__permissionCalls += 1;
        return original(constraints);
      };
    });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/companion");
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ACTIVE");
    await expect(page.getByTestId("sos")).toBeVisible();
    await expect(page.getByTestId("sos")).toBeDisabled();
    expect(await page.evaluate(() => (window as unknown as Record<string, number>).__permissionCalls)).toBe(0);
    await expect(page.getByTestId("gps-freshness")).toHaveText("UNAVAILABLE");
  });

  test("unauthorized token is rejected; token never appears in a URL", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/companion");
    await page.getByLabel("Cod de control al sesiunii").fill("v1.wrong.token");
    await page.getByRole("button", { name: "Asociază telefonul" }).click();
    await expect(page.getByTestId("pairing-problem")).toContainText("respins");
    await expect(page.getByTestId("sos")).toBeDisabled();
    expect(backend.requests.some((request) => request.url.includes("v1.wrong.token"))).toBe(false);
    expect(page.url()).not.toContain("token");
  });

  test("manual session-ID pairing validates through the token-scoped v2 endpoint", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await backend.install();
    await page.goto("/companion");
    await page.getByLabel("Identificatorul sesiunii").fill(SESSION_ID);
    await page.getByLabel("Cod de control al sesiunii").fill(TOKEN);
    await page.getByRole("button", { name: "Asociază telefonul" }).click();
    await expect(page.getByText("Sesiunea ta")).toBeVisible();
    const sessionRead = backend.calls(new RegExp(`/api/v1/sessions/${SESSION_ID}$`));
    expect(sessionRead.length).toBeGreaterThanOrEqual(1);
    expect(sessionRead.every((request) => request.auth === `Bearer ${TOKEN}`)).toBe(true);
    expect(page.url()).not.toContain(TOKEN);
  });

  test("quick cancel ends an accidental activation through the backend", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);
    expect(backend.calls(/\/heartbeat$/).every((request) => request.auth === `Bearer ${TOKEN}`)).toBe(true);

    await page.getByTestId("quick-cancel").click();
    await expect(page.getByText(/Serverul a încheiat sesiunea la/)).toBeVisible();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "CANCELLED");
    const cancel = backend.calls(/\/cancel$/);
    expect(cancel).toHaveLength(1);
    expect(String(cancel[0].body?.event_id)).toMatch(/^cancel-/);
    expect(cancel[0].body?.reason).toBe("ACCIDENTAL_ACTIVATION");
  });

  test("ending a session after the quick-cancel window sends USER_CANCELLED", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ quick_cancel_until: new Date(Date.now() - 1_000).toISOString() })];
    await openPaired(page, backend);
    await expect(page.getByTestId("quick-cancel")).toHaveCount(0);
    await page.getByTestId("end").click();
    await page.getByTestId("end-confirm").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "CANCELLED");
    expect(backend.calls(/\/cancel$/).map((call) => call.body?.reason)).toEqual(["USER_CANCELLED"]);
  });

  test("check-in prompt and 'Sunt bine' confirm only after the server answers", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    const session = makeSession();
    backend.sessions = [session];
    await openPaired(page, backend);

    session.state = "CHECK_IN";
    session.checkin_attempt = 1;
    session.checkin_id = "checkin-2026-01";
    session.checkin_status = "REQUESTED";
    session.response_deadline = new Date(Date.now() + 15_000).toISOString();
    backend.publish("safety.check_in_requested", {
      from_state: "ACTIVE",
      state: "CHECK_IN",
      attempt: 1,
      checkin_id: session.checkin_id,
      response_deadline: session.response_deadline,
    });
    await expect(page.getByTestId("checkin-prompt")).toBeVisible();

    await page.getByTestId("checkin-prompt").getByRole("button", { name: "Sunt bine" }).click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ACTIVE");
    // The prompt closes with the server's answer; the confirmation itself stays visible.
    await expect(page.getByTestId("checkin-prompt")).toHaveCount(0);
    await expect(page.getByText(/Serverul a primit confirmarea la/)).toBeVisible();
    expect(backend.calls(/\/checkins$/)).toHaveLength(1);
    expect(backend.calls(/\/checkins$/)[0].body).toMatchObject({
      checkin_id: "checkin-2026-01",
      confirmation: "safe",
      source: "android",
    });
  });

  test("SOS succeeds only on the server's answer, and a failed SOS says so and retries with the same event_id", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);

    let failSos = true;
    backend.responders.push((request) => (request.path.endsWith("/sos") && failSos ? "abort" : undefined));
    await page.getByTestId("sos").click();
    await expect(page.getByText("Alerta NU a fost confirmată ca transmisă.")).toBeVisible();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ACTIVE");
    await expect(page.getByTestId("sos")).toHaveText("Reîncearcă SOS");

    failSos = false;
    await page.getByTestId("sos").click();
    await expect(page.getByText(/Serverul a confirmat SOS-ul la/)).toBeVisible();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ALERT");
    const sosCalls = backend.calls(/\/sos$/);
    expect(sosCalls).toHaveLength(2);
    expect(sosCalls[0].body?.event_id).toBe(sosCalls[1].body?.event_id);
  });

  test("SOS works while the voice service is unavailable", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    backend.responders.push((request) =>
      request.path.endsWith("/voice/access")
        ? { status: 503, body: { error: { code: "VOICE_PROVIDER_UNAVAILABLE", message: "unavailable" } } }
        : undefined,
    );
    await openPaired(page, backend);
    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "unavailable");
    await expect(page.getByText("Conversația vocală este momentan indisponibilă.")).toBeVisible();
    await expect(page.getByTestId("sos")).toBeEnabled();
    await page.getByTestId("sos").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ALERT");
  });

  test("voice: one conversation, check-in context, audio disconnect is shown and reported", async ({ page }) => {
    await installDeviceFakes(page, { mic: "grant" });
    const backend = new MockBackend(page);
    const session = makeSession();
    backend.sessions = [session];
    await openPaired(page, backend);

    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "connected");
    expect(backend.calls(/\/voice\/access$/)).toHaveLength(1);
    await expect(page.getByTestId("voice-start")).toHaveCount(0); // no second conversation can start

    session.state = "CHECK_IN";
    backend.publish("safety.check_in_requested", { from_state: "ACTIVE", state: "CHECK_IN", attempt: 1 });
    await expect(page.getByTestId("checkin-prompt")).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as unknown as { __voiceLog: string[] }).__voiceLog)).toContain("context:safety_check_requested");

    // An agent message is shown as sent, not as heard.
    await page.evaluate(() => (window as unknown as { __voiceOptions: { onMessage: (m: unknown) => void } }).__voiceOptions.onMessage({ message: "Ești în regulă?", role: "agent" }));
    await expect(page.getByText("Textul nu confirmă că mesajul a fost auzit.", { exact: false })).toBeVisible();

    await expect.poll(() => backend.calls(/\/voice\/status$/).map((call) => `${call.body?.status}:${call.body?.conversation_id}`)).toEqual(["CONNECTED:conv_123"]);

    await page.evaluate(() =>
      (window as unknown as { __voiceOptions: { onDisconnect: (d: unknown) => void } }).__voiceOptions.onDisconnect({ reason: "error", message: "ice failed" }),
    );
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "disconnected");
    await expect(page.getByTestId("voice-status")).toHaveText(/Audio deconectat/);
    await expect.poll(() => backend.calls(/\/voice\/status$/).map((call) => call.body?.status)).toEqual(["CONNECTED", "DISCONNECTED"]);

    // The reservation was released, so the server accepts a new conversation.
    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "connected");
    expect(backend.calls(/\/voice\/access$/)).toHaveLength(2);
  });

  test("ending a conversation releases the server reservation; a new one can start", async ({ page }) => {
    await installDeviceFakes(page, { mic: "grant" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);

    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "connected");
    await page.getByRole("button", { name: "Încheie conversația" }).click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "ended");
    // Exactly one DISCONNECTED, although both stop() and onDisconnect ran.
    await expect.poll(() => backend.calls(/\/voice\/status$/).map((call) => call.body?.status)).toEqual(["CONNECTED", "DISCONNECTED"]);

    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "connected");
    expect(backend.calls(/\/voice\/access$/)).toHaveLength(2);
  });

  test("a conversation already open elsewhere is explained, not shown as a generic failure", async ({ page }) => {
    await installDeviceFakes(page, { mic: "grant" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    backend.voiceReserved = true;
    await openPaired(page, backend);
    await page.getByTestId("voice-start").click();
    await expect(page.getByText(/deja o conversație deschisă/)).toBeVisible();
    await expect(page.getByTestId("sos")).toBeEnabled();
  });

  test("microphone refused: shown as a microphone error and reported, session not blocked", async ({ page }) => {
    await installDeviceFakes(page, { mic: "deny" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);
    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveText(/Eroare microfon/);
    await expect.poll(() => backend.calls(/\/voice\/status$/).map((call) => call.body?.status)).toContain("MICROPHONE_ERROR");
    expect(backend.calls(/\/voice\/access$/)).toHaveLength(0);
    await expect(page.getByTestId("sos")).toBeEnabled();
  });

  test("GPS refused shows UNAVAILABLE; a stale fix is marked STALE, never current", async ({ page }) => {
    await installDeviceFakes(page, { gps: "deny" });
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/companion");
    await page.getByRole("button", { name: "Permite locația" }).click();
    await expect(page.getByText("Accesul la locație a fost refuzat.")).toBeVisible();
    await expect(page.getByTestId("gps-freshness")).toHaveText("UNAVAILABLE");

    await page.getByRole("button", { name: "Arată coordonate simulate" }).click();
    await expect(page.getByText("Simulate", { exact: true })).toBeVisible();
  });

  test("stale GPS fix", async ({ page }) => {
    await installDeviceFakes(page, { gps: "stale" });
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/companion");
    await page.getByRole("button", { name: "Permite locația" }).click();
    await expect(page.getByTestId("gps-freshness")).toHaveText("STALE");
    await expect(page.getByText("Aceasta este ultima poziție cunoscută, nu poziția actuală.")).toBeVisible();
  });

  test("GPS sharing is off by default and uploads only after explicit consent", async ({ page }) => {
    await installDeviceFakes(page, { gps: "grant" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);
    await page.getByRole("button", { name: "Permite locația" }).click();
    await expect(page.getByTestId("gps-freshness")).toHaveText("CURRENT");

    const share = page.getByTestId("location-share");
    await expect(share).toBeEnabled();
    await expect(share).not.toBeChecked();
    expect(backend.calls(/\/location$/)).toHaveLength(0);

    await share.check();
    await expect(page.getByTestId("location-upload-status")).toHaveText("Poziția a fost primită de server.");
    const sent = backend.calls(/\/location$/);
    expect(sent).toHaveLength(1);
    // Turning GPS on reports only its availability; coordinates went out once, after consent.
    expect(backend.calls(/\/client-status$/).map((call) => call.body?.gps_status)).toContain("GPS_AVAILABLE");
    expect(backend.calls(/\/client-status$/).every((call) => !("latitude" in (call.body ?? {})))).toBe(true);
    expect(sent[0].auth).toBe(`Bearer ${TOKEN}`);
    expect(sent[0].body).toMatchObject({ latitude: 45.75, longitude: 21.23, source: "android" });
    expect(String(sent[0].body?.event_id)).toMatch(/^location-/);
  });

  test("phone loses the server: heartbeats fail -> DISCONNECTED; recovery -> CONNECTED without a new session", async ({ page }) => {
    await page.clock.install();
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);

    let offline = true;
    backend.responders.push((request) => (offline && request.path.endsWith("/heartbeat") ? "abort" : undefined));
    await page.clock.fastForward(15_100);
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "RECONNECTING");
    await page.clock.fastForward(15_100);
    await page.clock.fastForward(15_100);
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "DISCONNECTED");

    offline = false;
    await page.clock.fastForward(15_100);
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "CONNECTED");
    expect(backend.sessions).toHaveLength(1);
    expect(backend.requests.some((request) => request.path.includes("device/events"))).toBe(false);
  });

  test("hidden page and return: resync with the server and tell the user", async ({ page }) => {
    await page.clock.install();
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);
    const sessionRead = new RegExp(`/api/v1/sessions/${SESSION_ID}$`);
    const before = backend.calls(sessionRead).length;

    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.clock.fastForward(12_000);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect(page.getByTestId("away-notice")).toBeVisible();
    await expect.poll(() => backend.calls(sessionRead).length).toBeGreaterThan(before);
    await expect
      .poll(() => backend.calls(/\/client-status$/).map((call) => call.body?.page_visibility).filter(Boolean))
      .toEqual(["VISIBLE", "HIDDEN", "VISIBLE"]);
  });

  test("backend restart: the companion reports the interrupted session and asks to pair again", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    backend.sequence = 4;
    await openPaired(page, backend);

    backend.sessions = [];
    backend.sequence = 0;
    await backend.dropSockets();
    await expect(page.getByTestId("pairing-problem")).toContainText("întreruptă", { timeout: 15_000 });
    await expect(page.getByTestId("sos")).toBeDisabled();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "IDLE");
  });

  test("closing an alert uses the explicit resolve endpoint and keeps the alert as RESOLVED", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    const now = new Date().toISOString();
    backend.sessions = [
      makeSession({
        state: "ALERT",
        alert_reason: "SOS_EXPLICIT",
        quick_cancel_until: new Date(Date.now() - 60_000).toISOString(),
        alert: { id: "alert-42", incident_type: "EXPLICIT_SOS", status: "ACTIVE", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: null, resolution_action: null, version: 1 },
      }),
    ];
    await openPaired(page, backend);
    await page.getByTestId("end").click();
    await expect(page.getByText("Alerta deja emisă rămâne în istoric ca rezolvată.", { exact: false })).toBeVisible();
    await page.getByTestId("end-confirm").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "CANCELLED");
    await expect(page.getByText(/Serverul a închis alerta la/)).toBeVisible();
    expect(backend.calls(/\/cancel$/)).toHaveLength(0);
    const resolve = backend.calls(/\/alerts\/alert-42\/resolve$/);
    expect(resolve).toHaveLength(1);
    expect(resolve[0].body).toMatchObject({ action: "USER_CONFIRMED_SAFE" });
    expect(backend.session(SESSION_ID)?.alert).toMatchObject({ status: "RESOLVED", resolution_action: "USER_CONFIRMED_SAFE" });
  });

  test("every request the phone sends matches the backend schema (no 422)", async ({ page }) => {
    await installDeviceFakes(page, { mic: "grant", gps: "grant" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);
    await page.getByRole("button", { name: "Permite locația" }).click();
    await page.getByTestId("location-share").check();
    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "connected");
    await page.getByTestId("sos").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ALERT");
    await page.getByTestId("end").click();
    await page.getByTestId("end-confirm").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "CANCELLED");
    const posts = backend.requests.filter((request) => request.method === "POST");
    expect(posts.length).toBeGreaterThan(5);
    for (const post of posts) {
      const action = post.path.replace(/^\/api\/v1\/sessions\/[^/]+\//, "").replace(/^alerts\/[^/]+\/resolve$/, "resolve");
      expect(bodyProblem(action, post.body), `${post.path} ${JSON.stringify(post.body)}`).toBeNull();
    }
  });
});
