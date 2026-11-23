// The shipping Topology renderer on thirteen invented fabric shapes.
//
//   node --test tests/js/fabric_topology_generality.test.js
//
// The fixtures are synthetic Linux SAS transport surveys run through the SCALE
// real builder (tests/topology_generality_fixtures.py writes the fixtures). Each must render without an error
// and without inventing anything: every PHY number, SAS address and name on
// the page has to be in the fixture, and nothing of a captured real system
// may appear.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const H = require("./helpers/fabric_topology");
const T = H.loadTopology();
const { fixtures } = JSON.parse(fs.readFileSync(path.join(H.ROOT, "tests/fixtures/sas_fabric/topology_generality.json"), "utf8"));
const byId = Object.fromEntries(fixtures.map((fixture) => [fixture.id, fixture]));
const build = (fixture, width = 1150, change = (fabric) => fabric) => {
  const fabric = change(JSON.parse(JSON.stringify(fixture.fabric)));
  return T.buildTopologyModel(fabric, { ...JSON.parse(JSON.stringify(fixture.options)), width, height: 900, enclosureHref: (id) => `view?enclosure=${id}` });
};
// The renderer runs in its own context, so its arrays are another realm's: compare as JSON.
const same = (actual, expected, message) => assert.equal(JSON.stringify(actual), JSON.stringify(expected), message);
const decode = (text) => text.replace(/<[^>]*>/g, " ").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

// Everything the page can say about a model: the drawing's text and names,
// the text around it, every check line, hover card, inspector table and trace line.
function everyText(model) {
  const html = T.renderTopology(model, {});
  const texts = [decode(html)];
  for (const match of html.matchAll(/aria-label="([^"]*)"/g)) texts.push(decode(match[1]));
  for (const check of model.checks) texts.push(check.title, ...check.lines, ...check.notes);
  const refs = [...model.nodes.map((entry) => ({ kind: "node", id: entry.id })), ...model.bays.map((bay) => ({ kind: "bay", id: bay.bayId })),
    ...[...model.portByKey.keys()].map((id) => ({ kind: "port", id })), ...model.links.map((link) => ({ kind: "link", id: link.id }))];
  for (const ref of refs) {
    const card = T.cardFor(model, ref);
    if (card) texts.push(card.title, ...card.rows.flatMap((row) => [row.label, row.value]));
    const detail = T.detailFor(model, ref);
    if (detail) texts.push(detail.title, decode(detail.html));
    texts.push(decode(/data-topology-pin>([^<]*)</.exec(T.renderTopology(model, { pin: ref }))[1]));
  }
  return { html, texts: texts.map(String) };
}

// What a fixture contains, to hold the page against.
function known(fixture) {
  const all = JSON.stringify([fixture.fabric, fixture.options]);
  const phys = new Set();
  const walk = (value, key) => {
    if (Array.isArray(value)) value.forEach((item) => walk(item, key));
    else if (value && typeof value === "object") Object.entries(value).forEach(([name, item]) => walk(item, name));
    else if (/phy/i.test(key || "") && Number.isInteger(value)) phys.add(value);
    else if (typeof value === "string") for (const found of value.matchAll(/phy-[0-9]+(?::[0-9]+)*:([0-9]+)/g)) phys.add(Number(found[1]));
  };
  walk([fixture.fabric, fixture.options]);
  const addresses = [...new Set((all.match(/[0-9a-f]{16}/g) || []))];
  return { all, phys, addresses };
}

// Hardware strings outside these invented fabrics: none may reach a synthetic fabric's page.
const OURS = /\bLSI\b|SAS3x|SMCDRS|Supermicro|QuantaStor|\bmpr[0-9]/;

