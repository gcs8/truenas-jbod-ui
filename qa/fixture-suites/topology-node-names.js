"use strict";

const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");
const H = require("../../tests/js/helpers/fabric_topology");
const { recorderFabric, overflowFabric } = require("../../tests/js/helpers/topology_node_names");

async function nativeNames(page, session) {
  const selector = "#fabric-map-panel :is(.topo-node, .topo-link-label, .topo-bay, .topo-expand)[role=button]";
  const labels = await page.locator(selector).evaluateAll((nodes) => nodes.map((node) => {
    const hook = ["data-topo-node", "data-topo-link", "data-topo-bay", "data-fabric-expand-slots"].find((name) => node.hasAttribute(name));
    return { selector: `#fabric-map-panel .${node.classList[0]}[${hook}=${JSON.stringify(node.getAttribute(hook))}]`, kind: node.getAttribute("class"), visible: [...node.querySelectorAll("text")].map((text) => text.textContent).join(" ") };
  }));
  expect(labels.length).toBeGreaterThan(0);
  for (const label of labels) {
    // A narrow drawing can resize between the two CDP commands. Re-resolve
    // the stable shipping identity, never reuse a detached DOM node id.
    await expect.poll(async () => {
      const { root } = await session.send("DOM.getDocument");
      const { nodeId } = await session.send("DOM.querySelector", { nodeId: root.nodeId, selector: label.selector });
      if (!nodeId) return false;
      try {
        const { nodes } = await session.send("Accessibility.getPartialAXTree", { nodeId, fetchRelatives: false });
        label.name = nodes.find((node) => node.role?.value === "button")?.name?.value;
        return typeof label.name === "string";
      } catch (error) {
        if (!error.message.includes("Could not find node with given id")) throw error;
        return false;
      }
    }, { timeout: 5000 }).toBe(true);
  }
  return labels;
}

module.exports = function register({ fixture, routeSynthetic, capture, axeViolations, viewports, origin }) {
  for (const [shape, build] of [["sas", () => structuredClone(H.producerCases().core_cascade.fabric)], ["recorder", recorderFabric], ["overflow", overflowFabric]]) {
    for (const viewport of viewports) {
      test(`Topology node names preserve ${shape} visible text at ${viewport.width} px`, async ({ page }) => {
        test.setTimeout(90_000);
        await page.setViewportSize(viewport);
        const fabric = build();
        const html = fixture.fabric.html.replace(/window\.SAS_FABRIC_BOOTSTRAP = (.+);/, (_match, json) => {
          const bootstrap = JSON.parse(json);
          fabric.system_id = bootstrap.systemId;
          fabric.selected_enclosure_id = bootstrap.enclosureId;
          bootstrap.fabric = fabric;
          return `window.SAS_FABRIC_BOOTSTRAP = ${JSON.stringify(bootstrap)};`;
        });
        const errors = await routeSynthetic(page, html, { fabric });
        const assetReady = page.waitForResponse((response) => new URL(response.url()).pathname === "/static/sas_fabric_topology.js");
        await page.goto(`${origin}/sas-fabric`);
        const asset = await assetReady;
        expect(asset.status()).toBe(200);
        expect(await asset.text()).toBe(fs.readFileSync(path.join(H.ROOT, "app/static/sas_fabric_topology.js"), "utf8"));
        await page.locator("#fabric-mode-topology").click();
        const panel = page.locator("#fabric-map-panel");
        await expect(panel.locator(".topo-node.kind-host")).toBeVisible();
        expect(await page.evaluate(() => innerWidth)).toBe(viewport.width);
        if (shape !== "sas") {
          await expect(panel.locator(".topo-node.kind-path text").first()).toContainText("…");
          await expect(panel.locator(".topo-bay")).toHaveCount(4);
        }
        if (shape === "overflow") {
          await expect(panel.locator(".topo-path-more").first()).toBeVisible();
          const bay = panel.locator(".topo-bay").filter({ has: page.locator(".topo-path-more") }).first();
          const count = await bay.locator(".topo-path-more").textContent();
          expect(await bay.getAttribute("aria-label")).toContain(count);
        }
        await expect(panel.locator("title, [title]")).toHaveCount(0);
        const hba = panel.locator(".topo-node.kind-controller").first();
        await hba.hover();
        await expect(panel.locator("[data-topology-tip]")).toBeVisible();
        await page.mouse.move(0, 0);
        const session = await page.context().newCDPSession(page);
        const observations = [];
        for (const state of ["unpinned", "pinned"]) {
          if (state === "pinned") {
            const node = panel.locator(shape !== "sas" ? ".topo-node.kind-path" : ".topo-node.kind-expander").first();
            await node.focus();
            await node.press("Enter");
            await expect(panel.locator(".topo-node.is-pinned")).toHaveCount(1);
            await expect(page.locator("#fabric-inspector-body")).not.toBeEmpty();
          }
          const scrolled = await axeViolations(page, ["label-content-name-mismatch"]);
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.mouse.move(0, 0);
          await page.locator("#fabric-mode-topology").focus();
          await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const captureName = `node-${shape}-${state}-${viewport.width}`;
          await capture(page, captureName);
          if (process.env.UI_A11Y_SCREENSHOT_DIR) {
            const geometry = await page.locator(".topo-node, .topo-group, .fabric-inspector-panel").evaluateAll((nodes) => nodes.map((node) => {
              const { x, y, width, height } = node.getBoundingClientRect();
              return { kind: node.className.baseVal || node.id, x, y: y + scrollY, width, height };
            }));
            fs.writeFileSync(path.join(process.env.UI_A11Y_SCREENSHOT_DIR, `${captureName}.json`), JSON.stringify({ viewport, geometry }, null, 2));
          }
          observations.push({ state, names: await nativeNames(page, session), scrolled, violations: await axeViolations(page, ["label-content-name-mismatch"]) });
        }
        await session.detach();
        for (const observation of observations) {
          for (const label of observation.names) {
            expect.soft(label.name?.startsWith(label.visible), `${observation.state} ${label.kind}: ${label.visible} must lead ${label.name}`).toBe(true);
          }
          expect.soft(observation.scrolled, `${observation.state} after keyboard scroll`).toEqual([]);
          expect.soft(observation.violations, observation.state).toEqual([]);
        }
        expect(errors).toEqual([]);
      });
    }
  }
};
