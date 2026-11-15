import { expect, test, type Page } from "playwright/test";
import { bodyProblem, COMPANION_TOKEN, installDeviceFakes, makeSession, MockBackend, SESSION_ID, TOKEN } from "./mockBackend";

/* The person's phone (v5): no pairing code, Vesta in the earbuds, recording, false alarm. */

const checkIn = (overrides = {}) =>
  makeSession({
    state: "CHECK_IN",
    checkin_id: "chk-1",
    checkin_status: "REQUESTED",
    checkin_attempt: 1,
    response_deadline: new Date(Date.now() + 20_000).toISOString(),
    ...overrides,
  });

async function open(page: Page, backend: MockBackend) {
  await backend.install();
  await page.goto("/companion");
}

async function arm(page: Page) {
  await page.getByTestId("arm").click();
  await expect(page.getByTestId("assistant-phase")).not.toHaveAttribute("data-phase", "off");
}

test.describe("companion", () => {
  test("opens without any code and takes over the joystick session by itself", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    await open(page, backend);
    await expect(page.getByText("Ține butonul apăsat 5 secunde")).toBeVisible();
    await expect(page.getByLabel("Codul de pe dispozitivul Vesta")).toHaveCount(0);
    expect(backend.calls("/api/v1/access/open")[0].body).toEqual({ scope: "companion" });
    // nothing is requested from the browser on load
    expect(await page.evaluate(() => (window as unknown as { __micRequests?: number }).__micRequests ?? 0)).toBe(0);

    backend.sessions = [makeSession()];
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("sos")).toBeEnabled();
    expect(backend.calls("/api/v1/companion/session").every((call) => call.auth === `Bearer ${COMPANION_TOKEN}`)).toBe(true);
    expect(backend.requests.some((request) => request.url.includes(COMPANION_TOKEN) || request.url.includes(TOKEN))).toBe(false);

    // The grant survives a reload; the control token is fetched again, never stored.
    await page.reload();
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    expect(await page.evaluate(() => JSON.stringify(window.localStorage))).not.toContain(TOKEN);
    expect(backend.calls("/api/v1/access/open")).toHaveLength(1);
  });

  test("a server without open access says so", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.openAccess = false;
    await open(page, backend);
    await expect(page.getByTestId("access-problem")).toContainText("nu se poate conecta");
  });

  test("the question is spoken in the earbuds and reported as delivered by voice", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await arm(page);

    const session = backend.session()!;
    Object.assign(session, checkIn());
    backend.publish("safety.check_in_requested", { from_state: "ACTIVE", state: "CHECK_IN", checkin_id: "chk-1", attempt: 1 });
    await expect(page.getByTestId("chat")).toContainText("A fost o apăsare din greșeală?");
    await expect.poll(() => backend.calls(/\/delivery$/).length).toBe(1);
    expect(backend.calls(/\/delivery$/)[0].body).toMatchObject({ status: "DELIVERED", channel: "voice" });
    expect(backend.calls(/\/assistant\/prompt$/)[0].body).toMatchObject({ line: "checkin" });
    expect(await page.evaluate(() => (window as unknown as { __spoken?: string[] }).__spoken ?? [])).toContain(
      "Bună, sunt Vesta. A fost o apăsare din greșeală?",
    );
    await expect(page.getByTestId("no-understanding")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sunt bine, continuă" })).toHaveCount(0);
    await expect(page.getByTestId("sos")).toBeVisible();
    await expect(page.getByTestId("false-alarm")).toBeVisible();
    await page.evaluate(() => (window as unknown as { __speak: (on: boolean) => void }).__speak(true));
    await page.waitForTimeout(1200);
    await page.evaluate(() => (window as unknown as { __speak: (on: boolean) => void }).__speak(false));
    await expect.poll(() => backend.calls(/\/assistant\/reply$/).length, { timeout: 10_000 }).toBe(1);
    await expect(page.getByTestId("chat")).toContainText("Mesajul nu a putut fi transcris.");
    await expect(page.getByTestId("chat")).toContainText("Nu am putut transcrie ce ai spus.");
  });

  test("without the earbuds prepared, the question on the open screen counts as delivered", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [checkIn()];
    await open(page, backend);
    await expect.poll(() => backend.calls(/\/delivery$/).length, { timeout: 10_000 }).toBe(1);
    expect(backend.calls(/\/delivery$/)[0].body).toMatchObject({ channel: "android" });
  });

  test("the whole conversation is recorded from the start and uploaded in order", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await arm(page);
    await expect.poll(() => backend.calls(/\/recording\/chunks$/).length, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
    const uploads = backend.calls(/\/recording\/chunks$/);
    expect(uploads.map((call) => Number(new URL(call.url).searchParams.get("seq"))).slice(0, 2)).toEqual([0, 1]);
    expect(uploads.every((call) => call.auth === `Bearer ${TOKEN}`)).toBe(true);
    await expect(page.getByTestId("recording-status")).toContainText(/\d+ s/);
  });

  test("Gemini hears 'it was a mistake': false alarm, Vesta stops", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.assistant = { understanding: true, voice: false };
    backend.sessions = [checkIn()];
    backend.nextReply = { transcript: "Da, a fost din greșeală.", reply: "Am înțeles, a fost o alarmă falsă.", action: "FALSE_ALARM" };
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await arm(page);
    await expect(page.getByTestId("assistant-phase")).toHaveAttribute("data-phase", "listening", { timeout: 10_000 });

    // She speaks for a second, then stops: one phrase is detected and sent as WAV.
    await page.evaluate(() => (window as unknown as { __speak: (on: boolean) => void }).__speak(true));
    await page.waitForTimeout(1200);
    await page.evaluate(() => (window as unknown as { __speak: (on: boolean) => void }).__speak(false));
    await expect.poll(() => backend.calls(/\/assistant\/reply$/).length, { timeout: 10_000 }).toBe(1);
    const sent = backend.calls(/\/assistant\/reply$/)[0].body!;
    expect(sent.mime_type).toBe("audio/wav");
    expect(atob(String(sent.audio_base64)).slice(0, 4)).toBe("RIFF");

    await expect(page.getByTestId("c-state")).toContainText("Alarmă falsă", { timeout: 10_000 });
    await expect(page.getByTestId("chat")).toContainText("Da, a fost din greșeală.");
  });

  test("the false-alarm button asks once, then stops Vesta (also during an alert)", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("sos").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ALERT");

    await page.getByTestId("false-alarm").click();
    await expect(page.getByText("Oprești alerta?")).toBeVisible();
    await page.getByTestId("false-alarm-confirm").click();
    await expect(page.getByTestId("c-state")).toContainText("Alarmă falsă");
    const cancel = backend.calls(/\/cancel$/);
    expect(cancel).toHaveLength(1);
    expect(cancel[0].body).toMatchObject({ reason: "FALSE_ALARM" });
    expect(backend.session(SESSION_ID)?.alert).toMatchObject({ status: "RESOLVED", resolution_action: "FALSE_ALARM" });
  });

  test("SOS succeeds only on the server's answer, and a failed SOS says so", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    let fail = true;
    backend.responders.push((request) => (request.path.endsWith("/sos") && fail ? "abort" : undefined));
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("sos").click();
    await expect(page.getByText("Alerta nu a plecat.")).toBeVisible();
    fail = false;
    await page.getByTestId("sos").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ALERT");
    const ids = backend.calls(/\/sos$/).map((call) => call.body?.event_id);
    expect(ids[0]).toBe(ids[1]);
  });

  test("microphone refused: explained, the buttons keep working", async ({ page }) => {
    await installDeviceFakes(page, { mic: "deny" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("arm").click();
    await expect(page.getByText("Microfonul nu a fost permis")).toBeVisible();
    await expect(page.getByTestId("sos")).toBeEnabled();
  });

  test("every request the phone sends matches the backend schema (no 422)", async ({ page }) => {
    await installDeviceFakes(page, { gps: "grant" });
    const backend = new MockBackend(page);
    backend.sessions = [checkIn()];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await arm(page);
    await expect.poll(() => backend.calls(/\/delivery$/).length).toBe(1);
    await page.getByTestId("sos").click();
    await expect(page.getByTestId("c-state")).toHaveAttribute("data-state", "ALERT");
    await page.getByTestId("false-alarm").click();
    await page.getByTestId("false-alarm-confirm").click();
    await expect(page.getByTestId("c-state")).toContainText("Alarmă falsă");
    const posts = backend.requests.filter((request) => request.method === "POST" && request.path.startsWith("/api/v1/sessions/"));
    expect(posts.length).toBeGreaterThan(4);
    for (const post of posts) {
      const action = post.path.replace(/^\/api\/v1\/sessions\/[^/]+\//, "").replace(/^alerts\/[^/]+\/resolve$/, "resolve").replace(/^checkins\/[^/]+\/delivery$/, "delivery");
      expect(bodyProblem(action, post.body), `${post.path} ${JSON.stringify(post.body)}`).toBeNull();
    }
  });

  test("coarse GPS asks the person to enable precise location in Android", async ({ page }) => {
    await installDeviceFakes(page, { gps: "coarse" });
    const backend = new MockBackend(page);
    backend.sessions = [makeSession()];
    await open(page, backend);
    await expect(page.getByTestId("phone-location-warning")).toContainText("Locație precisă", { timeout: 10_000 });
    await expect(page.getByTestId("phone-location")).toContainText("± 2000 m");
  });

  test("the dispatcher takes the call: the assistant stops and the dispatcher's voice plays", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    const now = new Date().toISOString();
    backend.sessions = [
      makeSession({
        state: "ALERT",
        alert_reason: "SOS_EXPLICIT",
        alert: { id: "alert-1", incident_type: "EXPLICIT_SOS", status: "ACTIVE", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: null, resolution_action: null, version: 1 },
      }),
    ];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await arm(page);
    const session = backend.session()!;
    session.operator_connected_at = new Date().toISOString();
    backend.publish("call.taken_over", { operator_connected_at: session.operator_connected_at }, session.id);
    await expect(page.getByText("Un operator de la dispecerat vorbește cu tine.")).toBeVisible();
    await expect.poll(() => backend.calls(/\/assistant\/prompt$/).some((call) => call.body?.line === "operator")).toBe(true);
    backend.operatorMessages.set(session.id, [{ id: "op-1", at: new Date().toISOString(), mime_type: "audio/webm", audio_base64: "AAAA" }]);
    await expect(page.getByTestId("chat")).toContainText("Mesaj vocal de la dispecer", { timeout: 8000 });
    await page.waitForTimeout(16_000);
    expect(backend.calls(/\/assistant\/prompt$/).filter((call) => call.body?.line === "nudge")).toHaveLength(0);
  });

  test("after the danger passes, she closes the alert from her phone; it closes at the dispatcher too", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    const now = new Date().toISOString();
    backend.sessions = [
      makeSession({
        state: "ALERT",
        alert_reason: "SOS_EXPLICIT",
        alert: { id: "alert-safe", incident_type: "EXPLICIT_SOS", status: "ACTIVE", created_at: now, updated_at: now, notification_requested_at: now, resolved_at: null, resolution_action: null, version: 1 },
      }),
    ];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("button", { name: /Sunt bine/ })).toHaveCount(0);
    await page.getByTestId("safe").click();
    await page.getByTestId("safe-confirm").click();
    await expect(page.getByText(/Alerta s-a închis la/)).toBeVisible();
    const resolve = backend.calls(/\/alerts\/alert-safe\/resolve$/);
    expect(resolve).toHaveLength(1);
    expect(resolve[0].body).toMatchObject({ action: "USER_CONFIRMED_SAFE" });
    expect(backend.session(SESSION_ID)?.alert).toMatchObject({ status: "RESOLVED" });
  });

  test("while Vesta waits for an answer, only the red emergency button is offered", async ({ page }) => {
    await installDeviceFakes(page);
    const backend = new MockBackend(page);
    backend.sessions = [checkIn()];
    await open(page, backend);
    await expect(page.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("sos")).toBeEnabled();
    await expect(page.getByRole("button", { name: /Sunt bine/ })).toHaveCount(0);
    await expect(page.getByTestId("safe")).toHaveCount(0);
  });
});