for (const fixture of fixtures) {
  test(`${fixture.id}: renders at two widths, every bay once, every line finite, every name led by its text`, () => {
    for (const width of [1150, 3372]) {
      const model = build(fixture, width);
      const html = T.renderTopology(model, {});
      assert.equal(model.counts.baysPlaced, model.counts.bayNodes, `${width}: every bay is placed once`);
      assert.equal(Boolean(model.transport), fixture.transport, `${width}: transport is ${fixture.transport ? "read" : "not read"}`);
      for (const link of model.links) for (const [x, y] of (link.points || []).concat(...(link.shape || []))) assert.ok(Number.isFinite(x) && Number.isFinite(y), `${width}: ${link.id} is routed with finite points`);
      for (const item of [...model.nodes.filter((entry) => !entry.mergedInto && !entry.physical), ...model.groups]) assert.ok([item.x, item.y, item.w, item.h].every(Number.isFinite), `${width}: ${item.id} has a place`);
      assert.ok(!model.notices.some((notice) => /^Not drawn|could not be routed/.test(notice)), `${width}: nothing is left undrawn (${model.notices.join(" | ")})`);
      // Every button and every link of the drawing.
      const named = [...html.matchAll(/<(g|a) class="topo-[^"]*"[^>]*(?:role="button"|href="[^"]*")[^>]*aria-label="([^"]*)"[^>]*>([\s\S]*?)<\/\1>/g)];
      assert.ok(named.length > 0);
      for (const [, tag, label, body] of named) {
        const visible = [...body.matchAll(/<text\b[^>]*>([^<]*)<\/text>/g)].map((found) => decode(found[1]).trim()).join(" ");
        assert.ok(decode(label).startsWith(visible), `${width}: <${tag}> "${visible}" leads its accessible name`);
        assert.ok(!/<title/.test(body), `${width}: no native tooltip on "${visible}"`);
      }
    }
  });

  test(`${fixture.id}: says nothing it was not given`, () => {
    const model = build(fixture);
    const { texts } = everyText(model);
    const { all, phys, addresses } = known(fixture);
    for (const text of texts) {
      assert.ok(!/\b(undefined|null|NaN)\b/.test(text), `no undefined, null or NaN: …${text.slice(0, 160)}…`);
      assert.ok(!OURS.test(text), `nothing of a captured system: ${OURS.exec(text)?.[0]} in …${text.slice(Math.max(0, (OURS.exec(text)?.index || 0) - 60), (OURS.exec(text)?.index || 0) + 40)}…`);
      // PHY numbers: "PHY 3", "PHY 0–7", "PHY 4, 6", "PHYs 0–3 | 4–11".
      for (const found of text.matchAll(/PHYs? ((?:[0-9]+(?:–[0-9]+)?(?:, | \| )?)+)/g)) {
        for (const number of found[1].match(/[0-9]+/g)) assert.ok(phys.has(Number(number)), `PHY ${number} is in the fixture: "${found[0]}"`);
      }
      // Whole SAS addresses, and their short forms.
      for (const found of text.match(/[0-9a-f]{16}/g) || []) assert.ok(all.includes(found), `address ${found} is in the fixture`);
      for (const found of text.matchAll(/…([0-9a-f]{4,16})\b/g)) assert.ok(addresses.some((address) => address.endsWith(found[1])), `short address …${found[1]} is the end of an address in the fixture`);
    }
  });

  test(`${fixture.id}: findings only where the fixture has a fault`, () => {
    const model = build(fixture);
    const found = model.checks.filter((check) => check.status === "critical" || check.status === "warning").map((check) => `${check.status}: ${check.title}: ${check.lines.join(" | ")}`);
    // Only the three-level cascade has counters on a PHY; nothing else has anything wrong.
    if (fixture.id.startsWith("cascade3")) same(found.map((line) => line.split(":").slice(0, 2).join(":")), ["warning: No PHY errors on the cables' lanes"], found.join("\n"));
    else same(found, [], found.join("\n"));
    assert.ok(![...model.flags.values()].flat().some((flag) => flag.tier === "critical"), "nothing critical");
  });
}

const stubsOf = (model) => model.nodes.filter((entry) => entry.neighbour || entry.stubGroup).map((entry) => entry.label);

