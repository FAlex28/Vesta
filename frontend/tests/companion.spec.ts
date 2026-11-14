import { expect, test, type Page } from "playwright/test";
import { installDeviceFakes, makeSession, MockBackend, SESSION_ID, TOKEN } from "./mockBackend";

async function openPaired(page: Page, backend: MockBackend) {
  await backend.install();
  await page.goto("/companion");
  await expect(page.getByTestId("c-state")).not.toHaveAttribute("data-state", "UNKNOWN");
  await page.getByLabel("Cod de control al sesiunii").fill(TOKEN);
  await page.getByRole("button", { name: "Asociază telefonul" }).click();
  await expect(page.getByText("Sesiunea ta")).toBeVisible();
}

test.describe("companion", () => {
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

    await page.evaluate(() =>
      (window as unknown as { __voiceOptions: { onDisconnect: (d: unknown) => void } }).__voiceOptions.onDisconnect({ reason: "error", message: "ice failed" }),
    );
    await expect(page.getByTestId("voice-status")).toHaveAttribute("data-phase", "disconnected");
    await expect(page.getByTestId("voice-status")).toHaveText(/Audio deconectat/);
    await expect.poll(() => backend.calls(/\/technical-status$/).map((call) => `${call.body?.issue}:${call.body?.active}`)).toContain("audio_interrupted:true");
  });

  test("microphone refused: shown as a microphone error and reported, session not blocked", async ({ page }) => {
    await installDeviceFakes(page, { mic: "deny" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await openPaired(page, backend);
    await page.getByTestId("voice-start").click();
    await expect(page.getByTestId("voice-status")).toHaveText(/Eroare microfon/);
    await expect.poll(() => backend.calls(/\/technical-status$/).map((call) => call.body?.issue)).toContain("microphone_denied");
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
    await page.clock.fastForward(7_100);
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "RECONNECTING");
    await page.clock.fastForward(7_100);
    await page.clock.fastForward(7_100);
    await expect(page.getByTestId("link-badge")).toHaveAttribute("data-state", "DISCONNECTED");

    offline = false;
    await page.clock.fastForward(7_100);
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

  test("closing an alert keeps it as resolved", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ state: "ALERT", alert_reason: "explicit_sos" })];
    await openPaired(page, backend);
    await page.getByTestId("end").click();
    await expect(page.getByText("Alerta deja emisă rămâne în istoric ca rezolvată.", { exact: false })).toBeVisible();
    await page.getByTestId("end-confirm").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "CANCELLED");
    expect(backend.session(SESSION_ID)?.alert_reason).toBe("explicit_sos");
  });
});
