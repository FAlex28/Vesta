import { expect, test } from "playwright/test";
import { installDeviceFakes, makeSession, MockBackend, SESSION_ID } from "./mockBackend";

/*
 * The dispatcher talks live with her (WebRTC between the dashboard and her phone, both served
 * by the same mock). Real host candidates instead of mDNS names, so the two pages find each other.
 */
test.use({ launchOptions: { args: ["--disable-features=WebRtcHideLocalIpsWithMdns"] } });

test.describe("live call", () => {
  test("the dispatcher takes the call and talks with her; her phone answers by itself", async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "two pages in one test: run once");
    const dashContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const phoneContext = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
    const dash = await dashContext.newPage();
    const phone = await phoneContext.newPage();
    await installDeviceFakes(dash);
    await installDeviceFakes(phone);
    const backend = new MockBackend(dash);
    backend.sessions = [makeSession()];
    await backend.install();
    await backend.install(phone);

    await phone.goto("/companion");
    await expect(phone.getByText("Sesiunea ta")).toBeVisible({ timeout: 10_000 });
    await dash.goto("/");
    await dash.getByTestId("take-call").click();

    await expect(dash.getByTestId("call-state")).toHaveAttribute("data-state", "live", { timeout: 15_000 });
    await expect(phone.getByTestId("phone-call")).toHaveAttribute("data-state", "live", { timeout: 10_000 });
    await expect(phone.getByTestId("phone-call")).toContainText("În apel cu dispecerul");
    await expect(dash.getByTestId("ptt")).toHaveCount(0);
    expect(backend.calls(/\/call\/offer$/)).toHaveLength(1);
    expect(backend.calls(/\/call\/answer$/).filter((call) => call.method === "POST")).toHaveLength(1);
    // The assistant stays quiet: the server knows the call was taken over.
    expect(backend.session()!.operator_connected_at).not.toBeNull();

    await dash.getByTestId("hangup-call").click();
    await expect(phone.getByTestId("phone-call")).toHaveCount(0, { timeout: 6000 });
    await expect(dash.getByTestId("call-state")).toHaveAttribute("data-state", "ended");
    await expect(dash.getByTestId("redial-call")).toBeVisible();
    await expect(dash.getByTestId("ptt-button")).toBeVisible();
    expect(backend.liveCalls.has(SESSION_ID)).toBe(false);

    await dashContext.close();
    await phoneContext.close();
  });
});