test("a three-level cascade: each enclosure's page shows the others as stubs, in chain order, linked, without their bays", () => {
  for (const [id, own] of [["cascade3-top", 12], ["cascade3-middle", 8], ["cascade3-bottom", 6]]) {
    const model = build(byId[id]);
    same(stubsOf(model), byId[id].expect.stubs, id);
    assert.equal(model.bays.length, own, `${id}: only this enclosure's bays are drawn`);
    assert.equal(model.groups.length, 1, `${id}: one board`);
    same(model.links.filter((link) => link.lanes).map((link) => link.lanes.count), [4, 4, 4], `${id}: three x4 cables`);
    for (const stub of model.nodes.filter((entry) => entry.neighbour)) {
      assert.match(stub.neighbour.href, /^view\?enclosure=5bbb/, `${id}: ${stub.label} links to its enclosure's view`);
      assert.match(T.renderTopology(model, {}), new RegExp(`<a class="topo-node[^"]*topo-stub[^>]*href="${stub.neighbour.href.replace(/[?]/g, "\\?")}"`), `${id}: the stub is a link`);
    }
  }
  // The Friendly Label names the neighbour where one is saved; the label where not.
  const top = build(byId["cascade3-top"]);
  same(top.nodes.filter((entry) => entry.neighbour).map((entry) => [entry.neighbour.name, entry.neighbour.disks, entry.neighbour.bays, entry.level]), [["Scratch shelf", 8, 8, 2], ["Shelf C", 4, 6, 3]]);
  // Three addresses that end alike are told apart: the short form grows.
  same(top.nodes.filter((entry) => entry.kind === "expander").map((entry) => entry.lines.join(" ").match(/…[0-9a-f]+/)[0]), ["…a00ff", "…b00ff", "…c00ff"]);
  // The neighbours are named in the expander's card.
  const middle = build(byId["cascade3-middle"]);
  const chip = middle.groups[0].chips[0];
  const rows = Object.fromEntries(T.cardFor(middle, { kind: "node", id: chip.id }).rows.map((row) => [row.label, row.value]));
  assert.equal(rows.Upstream, "Shelf A (…a00ff), level 1");
  assert.equal(rows["Cascades to"], "Shelf C (…c00ff), level 3");
});

test("one expander with two children has two stubs; with five, one stub that lists them, and an unknown enclosure is said to be not read", () => {
  const two = build(byId["fanout-2"]);
  same(stubsOf(two), ["Next in chain: Leaf 1", "Next in chain: Leaf 2"]);
  const leaf = build(byId["fanout-2-leaf"]);
  same(stubsOf(leaf), ["Reached through: Head shelf", "Elsewhere in the fabric: Leaf 2"]);
  const five = build(byId["fanout-5"]);
  same(stubsOf(five), ["Next in chain: 5 enclosures"]);
  const group = five.nodes.find((entry) => entry.stubGroup);
  same(group.stubGroup.map((entry) => entry.neighbour.name).sort(), ["Leaf 1", "Leaf 2", "Leaf 3", "Leaf 4", "enclosure not read"]);
  const card = T.cardFor(five, { kind: "node", id: group.id });
  assert.equal(card.rows.length, 5, "the card lists every member");
  const unknown = group.stubGroup.find((entry) => !entry.neighbour.known);
  assert.equal(unknown.neighbour.href, "", "an enclosure that is not read has no link");
  assert.equal(unknown.neighbour.bays, null);
  // The grouped cables keep their lanes in the group cable's card.
  const cable = five.links.find((link) => link.stubCables);
  assert.equal(T.cardFor(five, { kind: "link", id: cable.id }).rows.length, 5);
  // The checks ran on every cable before the grouping.
  assert.match(five.checks.find((check) => check.id === "cascades").lines[0], /^5 cascades/);
});

