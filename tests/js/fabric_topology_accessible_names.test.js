"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");
const { Crawler } = require("../../qa/ui-crawl/crawler");
const { loadInventory } = require("../../qa/ui-crawl/inventory");

const decode = (text) => text.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const texts = (body) => [...body.matchAll(/<text\b[^>]*>([^<]*)<\/text>/g)].map((match) => decode(match[1]));

for (const [name, fabric] of Object.entries(H.producerCases()).map(([name, entry]) => [name, entry.fabric])) {
  for (const pinned of [false, true]) {
    test(`${name}: Topology link names start with their visible words, ${pinned ? "pinned" : "unpinned"}`, () => {
      const T = H.loadTopology();
      const model = T.buildTopologyModel(fabric);
      const links = model.links.filter((link) => link.bundle || link.lanesState === "reported");
      assert.ok(links.length > 0);
      for (const link of links) {
        const html = T.renderTopology(model, pinned ? { pin: { kind: "link", id: link.id } } : {});
        const labels = [...html.matchAll(/<g class="topo-link-label[^>]*data-topo-link="([^"]*)" aria-label="([^"]*)">([\s\S]*?)<\/g>/g)];
        const label = labels.find((match) => decode(match[1]) === link.id);
        assert.ok(label, link.id);
        const visible = texts(label[3]).join(" ");
        assert.ok(visible.length > 0);
        assert.ok(decode(label[2]).startsWith(`${visible}, Link `), `${visible} must lead ${decode(label[2])}`);
        if (pinned) assert.match(label[0], /is-pinned/);
      }
    });
  }
}

test("multiline SVG node and link text keeps word separators without changing line contents", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(H.producerCases().core_cascade.fabric);
  const html = T.renderTopology(model, {});
  let multiline = 0;
  for (const match of html.matchAll(/<g class="topo-(?:node|link-label)[^>]*>([\s\S]*?)<\/g>/g)) {
    const lines = texts(match[1]);
    if (lines.length < 2) continue;
    multiline += 1;
    const content = decode(match[1].replace(/<title>[\s\S]*?<\/title>/g, "").replace(/<[^>]*>/g, "")).replace(/\s+/g, " ").trim();
    assert.equal(content, lines.join(" "));
  }
  assert.ok(multiline > 0);
});

function known(state, selector, type = "axe-label-content-name-mismatch") {
  const finding = { type, selector };
  Crawler.prototype.markKnown.call({ inventory: loadInventory() }, { state, findings: [], widths: [{ findings: [finding] }] });
  return finding.known;
}

test("Topology label mismatches are not covered by the original bay-tile and Rename issue", () => {
  for (const state of ["fabric.topology", "fabric.topology.pin", "fabric.topology.drawn"]) {
    for (const selector of ['.topo-link-label.is-bundle[role="button"]', '.is-lanes-reported.topo-link-label[role="button"]', '.topo-node.kind-host[role="button"]']) {
      assert.equal(known(state, selector), undefined, `${state}: ${selector}`);
    }
  }
  assert.equal(known("main.default", "#enclosure-alias-edit-button"), 1110);
  assert.equal(known("main.heatmap", 'button[data-slot="0"]'), 1110);
  assert.equal(known("main.heatmap.select", ".peer-highlight"), 1110);
  assert.equal(known("main.default", ".new-component"), undefined);
  assert.equal(known("fabric.topology", 'button[data-slot="0"]'), undefined);
  assert.equal(known("main.heatmap", 'button[data-slot="0"]', "axe-button-name"), undefined);
});
