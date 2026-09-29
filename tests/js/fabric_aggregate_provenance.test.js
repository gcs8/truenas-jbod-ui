"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(path.resolve(__dirname, "../../app/static/sas_fabric_view.js"), "utf8");
function fixture(n){
 const slots=Array.from({length:n},(_,slot)=>({slot,physical_location_known:true,state:'online',device_name:'da'+slot,model:'Synthetic disk',serial:'SYNTH-'+slot}));
 const all=slots.map(x=>x.slot); const nodes=[{id:'host',kind:'host',label:'Synthetic host',related_slots:all,metrics:{}}],controllers=[],paths=[],traces=[],links=[];
 for(let c=0;c<2;c++){
  const name='mpr'+c,id='controller:'+name; controllers.push({id,name,related_slots:all}); nodes.push({id,kind:'controller',label:name,related_slots:all,metrics:{}});
  for(let p=0;p<2;p++){const ps=all.filter(s=>s%2===p);const pid='path:'+name+':'+p;paths.push({id:pid,controller:name,slots:ps,state:'active'});nodes.push({id:pid,kind:'path',label:pid,related_slots:ps,metrics:{}});traces.push({id:pid,kind:'path',slots:ps,node_ids:['host',id,pid],link_ids:[],metrics:{}});}
  for(let e=0;e<4;e++){const es=all.filter(s=>s%4===e); for(const kind of ['expander','ses-enclosure'])nodes.push({id:kind+':'+c+':'+e,kind,label:kind+e,controller_id:id,related_slots:es,metrics:{}});}
 }
 slots.forEach(s=>{const ids=['host','controller:mpr0','path:mpr0:'+s.slot%2,'expander:0:'+s.slot%4,'ses-enclosure:0:'+s.slot%4,'bay:'+s.slot];nodes.push({id:'bay:'+s.slot,kind:'bay',label:'Bay '+s.slot,related_slots:[s.slot],metrics:{physical_location_known:true}});traces.push({id:'bay:'+s.slot,kind:'bay',label:'Bay '+s.slot,slots:[s.slot],node_ids:ids,link_ids:[],metrics:{physical_location_known:true}});});
 return {snapshot:{slots,systems:[],enclosures:[]},fabric:{available:true,platform:'core',fabric_kind:'sas',system_id:'synthetic',nodes,controllers,paths,traces,links,aliases:[],warnings:[]}};
}

