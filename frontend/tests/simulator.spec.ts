import { expect, test } from "playwright/test";
import { MockBackend } from "./mockBackend";

/* /buton: stands in for the joystick while the Raspberry Pi has no battery. */

test.describe("button simulator", () => {
  test("a short press does nothing; holding 5 s starts the session like the joystick", async ({ page }) => {
    const backend = new MockBackend(page);
    await backend.install();
    await page.goto("/buton");
    const button = page.getByTestId("sim-button");
    await expect(page.getByTestId("sim-state")).toHaveAttribute("data-state", "IDLE");

    const box = (await button.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.waitForTimeout(1500);
    await page.mouse.up();
    await expect(page.getByTestId("sim-result")).toContainText("prea devreme");
    expect(backend.calls("/api/v1/simulator/press")).toHaveLength(0);

    await page.mouse.down();
    await page.waitForTimeout(5400);
    await page.mouse.up();
    await expect(page.getByTestId("sim-result")).toContainText("Vesta a pornit");
    expect(backend.calls("/api/v1/simulator/press")).toHaveLength(1);
    await expect(page.getByTestId("sim-state")).toHaveAttribute("data-state", "ACTIVE");

    // A second hold while the session runs does not start another one.
    await page.keyboard.press("Tab");
    await button.focus();
    await page.keyboard.down("Space");
    await page.waitForTimeout(5400);
    await page.keyboard.up("Space");
    await expect(page.getByTestId("sim-result")).toContainText("Există deja o sesiune");
    expect(backend.sessions).toHaveLength(1);
  });
});