test("two HBAs to one expander on Linux: one chip, both HBAs, and the unjoined second path is not read", () => {
  const model = build(byId["dual-domain-linux"]);
  assert.equal(model.groups[0].chips.length, 1);
  assert.equal(model.groups[0].chips[0].viaLine, "via host6 + host7, level 1");
  same(stubsOf(model), [], "the second host's row of the same expander is not a neighbour");
  assert.equal(model.links.filter((link) => link.cable).length, 2, "a cable from each HBA into the one chip");
  const byCheck = Object.fromEntries(model.checks.map((check) => [check.id, check]));
  assert.equal(byCheck.redundancy.status, "pass");
  assert.equal(byCheck.ports.status, "not_read");
  assert.match(byCheck.ports.lines[0], /a path via another HBA is not read for 8 disks/);
  assert.ok(model.bays.every((bay) => bay.ports.length === 1), "one mark per bay: the path that was joined");
});

test("an x4 port beside an x8 port to another backplane is not a finding; SATA disks are one port each and not judged for rate", () => {
  const model = build(byId["wide-x4-sata"]);
  same(model.links.filter((link) => link.lanes).map((link) => link.lanes.count).sort(), [4, 8]);
  assert.equal(model.checks.find((check) => check.id === "lanes").status, "pass");
  const sata = model.bays.filter((bay) => bay.protocol === "SATA");
  assert.equal(sata.length, 2);
  for (const bay of sata) {
    const rate = T.cardFor(model, { kind: "port", id: bay.ports[0].key }).rows.find((row) => row.label === "Rate");
    assert.equal(rate.value, "6 Gb/s (drive maximum not read)");
    assert.ok(!rate.warn, "a disk port at 6 Gb/s is not marked");
  }
  assert.match(model.checks.find((check) => check.id === "ports").lines[0], /2 SATA disks, one port each/);
});

test("an expander with no SES device: no enclosure is drawn or named, the disks hang off the expander", () => {
  const model = build(byId["no-ses"]);
  assert.equal(model.physical, null);
  assert.ok(model.links.some((link) => link.bundle && model.nodeById.get(link.source)?.kind === "expander"), "a bundle from the expander to its bays");
  assert.equal(model.bays.length, 4);
  assert.ok(model.bays.every((bay) => bay.ports.length === 1 && bay.ports[0].phy), "each disk on its expander PHY");
  assert.equal(model.checks.length, 0, "no wiring check without an enclosure to check");
  assert.ok(!model.tiers.some((tier) => /read complete/.test(tier.note)), "no claim that the enclosure read was complete");
});

test("an enclosure with no layout rows is drawn in bay order and says so; its expander still counts", () => {
  const model = build(byId["no-layout"]);
  assert.ok(model.groups.every((group) => !group.rows), "no rows are made up");
  assert.ok(model.notices.some((notice) => /no layout rows/.test(notice)));
  assert.equal(model.checks.find((check) => check.id === "redundancy").status, "pass");
});

test("unknown vendor, model, firmware and enclosure label: nothing is filled in", () => {
  const model = build(byId["unknown-strings"]);
  const hba = model.nodes.find((entry) => entry.kind === "controller");
  same(hba.lines, ["host8", "SAS …0801", "4 of 8 PHYs linked", "PHYs 0–3"]);
  const chip = model.groups[0].chips[0];
  same(chip.lines, ["Expander …9c9c", "via host8, level 1", "5def000000009c9c", "10 of 20 PHYs linked"]);
  const card = Object.fromEntries(T.cardFor(model, { kind: "node", id: hba.id }).rows.map((row) => [row.label, row.value]));
  assert.equal(card.Board, "not read");
  assert.equal(card.Firmware, "not read");
  // A PHY whose counters sysfs did not return is not read, not zero.
  assert.equal(model.checks.find((check) => check.id === "cable-errors").status, "not_read");
});

