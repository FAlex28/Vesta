import { expect, test } from "playwright/test";
import { CONTACT_TOKEN, makeSession, MockBackend, SESSION_ID } from "./mockBackend";

/* The trusted contact (v5): no invitation code, dashboard-like screen, live location in an alert. */

const alertOf = (status: "ACTIVE" | "RESOLVED", resolution: string | null = null) => {
  const now = new Date().toISOString();
  return {
    id: "alert-9",
    incident_type: "MISSED_CHECK_INS",
    status,
    created_at: now,
    updated_at: now,
    notification_requested_at: now,
    resolved_at: status === "RESOLVED" ? now : null,
    resolution_action: status === "RESOLVED" ? resolution ?? "USER_CONFIRMED_SAFE" : null,
    version: 1,
    viewed_at: null,
  };
};

test.describe("trusted contact", () => {
  test("opens without any code and shows the calm state", async ({ page }) => {
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/contact");
    await expect(page.getByTestId("contact-state")).toContainText("Nicio alertă activă");
    expect(backend.calls("/api/v1/access/open")[0].body).toEqual({ scope: "contact" });
    expect(backend.calls("/api/v1/contact/status").every((call) => call.auth === `Bearer ${CONTACT_TOKEN}`)).toBe(true);
    await expect(page.getByLabel("Codul din SMS")).toHaveCount(0);
    await expect(page.getByTestId("call-112")).toHaveCount(0);
  });

  test("active alert: live location from the device GPS, 112, marked as viewed once", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.ownerName = "Ana";
    backend.sessions = [makeSession({ state: "ALERT", alert_reason: "SOS_EXPLICIT", alert: alertOf("ACTIVE") })];
    backend.locations.set(SESSION_ID, { latitude: 45.74891, longitude: 21.20872, captured_at: new Date().toISOString(), accuracy_meters: 5, source: "device_gps" });
    await backend.install();
    await page.goto("/contact");
    await expect(page.getByTestId("contact-state")).toContainText("Alertă activă");
    await expect(page.getByText("Ana", { exact: true })).toBeVisible();
    await expect(page.getByTestId("contact-coordinates")).toHaveText("45.74891, 21.20872");
    await expect(page.getByTestId("contact-location-status")).toHaveText("CURRENT");
    await expect(page.getByTestId("call-112")).toHaveAttribute("href", "tel:112");
    await expect.poll(() => backend.calls(/\/viewed$/).length).toBe(1);

    // The position moves: the page follows it.
    backend.locations.set(SESSION_ID, { latitude: 45.75, longitude: 21.21, captured_at: new Date().toISOString(), accuracy_meters: 5, source: "device_gps" });
    await expect(page.getByTestId("contact-coordinates")).toHaveText("45.75000, 21.21000", { timeout: 6000 });

    // Resolved: history only, no location.
    backend.sessions[0].alert = alertOf("RESOLVED");
    backend.sessions[0].state = "CANCELLED";
    await expect(page.getByTestId("contact-state")).toContainText("Nicio alertă activă", { timeout: 8000 });
    await expect(page.getByTestId("contact-coordinates")).toHaveCount(0);
  });

  test("an alert without a location is still shown", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ state: "ALERT", alert_reason: "MISSED_CHECK_INS", alert: alertOf("ACTIVE") })];
    await backend.install();
    await page.goto("/contact");
    await expect(page.getByTestId("contact-state")).toContainText("Alertă activă");
    await expect(page.getByTestId("contact-location-status")).toHaveText("UNAVAILABLE");
    await expect(page.getByText("Telefonul ei nu a trimis încă locația")).toBeVisible();
  });

  test("marks a location with low accuracy as imprecise", async ({ page }) => {
    const backend = new MockBackend(page);
    backend.sessions = [makeSession({ state: "ALERT", alert_reason: "SOS_EXPLICIT", alert: alertOf("ACTIVE") })];
    backend.locations.set(SESSION_ID, { latitude: 45.74891, longitude: 21.20872, captured_at: new Date().toISOString(), accuracy_meters: 2000, source: "device_gps" });
    await backend.install();
    await page.goto("/contact");
    await expect(page.getByTestId("contact-location-imprecise")).toContainText("Poziție imprecisă (± 2000 m).");
  });
});
