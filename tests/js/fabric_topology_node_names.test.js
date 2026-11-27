"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");
const { nameCases, recorderFabric } = require("./helpers/topology_node_names");
const { Crawler } = require("../../qa/ui-crawl/crawler");
const { loadInventory } = require("../../qa/ui-crawl/inventory");

const decode = (text) => text.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const texts = (body) => [...body.matchAll(/<text\b[^>]*>([^<]*)<\/text>/g)].map((match) => decode(match[1]));
const buttons = (html, kind) => [...html.matchAll(new RegExp(`<g class="${kind}[^>]*aria-label="([^"]*)"[^>]*>([\\s\\S]*?)<\\/g>`, "g"))];

for (const [name, fabric] of Object.entries(nameCases())) {
  for (const pinned of [false, true]) {
    test(`${name}: every node name starts with its rendered lines, ${pinned ? "pinned" : "unpinned"}`, () => {
      const T = H.loadTopology();
      const model = T.buildTopologyModel(fabric);
      assert.ok(model.nodes.length > 0);
      for (const entry of model.nodes) {
        const html = T.renderTopology(model, pinned ? { pin: { kind: "node", id: entry.id } } : {});
        const node = buttons(html, "topo-node ").find((match) => match[0].includes(`data-topo-node="${entry.id}"`));
        assert.ok(node, entry.id);
        const visible = texts(node[2]).join(" ");
        assert.equal(visible, entry.lines.join(" "));
        assert.ok(decode(node[1]).startsWith(visible), `${visible} must lead ${decode(node[1])}`);
        // One hover surface (#1465): no native title; the full label is in the accessible name.
        assert.doesNotMatch(node[0], /<title\b/);
        assert.ok(decode(node[1]).includes(entry.label), "the accessible name retains the full label");
        if (pinned) assert.match(node[0], /is-pinned/);
      }
    });
  }
}

test("recorder path fixture clips a long token but retains its full supplementary label", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(recorderFabric());
  const entry = model.nodes.find((node) => node.kind === "path");
  assert.ok(entry.lines.some((line) => line.includes("…")));
  assert.ok(entry.lines.length >= 3);
  const node = buttons(T.renderTopology(model, {}), "topo-node ").find((match) => match[0].includes('class="topo-node kind-path'));
  assert.ok(decode(node[1]).startsWith(entry.lines.join(" ")));
  assert.ok(decode(node[1]).includes(entry.label));
});

test("bay names include the shown number and overflow path count in reading order", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(recorderFabric());
  for (const bay of model.bays) bay.pathStates = Array.from({ length: 9 }, (_, index) => ({ controller: `synthetic-path-${index}`, state: "active" }));
  const cells = buttons(T.renderTopology(model, {}), "topo-bay ");
  assert.equal(cells.length, 4);
  for (const cell of cells) {
    const visible = texts(cell[2]).join(" ");
    assert.match(visible, /^0[1-4] \+\d+$/);
    assert.ok(decode(cell[1]).startsWith(visible), `${visible} must lead ${decode(cell[1])}`);
    assert.match(decode(cell[1]), /disk present/);
  }
});

test("group names lead with their own rendered summary in collapsed and expanded states", () => {
  const T = H.loadTopology();
  const fabric = H.fourShelves().fabric;
  const collapsed = T.buildTopologyModel(fabric);
  assert.ok(collapsed.groups.some((group) => group.collapsed));
  const expanded = Object.fromEntries(collapsed.groups.map((group) => [group.expandKey, true]));
  for (const model of [collapsed, T.buildTopologyModel(fabric, { expanded })]) {
    const html = T.renderTopology(model, {});
    for (const group of model.groups) {
      const header = [...html.matchAll(/<g class="topo-group[^>]*data-topo-group="([^"]*)" aria-label="([^"]*)"/g)].find((match) => decode(match[1]) === group.id);
      assert.ok(decode(header[2]).startsWith(group.lines.map((line) => line.text).join(" ")));
    }
    for (const toggle of buttons(html, "topo-expand")) assert.equal(decode(toggle[1]), texts(toggle[2]).join(" "));
  }
});

test("tier names start with their rendered label and note with word separators", () => {
  const T = H.loadTopology();
  for (const fabric of Object.values(nameCases())) {
    const html = T.renderTopology(T.buildTopologyModel(fabric), {});
    // One hover surface (#1465): the tier's full wording moved from a native title to its accessible name.
    assert.doesNotMatch(html, /<title\b/);
    const tiers = [...html.matchAll(/<g class="topo-tier" data-topo-tier="[^"]*" aria-label="([^"]*)">([\s\S]*?)<\/g>/g)];
    assert.ok(tiers.length > 0);
    for (const tier of tiers) {
      const visible = texts(tier[2]).join(" ");
      assert.ok(visible.length > 0);
      assert.ok(decode(tier[1]).startsWith(visible), `${visible} must lead ${decode(tier[1])}`);
    }
  }
});

test("identical visible and full node wording is not repeated and markup stays escaped", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(recorderFabric());
  const entry = model.nodes.find((node) => node.kind === "path");
  entry.label = '<Synthetic & "quoted">';
  entry.facts = [];
  entry.phyGroups = [];
  entry.parentUnknown = false;
  entry.lines = [entry.label];
  const node = buttons(T.renderTopology(model, {}), "topo-node ").find((match) => match[0].includes('class="topo-node kind-path'));
  assert.equal(decode(node[1]), entry.label);
  assert.doesNotMatch(node[0], /<Synthetic/);
});

test("no Topology node, bay or group mismatch is masked by known findings", () => {
  for (const state of ["fabric.topology", "fabric.topology.pin", "fabric.topology.drawn"]) {
    for (const selector of [".kind-path", ".topo-node.kind-controller", ".topo-bay", ".topo-group", ".topo-expand", ".topo-tier"]) {
      const finding = { type: "axe-label-content-name-mismatch", selector };
      Crawler.prototype.markKnown.call({ inventory: loadInventory() }, { state, findings: [], widths: [{ findings: [finding] }] });
      assert.equal(finding.known, undefined, `${state}: ${selector}`);
    }
  }
});