test("a silent peer fixture cannot add a head without the per-head backend contract", () => {
  const model = build(byId["pair-standby-silent"]);
  // Per-head collection/aggregation is deliberately deferred to #1481.
  assert.equal(model.shared, null);
  assert.equal(model.groups.length, 1);
  assert.equal(model.bays.length, 8);
  assert.ok(!model.nodes.some((entry) => entry.label === "node-b" || entry.peer));
  assert.ok(!model.checks.some((check) => check.status === "warning" || check.status === "critical"));
});

test("an empty, host-less or incomplete survey: the view is the SES view it was", () => {
  const model = build(byId["empty-survey"]);
  assert.equal(model.transport, null);
  assert.ok(model.tiers.some((tier) => tier.state === "not_collected"));
  assert.ok(!model.transportNote);
  assert.ok(model.checks.every((check) => check.status !== "critical" && check.status !== "warning"));
});

test("an older payload without the host-wide port census: every fixture still renders, and nothing is inferred", () => {
  for (const fixture of fixtures.filter((item) => item.transport)) {
    const model = build(fixture, 1150, (fabric) => {
      delete fabric.raw.sas_transport_ports;
      return fabric;
    });
    const { texts } = everyText(model);
    assert.ok(!texts.some((text) => /\binferred\b/i.test(text)), `${fixture.id}: nothing is called inferred`);
    assert.ok(model.links.filter((link) => link.lanes).every((link) => !link.lanes.targetPhys.length), `${fixture.id}: no far-end PHYs`);
    for (const stub of model.nodes.filter((entry) => entry.neighbour)) assert.equal(stub.neighbour.disks, null, `${fixture.id}: a stub's disks are not read without the port list`);
  }
});

test("a far end the port census only lets the page infer is called inferred on its line, its name, its card and its inspector", () => {
  let inferred = 0;
  for (const fixture of fixtures.filter((item) => item.transport)) {
    const model = build(fixture);
    const html = T.renderTopology(model, {});
    for (const link of model.links.filter((item) => item.lanes?.targetBasis === "inferred")) {
      inferred += 1;
      const ref = { kind: "link", id: link.id };
      assert.match(link.labelLines.join(" "), /↔ PHY [0-9–, ]+ inferred$/, `${fixture.id}: ${link.id} line label`);
      const named = [...html.matchAll(/<g class="topo-link-label[^>]*data-topo-link="([^"]*)" aria-label="([^"]*)"/g)].find((match) => decode(match[1]) === link.id);
      assert.ok(named, `${fixture.id}: ${link.id} is drawn with a name`);
      assert.match(decode(named[2]), /\(far end inferred\)/, `${fixture.id}: ${link.id} accessible name`);
      const card = T.cardFor(model, ref);
      assert.match(card.rows.find((row) => row.label === "Far end").value, /^inferred: /, `${fixture.id}: ${link.id} card`);
      assert.match(decode(T.detailFor(model, ref).html), /\binferred\b/, `${fixture.id}: ${link.id} inspector`);
    }
  }
  assert.ok(inferred > 10, `${inferred} inferred far ends checked`);
});

test("control: the checks above can fail. A page held against another fixture's contents is caught", () => {
  const { texts } = everyText(build(byId["wide-x4-sata"]));
  const other = known(byId["no-ses"]);
  const strangers = { phys: 0, addresses: 0 };
  for (const text of texts) {
    for (const found of text.matchAll(/PHYs? ((?:[0-9]+(?:–[0-9]+)?(?:, | \| )?)+)/g)) for (const number of found[1].match(/[0-9]+/g)) if (!other.phys.has(Number(number))) strangers.phys += 1;
    for (const found of text.match(/[0-9a-f]{16}/g) || []) if (!other.all.includes(found)) strangers.addresses += 1;
  }
  assert.ok(strangers.phys > 20 && strangers.addresses > 20, JSON.stringify(strangers));
  // And a captured system's word is caught where one is put.
  assert.ok(OURS.test("Expander of a Supermicro shelf") && OURS.test("via mpr0") && !OURS.test("via host6, ACME EXP-36"));
});
