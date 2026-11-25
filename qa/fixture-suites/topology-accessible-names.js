"use strict";

// Registered beside the shared full-page accessibility fixture so CI exercises
// the shipping template, handlers and assets, without another browser leg.
const { test, expect } = require("@playwright/test");
const H = require("../../tests/js/helpers/fabric_topology");
const fs = require("node:fs");
const path = require("node:path");

async function nativeName(session, selector) {
  let name;
  // Resize can replace a narrow SVG between CDP commands. Re-resolve only
  // detached-node failures; an absent name still fails the bounded probe.
  await expect.poll(async () => {
    const { root } = await session.send("DOM.getDocument");
    const { nodeId } = await session.send("DOM.querySelector", { nodeId: root.nodeId, selector });
    if (!nodeId) return false;
    try {
      const { nodes } = await session.send("Accessibility.getPartialAXTree", { nodeId, fetchRelatives: false });
      name = nodes.find((node) => node.role?.value === "button")?.name?.value;
      return typeof name === "string";
    } catch (error) {
      if (!error.message.includes("Could not find node with given id")) throw error;
      return false;
    }
  }, { timeout: 5000 }).toBe(true);
  return name;
}

module.exports = function register({ fixture, routeSynthetic, capture, axeViolations, viewports, origin }) {
  for (const viewport of viewports) {
    test(`Topology accessible names preserve visible labels at ${viewport.width} px`, async ({ page }) => {
      test.setTimeout(90_000);
      await page.setViewportSize(viewport);
      const fabric = structuredClone(H.producerCases().core_cascade.fabric);
      const html = fixture.fabric.html.replace(/window\.SAS_FABRIC_BOOTSTRAP = (.+);/, (_match, json) => {
        const bootstrap = JSON.parse(json);
        fabric.system_id = bootstrap.systemId;
        fabric.selected_enclosure_id = bootstrap.enclosureId;
        bootstrap.fabric = fabric;
        return `window.SAS_FABRIC_BOOTSTRAP = ${JSON.stringify(bootstrap)};`;
      });
      const errors = await routeSynthetic(page, html, { fabric });
      const assets = ["sas_fabric_topology_layout.js", "sas_fabric_topology_ui.js", "sas_fabric_topology_evidence.js", "sas_fabric_topology.js", "sas_fabric_topology.css"].map((name) => ({
        name, response: page.waitForResponse((response) => new URL(response.url()).pathname === `/static/${name}`),
      }));
      await page.goto(`${origin}/sas-fabric`);
      for (const asset of assets) {
        const response = await asset.response;
        expect(response.status()).toBe(200);
        expect(await response.text()).toBe(fs.readFileSync(path.join(H.ROOT, "app/static", asset.name), "utf8"));
      }
      await page.locator("#fabric-mode-topology").click();
      const panel = page.locator("#fabric-map-panel");
      await expect(panel.locator(".topo-link-label.is-bundle").first()).toBeVisible();
      expect(await page.evaluate(() => innerWidth)).toBe(viewport.width);
      const session = await page.context().newCDPSession(page);
      const observations = [];
      for (const state of ["unpinned", "bundle", "lane"]) {
        if (state !== "unpinned") {
          const selector = state === "bundle" ? ".is-bundle" : ".is-lanes-reported";
          const button = panel.locator(`.topo-link-label${selector}`).first();
          // Keyboard pinning also reaches labels outside the initial scroll slice.
          await button.focus();
          await button.press("Enter");
          await expect(panel.locator(`.topo-link-label${selector}.is-pinned`)).toHaveCount(1);
          // Approved design: a bundle into a physical enclosure is an enclosure path; a cable has its own inspector.
          await expect(page.locator("#fabric-inspector-title")).toHaveText(state === "bundle" ? "Selected enclosure path" : "Selected cable");
          await expect(page.locator("#fabric-inspector-body")).toContainText(state === "bundle" ? "Bays in the enclosure" : "PHYs");
        }
        const scrollViolations = state === "unpinned" ? [] : await axeViolations(page, ["label-content-name-mismatch"]);
        // Restore the same page position after keyboard focus scrolls a badge.
        // The diagram is put at its left edge: its first view centres the host,
        // and axe drops a text line scrolled wholly past the viewport's left
        // edge, then reads the remaining lines of that box as out of order.
        await page.evaluate(() => {
          window.scrollTo(0, 0);
          document.querySelector("#fabric-map-panel .fabric-topology-scroll").scrollLeft = 0;
        });
        await page.mouse.move(0, 0);
        await page.locator("#fabric-mode-topology").focus();
        // Focus and hover repaint on animation frames; inspect the settled DOM.
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await capture(page, `topology-${state}-${viewport.width}`);
        const names = [];
        for (const kind of ["is-bundle", "is-lanes-reported"]) {
          const selector = `#fabric-map-panel .topo-link-label.${kind}`;
          const visible = await page.locator(selector).first().locator("text").allTextContents();
          names.push({ kind, visible: visible.join(" "), name: await nativeName(session, selector) });
        }
        observations.push({ state, names, scrollViolations, violations: await axeViolations(page, ["label-content-name-mismatch"]) });
      }
      // Fit draws the whole drawing inside its box: every name is checked at once.
      await panel.locator("[data-topology-fit]").click();
      await expect(panel.locator(".fabric-topology-svg.is-fitted")).toHaveCount(1);
      expect(await axeViolations(page, ["label-content-name-mismatch"]), "fitted").toEqual([]);
      // Name-from-content diagnostic: keep positioned text and its separators,
      // remove only the explicit name on a disposable host-node copy. One hover
      // surface (#1465): the drawing carries no native title at all.
      await expect(panel.locator("title, [title]")).toHaveCount(0);
      const lines = await panel.locator(".topo-node.kind-host text").allTextContents();
      expect(lines.length).toBeGreaterThan(1);
      await panel.locator(".topo-node.kind-host").evaluate((node) => {
        const copy = node.cloneNode(true);
        copy.id = "topology-content-name-control";
        copy.removeAttribute("aria-label");
        node.parentNode.append(copy);
      });
      const contentName = await nativeName(session, "#topology-content-name-control");
      await page.locator("#topology-content-name-control").evaluate((node) => node.remove());
      await session.detach();
      for (const { state, names, scrollViolations, violations } of observations) {
        for (const { kind, visible, name } of names) {
          // Approved design: a bundle into a physical enclosure is named as a path.
          expect.soft(name?.startsWith(`${visible}, ${kind === "is-bundle" ? "Path" : "Link"} `), `${state} ${kind}: ${name}`).toBe(true);
        }
        expect.soft(violations, state).toEqual([]);
        expect.soft(scrollViolations, `${state} after keyboard scroll`).toEqual([]);
      }
      expect(contentName).toBe(lines.join(" "));
      expect(errors).toEqual([]);
    });
  }
};
