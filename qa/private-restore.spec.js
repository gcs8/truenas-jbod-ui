const { test, expect } = require("@playwright/test");

const browserErrors = new WeakMap();
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:8080";

test.beforeEach(async ({ page }) => {
  const errors = [];
  browserErrors.set(page, errors);
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(`console:${message.text()}`);
    }
  });
  page.on("pageerror", (error) => {
    errors.push(`page:${error.name}`);
  });
});

test.afterEach(async ({ page }) => {
  expect(browserErrors.get(page) || []).toEqual([]);
});

test.describe("private restored QA stack", () => {
  test("restored read UI renders under the real authentication boundary", async ({ page }) => {
    const response = await page.goto("/", { waitUntil: "domcontentloaded" });
    expect(response).not.toBeNull();
    expect(response.status()).toBe(200);
    await expect(page.locator("#system-select")).toBeVisible();
    await expect(page.locator("#status-text")).toHaveAttribute("role", "status");
    await expect(page.locator("#enclosure-alias-edit-button")).toHaveCount(1);
  });

  test("label pencil mutation saves and clears through the served UI origin", async ({ request }) => {
    const objectId = `qa-browser-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const savePayload = {
      object_kind: "system",
      object_id: objectId,
      label: "QA restore browser label",
    };

    const save = await request.post("/api/sas-fabric/aliases", {
      data: savePayload,
      headers: { Origin: baseURL },
    });
    expect(save.status()).toBe(200);
    const saved = await save.json();
    expect(saved.ok).toBe(true);
    expect(saved.alias.label).toBe(savePayload.label);

    const clear = await request.post("/api/sas-fabric/aliases", {
      data: { ...savePayload, label: null },
      headers: { Origin: baseURL },
    });
    expect(clear.status()).toBe(200);
    const cleared = await clear.json();
    expect(cleared.ok).toBe(true);
    expect(cleared.cleared).toBe(true);
  });
});
