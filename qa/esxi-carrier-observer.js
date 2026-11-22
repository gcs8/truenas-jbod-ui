const { expect } = require("@playwright/test");

// Shared by the restored-live observer and synthetic fixture coverage.
async function observeAocCarrier(page) {
  await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:supermicro-aoc-slg4-2h8m2");
  await expect(page.locator(".nvme-carrier-board-image")).toHaveAttribute("src", /aoc-slg4-2h8m2\.jpg$/);

  const enclosureOptions = await page.locator("#enclosure-select option").evaluateAll((options) =>
    options
      .map((option) => option.value)
      .filter((value) => typeof value === "string" && value.length > 0)
  );
  expect(enclosureOptions).not.toContain("view:aoc-slg4-2h8m2");

  const tiles = page.locator("#slot-grid .slot-tile");
  await expect(tiles).toHaveCount(2);

  // Read only this selected system/enclosure. A warning or tile text alone
  // cannot select a more permissive branch or turn a profile hint into a disk.
  const systemId = await page.locator("#system-select").inputValue();
  const enclosureId = (await page.locator("#enclosure-select").inputValue()).slice("enclosure:".length);
  const observed = await page.evaluate(async ({ systemId, enclosureId }) => {
    const query = new URLSearchParams({ system_id: systemId, enclosure_id: enclosureId, force: "false" });
    const response = await fetch(`/api/inventory?${query}`);
    return { ok: response.ok, snapshot: await response.json() };
  }, { systemId, enclosureId });
  expect(observed.ok).toBe(true);
  const snapshot = observed.snapshot;
  expect(snapshot.selected_system_id).toBe(systemId);
  expect(snapshot.selected_enclosure_id).toBe(enclosureId);
  const expectedOrder = snapshot.layout_rows.flat().filter(Number.isInteger);
  expect(expectedOrder).toHaveLength(2);
  expect(new Set(expectedOrder).size).toBe(2);
  expect(await tiles.evaluateAll(nodes => nodes.map(node => node.dataset.slot))).toEqual(expectedOrder.map(String));
  const slots = expectedOrder.map(slot => snapshot.slots.find(item => item.slot === slot));
  expect(slots.every(Boolean)).toBe(true);
  const unread = slots.every(slot => slot.presence_known === false);
  if (unread) {
    const missingSource = /StorCLI is not (installed|available) on this ESXi host/;
    expect(snapshot.warnings.some(warning => missingSource.test(warning))).toBe(true);
    await expect(page.locator("#warning-list")).toContainText(missingSource);
  }
  for (const [index, slot] of slots.entries()) {
    const tile = tiles.nth(index);
    await expect(tile.locator(".storage-view-runtime-card-slot")).toHaveText(String(slot.slot).padStart(2, "0"));
    await expect(tile).toHaveAttribute("data-slot-size", "2280");
    if (unread) {
      expect(slot.present).toBe(false);
      await expect(tile).toHaveClass(/state-unknown/);
      await expect(tile.locator(".storage-view-runtime-card-device")).toHaveText("Unknown");
      await expect(tile).toContainText("Whether this slot holds a disk is not known.");
      await expect(tile).not.toContainText("Matched from live inventory.");
      await expect(tile).not.toContainText(`13:${slot.slot}`);
    } else {
      // Known occupancy is the API default and is omitted by its serializer.
      expect(slot.presence_known ?? true).toBe(true);
      expect(slot.present).toBe(true);
      expect(slot.device_name).toBe(`13:${slot.slot}`);
      expect(slot.model).toBe("Samsung SSD 970 EVO 2TB");
      await expect(tile).toContainText(`13:${slot.slot}`);
    }
  }

  await tiles.first().click();
  await expect(page.locator("#detail-content")).toBeVisible();
  // The detail's device coordinate remains a hint when no occupant was read.
  await expect(page.locator("#detail-content")).toContainText(unread ? `13:${slots[0].slot}` : "Samsung SSD 970 EVO 2TB");
  await expect(page.locator("#detail-led-controls")).toBeHidden();
  await expect(page.locator("#detail-smart-note")).toBeVisible();
  await expect(page.locator(".nvme-carrier-edge-note")).toContainText("M2-1 lower slot");
}

module.exports = { observeAocCarrier };
