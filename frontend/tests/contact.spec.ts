import { expect, test, type Page } from "playwright/test";
import { CONTACT_TOKEN, makeSession, MockBackend, SESSION_ID } from "./mockBackend";

const alertOf = (status: "ACTIVE" | "RESOLVED") => {
  const now = new Date().toISOString();
  return {
    id: "alert-9",
    incident_type: "MISSED_CHECK_INS",
    status,
    created_at: now,
    updated_at: now,
    notification_requested_at: now,
    resolved_at: status === "RESOLVED" ? now : null,
    resolution_action: status === "RESOLVED" ? "USER_CONFIRMED_SAFE" : null,
    version: 1,
    viewed_at: null,
  };
};

async function openWithCode(page: Page, backend: MockBackend) {
  await backend.install();
  await page.goto("/contact");
  await page.getByLabel("Codul de acces primit").fill(backend.issueCode("contact"));
  await page.getByRole("button", { name: "Deschide alertele" }).click();
}

test.describe("trusted contact", () => {
  test("without a valid code nothing is shown; a phone code is refused", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ state: "ALERT", alert_reason: "SOS_EXPLICIT", alert: alertOf("ACTIVE") })];
    await backend.install();
    await page.goto("/contact");
    await expect(page.getByTestId("contact-state")).toHaveCount(0);
    await page.getByLabel("Codul de acces primit").fill("WRONG234");
    await page.getByRole("button", { name: "Deschide alertele" }).click();
    await expect(page.getByTestId("contact-problem")).toContainText("nu este valid");
    await page.getByLabel("Codul de acces primit").fill(backend.issueCode("companion"));
    await page.getByRole("button", { name: "Deschide alertele" }).click();
    await expect(page.getByTestId("contact-problem")).toContainText("telefonul persoanei");
    expect(backend.calls("/api/v1/contact/status")).toHaveLength(0);
  });

  test("active alert with location, marked as viewed once; resolved alert keeps no location", async ({ page }) => {
    const backend = new MockBackend(page);
    const session = makeSession({ state: "ALERT", alert_reason: "MISSED_CHECK_INS", alert: alertOf("ACTIVE") });
    backend.sessions = [session];
    backend.locations.set(SESSION_ID, { latitude: 45.761234, longitude: 21.229876, captured_at: new Date().toISOString(), accuracy_meters: 15 });
    await openWithCode(page, backend);

    await expect(page.getByTestId("contact-state")).toHaveText(/Alertă activă/);
    await expect(page.getByTestId("alert-row")).toContainText("Verificări fără răspuns");
    await expect(page.getByTestId("contact-coordinates")).toHaveText("45.76123, 21.22988");
    await expect(page.getByTestId("contact-location-status")).toHaveText("CURRENT");
    await expect.poll(() => backend.calls(/\/contact\/alerts\/alert-9\/viewed$/).length).toBe(1);
    expect(backend.calls("/api/v1/contact/status").every((call) => call.auth === `Bearer ${CONTACT_TOKEN}`)).toBe(true);
    expect(page.url()).not.toContain(CONTACT_TOKEN);

    session.state = "CANCELLED";
    session.alert = { ...alertOf("RESOLVED"), viewed_at: session.alert!.viewed_at };
    await expect(page.getByTestId("contact-state")).toHaveText(/Nicio alertă activă/, { timeout: 10_000 });
    await expect(page.getByTestId("alert-row")).toContainText("RESOLVED");
    await expect(page.getByTestId("contact-coordinates")).toHaveCount(0);
    expect(backend.calls(/\/viewed$/)).toHaveLength(1);
  });

  test("an alert without an authorized location is still shown", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ state: "ALERT", alert_reason: "SOS_EXPLICIT", alert: alertOf("ACTIVE") })];
    await openWithCode(page, backend);
    await expect(page.getByTestId("contact-state")).toHaveText(/Alertă activă/);
    await expect(page.getByTestId("contact-location-status")).toHaveText("UNAVAILABLE");
    await expect(page.getByText("Alerta este valabilă și fără locație.")).toBeVisible();
  });
});