function load(data = fixture(2), instrument = false) {
  const stats = { traceBuilds: 0, traceEntries: 0, slotEntries: 0 };
  if (instrument) {
    for (const trace of data.fabric.traces) {
      const id = trace.id;
      Object.defineProperty(trace, "id", { get() { stats.traceEntries++; return id; } });
    }
    for (const slot of data.snapshot.slots) {
      const number = slot.slot;
      Object.defineProperty(slot, "slot", { get() { stats.slotEntries++; return number; } });
    }
  }
  class CountedMap extends Map {
    constructor(entries) {
      super(entries);
      if (instrument && entries && entries.length && entries[0][1] && data.fabric.traces.includes(entries[0][1])) stats.traceBuilds++;
    }
  }
  const elements = new Map();
  const window = { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: data };
  const sandbox = vm.createContext({ Map: CountedMap, URLSearchParams, window, document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "" });
      return elements.get(id);
    }, querySelectorAll: () => [],
  }});
  const end = source.indexOf("  elements.modeButtons.forEach((button) => {", source.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${source.slice(0, end)}
    window.api = { state, diskLocation, slotByNumber, traceMap, renderPathButton, renderLanesMode, renderImpactMode, renderTraceMode, renderBayChips, renderSlotList, renderInspector, renderDiskPathMode, branchSeedsForDiskTrace, renderDiskPathBranch };
  })();`, sandbox);
  window.api.state.selectedTraceId = "bay:0";
  return { ...window.api, elements, stats };
}
function text(html) { return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim(); }
function provenance(data, kind) {
  for (const slot of data.snapshot.slots) {
    delete slot.physical_location_known;
    if (kind === "physical" || kind === "contradictory" || (kind === "mixed" && slot.slot === 0)) slot.physical_location_known = true;
    if (kind === "virtual") slot.raw_status = { virtual_enclosure: true };
  }
  for (const item of [...data.fabric.nodes, ...data.fabric.traces]) {
    if (item.kind !== "bay") continue;
    item.label = "Synthetic disk";
    item.metrics = kind === "contradictory" ? { physical_location_known: false } : {};
  }
  return data;
}
module.exports = { fixture, load, text, provenance, source };
if (require.main === module) {
  for (const n of [1, 2, 125]) for (const kind of ["physical", "virtual", "mixed", "missing", "contradictory"]) {
    test(`aggregate provenance ${kind}, ${n} members`, () => {
      const data = provenance(fixture(n), kind);
      const slots = data.snapshot.slots.map(s => s.slot);
      data.fabric.paths[0].slots = slots;
      const ui = load(data);
      const physical = kind === "physical" || (kind === "mixed" && n === 1);
      const noun = physical ? "bay" : "disk";
      assert.match(text(ui.renderPathButton(data.fabric.paths[0])), new RegExp(`\\b${n} ${noun}${n === 1 ? "" : "s"}\\b`));
      assert.match(text(ui.renderLanesMode(data.fabric)), new RegExp(`Impacted ${physical ? "Bays" : "Disks"}`));
      assert.match(text(ui.renderImpactMode(data.fabric)), new RegExp(`${n} affected ${noun}${n === 1 ? "" : "s"}\\b`));
      if (n > 120) assert.match(text(ui.renderBayChips(slots, 120)), new RegExp(`\\+5 ${noun}s`));
      assert.match(ui.renderBayChips(slots), /data-fabric-trace="bay:0"/);
    });
  }
  for (const kind of ["virtual", "mixed", "missing", "contradictory"]) {
    for (const mode of ["renderLanesMode", "renderImpactMode", "overflow"]) test(`${mode} denial: ${kind}`, () => {
      const data = provenance(fixture(125), kind);
      const ui = load(data);
      const html = mode === "overflow" ? ui.renderBayChips(data.snapshot.slots.map(s => s.slot), 120) : ui[mode](data.fabric);
      assert.doesNotMatch(text(html), /\bbays?\b/i);
    });
  }
  for (const count of [0, 4]) test(`empty/count-only aggregates: ${count}`, () => {
    const data = fixture(0);
    data.fabric.paths[0].count = count;
    const ui = load(data);
    for (const html of [ui.renderPathButton(data.fabric.paths[0]), ui.renderLanesMode(data.fabric), ui.renderImpactMode(data.fabric), ui.renderBayChips([]), ui.renderSlotList([])]) {
      assert.doesNotMatch(text(html), /\bbays?\b/i);
    }
  });
  test("count exceeding confirmed membership is not physical", () => {
    const data = fixture(2);
    data.fabric.paths[0].count = 4;
    assert.match(text(load(data).renderPathButton(data.fabric.paths[0])), /4 disks/);
  });
}

if (require.main === module) for (const label of ["Friendly source", '<img src=x onerror="alert(1)">', undefined, ""]) for (const view of ["active", "generic"]) {
  test(`controller display label is text only in ${view} view: ${label}`, () => {
    const data = fixture(2);
    const escaped = label ? label.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;") : "storage-v2:synthetic-1";
    // Two identical human labels must not merge canonical routes. Put the wrong
    // controller/enclosure first so fallback-to-first cannot conceal a bad join.
    for (let c = 0; c < 2; c++) {
      const name = `storage-v2:synthetic-${c}`, id = `controller:${name}`;
      const old = `controller:mpr${c}`;
      data.fabric.controllers[c] = {id, name, label, related_slots: [c]};
      for (const node of data.fabric.nodes) {
        if (node.id === old) { node.id = id; node.label = label || name; }
        if (node.controller_id === old) node.controller_id = id;
      }
      for (const p of data.fabric.paths.filter(p => p.controller === `mpr${c}`)) {
        p.controller = name; p.controller_label = label;
      }
      for (const trace of data.fabric.traces) trace.node_ids = trace.node_ids.map(x => x === old ? id : x);
    }
    const bay = data.fabric.traces.find(t => t.id === "bay:0");
    bay.node_ids = ["host", ...data.fabric.nodes.filter(n => ["controller", "ses-enclosure"].includes(n.kind)).map(n => n.id), "bay:0"];
    bay.metrics.path_states = [0, 1].map(c => ({controller: `storage-v2:synthetic-${c}`, controller_label: label, state: c ? "fault" : "healthy", device_name: `da${c}`, path_id: data.fabric.paths[c * 2].id}));
    const generic = {...bay, id: "synthetic-trace", kind: "path"};
    data.fabric.traces.push(generic);
    const ui = load(data);
    ui.renderInspector(data.fabric);
    const active = ui.elements.get("fabric-inspector-body").innerHTML;
    if (view === "active") assert.ok(active.split("<h4>Path Members</h4>")[1].split("</section>")[0].includes(`<strong>${escaped}</strong>`));
    assert.match(active, /status-healthy/);
    assert.match(active, /status-fault/);
    if (label) assert.ok(!active.includes(label) || label === "Friendly source");
    ui.state.selectedTraceId = generic.id;
    ui.renderInspector(data.fabric);
    if (view === "generic") assert.ok(ui.elements.get("fabric-inspector-body").innerHTML.includes(`<span>${escaped}</span>`));
    const seeds = ui.branchSeedsForDiskTrace(bay, data.fabric);
    assert.equal(seeds.length, 2);
    const branch = ui.renderDiskPathBranch(seeds[1], bay, data.fabric, 0);
    assert.match(branch, /data-fabric-node="controller:storage-v2:synthetic-1"/);
    assert.match(branch, /data-fabric-node="ses-enclosure:1:0"/);
    assert.doesNotMatch(branch, /data-fabric-node="ses-enclosure:0:0"/);
    ui.state.selectedDiskTraceId = bay.id;
    const diskMode = ui.renderDiskPathMode(data.fabric);
    const secondBranch = diskMode.split('<section class="disk-path-branch status-fault">')[1].split("</section>")[0];
    assert.match(secondBranch, /data-fabric-node="controller:storage-v2:synthetic-1"/);
    assert.match(secondBranch, /data-fabric-node="ses-enclosure:1:0"/);
    assert.doesNotMatch(secondBranch, /data-fabric-node="ses-enclosure:0:0"/);
    const lanes = ui.renderLanesMode(data.fabric);
    for (const p of data.fabric.paths) assert.ok(lanes.includes(`data-fabric-trace="${p.id}"`));
    const buttons = ui.renderPathButton(data.fabric.paths[2]);
    assert.ok(buttons.includes(`<span>${escaped}</span>`));
    assert.ok(ui.renderImpactMode(data.fabric).includes(escaped));
  });
}
