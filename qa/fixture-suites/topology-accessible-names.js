"use strict";

// Registered beside the shared full-page accessibility fixture so CI exercises
// the shipping template, handlers and assets, without another browser leg.
const { test, expect } = require("@playwright/test");
const H = require("../../tests/js/helpers/fabric_topology");
const fs = require("node:fs");
const path = require("node:path");

async function nativeName(session, selector) {
  const { root } = await session.send("DOM.getDocument");
  const { nodeId } = await session.send("DOM.querySelector", { nodeId: root.nodeId, selector });
  expect(nodeId).not.toBe(0);
  const { nodes } = await session.send("Accessibility.getPartialAXTree", { nodeId, fetchRelatives: false });
  return nodes.find((node) => node.role?.value === "button")?.name?.value;
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
      const assets = ["sas_fabric_topology.js", "sas_fabric_topology.css"].map((name) => ({
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
          await expect(page.locator("#fabric-inspector-title")).toHaveText(state === "bundle" ? "Selected bay bundle" : "Selected link");
          await expect(page.locator("#fabric-inspector-body")).toContainText(state === "bundle" ? "Bays it carries" : "PHYs");
        }
        const scrollViolations = state === "unpinned" ? [] : await axeViolations(page, ["label-content-name-mismatch"]);
        // Restore the same page position after keyboard focus scrolls a badge.
        // The diagram keeps its own horizontal scroll position.
        await page.evaluate(() => window.scrollTo(0, 0));
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
      // Name-from-content diagnostic: keep positioned text and its separators,
      // remove only the explicit name and title on a disposable host-node copy.
      const lines = await panel.locator(".topo-node.kind-host text").allTextContents();
      expect(lines.length).toBeGreaterThan(1);
      await panel.locator(".topo-node.kind-host").evaluate((node) => {
        const copy = node.cloneNode(true);
        copy.id = "topology-content-name-control";
        copy.removeAttribute("aria-label");
        copy.querySelector("title").remove();
        node.parentNode.append(copy);
      });
      const contentName = await nativeName(session, "#topology-content-name-control");
      await page.locator("#topology-content-name-control").evaluate((node) => node.remove());
      await session.detach();
      for (const { state, names, scrollViolations, violations } of observations) {
        for (const { kind, visible, name } of names) {
          expect.soft(name?.startsWith(`${visible}, Link `), `${state} ${kind}: ${name}`).toBe(true);
        }
        expect.soft(violations, state).toEqual([]);
        expect.soft(scrollViolations, `${state} after keyboard scroll`).toEqual([]);
      }
      expect(contentName).toBe(lines.join(" "));
      expect(errors).toEqual([]);
    });
  }
};
