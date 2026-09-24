"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "app/static/app.js"), "utf8");
const helperText = fs.readFileSync(path.join(__dirname, "refresh_races.test.js"), "utf8");
const helpers = vm.createContext({ assert });
vm.runInContext(helperText.slice(helperText.indexOf("function functionSource("), helperText.indexOf("function loadFunction(")), helpers);
function load(names, context) {
  vm.createContext(context);
  vm.runInContext(names.map(name => helpers.functionSource(source, name)).join("\n"), context);
  return context;
}
function deferred() { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise,resolve,reject}; }
const guardNames = ["currentUiScopeKey", "inventoryScopeMatchesSelection", "captureMutationContext", "mutationContextIsCurrent", "finishMutationContext"];

test("explicit enclosure cache miss never borrows or relabels another shelf", () => {
  const state = { snapshotReuseCache: {} }; let applied;
  const c = load(["cloneJsonValue", "snapshotReuseCacheKey", "snapshotHasTrustedTopology", "rememberReusableSnapshot", "findReusableSnapshot", "applyReusableSnapshot"], {state, applySnapshot(s) { applied=s; }, renderAll() {}});
  c.rememberReusableSnapshot({selected_system_id:"a", selected_enclosure_id:"shelf-a", slots:[{serial:"SYNTHETIC-A"}]});
  assert.equal(c.applyReusableSnapshot("a", "shelf-b"), false);
  assert.equal(applied, undefined);
  assert.equal(c.findReusableSnapshot("a", "shelf-b"), null);
  assert.equal(c.applyReusableSnapshot("a", "shelf-a"), true);
  assert.equal(applied.slots[0].serial, "SYNTHETIC-A");
});

for (const action of ["sendLedAction", "saveMapping", "clearMapping", "importMappingsFromFile"]) {
  for (const transition of ["system", "enclosure", "refresh-epoch", "slot", "draft", "same"]) {
    test(`${action} completion respects ${transition}`, async () => {
      const pending = deferred();
      const state = { snapshotMode:false, snapshot:{selected_system_id:"a",selected_enclosure_id:"one"}, selectedSystemId:"a", selectedEnclosureId:"one", selectedSlot:0, latestRefreshToken:1, mappingDraftRevision:0, snapshotReuseCache:{}, mappingFormScopeKey:"original" };
      let applied=0, requests=0;
      const context = {state, URLSearchParams, mappingForm:{}, FormData:class { get() { return "draft"; } }, window:{confirm:()=>true}, mappingImportFile:{value:""}, mappingImportUnavailableReason:()=>"", mappingImportPreviewMessage:()=>"confirm", writeBlockedByPolicy:()=>false, getSlotById:()=>({slot:0,slot_label:"00",led_supported:true,mapping_revision:"r",mapping_clear_revision:"r"}), setStatus(){}, sendScopedRequest:async url=>{requests++; return url.endsWith("preview") ? {revision:"r"} : pending.promise;}, applySnapshot(){applied++;}, invalidateHistoryCaches(){}, renderAll(){}, scheduleSmartPrefetch(){}, ledBackendLabel:()=>"synthetic", handleWriteRejection(){} };
      // Before the fix the handlers have no completion guard. Load new helpers only once present.
      const available = guardNames.filter(n => source.includes(`function ${n}(`));
      const c=load([...available, action],context);
      const args = action === "saveMapping" ? [{preventDefault(){}}] : action === "importMappingsFromFile" ? [{name:"synthetic.json",text:async()=>"{}"}] : ["IDENTIFY"];
      const run=c[action](...args);
      await new Promise(resolve=>setImmediate(resolve));
      if (transition === "system") state.selectedSystemId="b";
      if (transition === "enclosure") state.selectedEnclosureId="two";
      if (transition === "refresh-epoch") state.latestRefreshToken++;
      if (transition === "slot") state.selectedSlot=1;
      if (transition === "draft") state.mappingDraftRevision++;
      pending.resolve({snapshot:{selected_system_id:"a",selected_enclosure_id:"one"}, imported:1});
      await run;
      assert.ok(requests > 0);
      assert.equal(applied, transition === "same" ? 1 : 0);
      if (transition !== "same") assert.equal(state.mappingFormScopeKey,"original");
    });
  }
}

for (const outcome of ["success", "failure"]) {
 test(`storage runtime ignores old-scope ${outcome}`, async () => {
  const d=deferred(); let applied=0, statuses=0, renders=0;
  const state={snapshotMode:false, selectedSystemId:"a",selectedEnclosureId:"one",storageViewsRuntimeRequestToken:0};
  const names=["currentUiScopeKey", "fetchStorageViewRuntime"].filter(n=>source.includes(`function ${n}(`));
  const c=load(names,{state,renderSelectors(){renders++;},buildSelectionParams:()=>new URLSearchParams(),fetchJson:()=>d.promise,applyStorageViewRuntime(){applied++;},renderAll(){renders++;},renderStorageViewRuntimeStatus(){},setStatus(){statuses++;}});
  const p=c.fetchStorageViewRuntime(false,true); state.selectedSystemId="b"; state.storageViewsRuntimeLoading=true;
  const before=renders;
  if(outcome==="success") d.resolve({system_id:"a",views:[]}); else d.reject(new Error("synthetic failure"));
  await p;
  assert.equal(applied,0); assert.equal(statuses,0); assert.equal(renders,before); assert.equal(state.storageViewsRuntimeLoading,true);
 });
}
test("quiet runtime failure is explicitly stale and retryable", async () => {
 const state={snapshotMode:false,selectedSystemId:"a",selectedEnclosureId:"one",storageViewsRuntimeRequestToken:0,storageViewsRuntime:{system_id:"a",views:[{id:"old"}]}}; let status="";
 const names=["currentUiScopeKey", "fetchStorageViewRuntime"].filter(n=>source.includes(`function ${n}(`));
 const c=load(names,{state,renderSelectors(){},renderAll(){},renderStorageViewRuntimeStatus(){},buildSelectionParams:()=>new URLSearchParams(),fetchJson:async()=>{throw new Error("synthetic unavailable");},setStatus(m){status=m;}});
 await c.fetchStorageViewRuntime(false,true);
 assert.match(status,/Storage view refresh failed/);
 assert.equal(state.storageViewsRuntimeError,"synthetic unavailable");
});

for (const included of [true, false]) {
 test(`export explanation uses selected view identity, included=${included}`, () => {
  const state={selectedSlot:7,selectedStorageViewRuntimeId:"virtual",snapshot:{selected_enclosure_id:"one"},history:{panelOpen:false},export:{packaging:"auto",estimate:{},includeStorageViews:included}};
  const note={textContent:""};
  const names=["snapshotExportSelectionDescription", "syncSnapshotExportDialog"].filter(n=>source.includes(`function ${n}(`));
  const c=load(names,{state,exportRedactToggle:null,exportPackagingSelect:null,exportAllowOversizeToggle:null,exportSnapshotNote:note,exportSnapshotWindowHint:null,exportSnapshotConfirm:null,renderSnapshotExportScopeControls(){},getSelectedEnclosureOption:()=>({label:"Live Shelf"}),selectedExportEnclosureIds:()=>["one"],selectedExportStorageViewIds:()=>included?["virtual"]:[],snapshotExportRequestPayload:()=>({selected_slot:7,selected_storage_view_id:"virtual",storage_view_ids:included?["virtual"]:[]}),getStorageViewRuntimeById:()=>({label:"Virtual Archive",slots:[{slot_index:7,slot_label:"07"}]}),getSlotById:()=>({slot_label:"WRONG LIVE BAY"}),formatHistoryWindowDescription:()=>"24h",currentHistoryWindowHours:()=>24,isHistoryAvailable:()=>false,renderSnapshotExportEstimate(){}});
  c.syncSnapshotExportDialog();
  assert.match(note.textContent,/Virtual Archive/);
  assert.doesNotMatch(note.textContent,/WRONG LIVE BAY|No slot is currently selected/);
  assert.match(note.textContent,included?/stay selected/:/not included|cleared/);
 });
}

test("history and export declare honest accessible state", () => {
 const template=fs.readFileSync(path.join(root,"app/templates/index.html"),"utf8");
 assert.match(template, /id="export-snapshot-dialog"[^>]*aria-labelledby="export-snapshot-title"/);
 assert.match(template, /id="history-toggle-button"[^>]*aria-controls="detail-history-panel"/);
 assert.doesNotMatch(source,/History backend reachable, but this slot query failed/);
 assert.match(helpers.functionSource(source,"renderHistoryPanel"),/aria-pressed/);
});

function importFixture() {
  const state = {snapshotMode:false, snapshot:{selected_system_id:"a",selected_enclosure_id:"one"}, selectedSystemId:"a",selectedEnclosureId:"one",selectedSlot:0,latestRefreshToken:1,mappingDraftRevision:0};
  const input = {value:"",files:[]};
  const requests = [], applied = [], errors = [];
  const c = load([...guardNames,"importMappingsFromFile"], {
    state, mappingImportFile:input, window:{confirm:()=>true},
    mappingImportUnavailableReason:()=>"", mappingImportPreviewMessage:()=>"confirm",
    writeBlockedByPolicy:()=>false, setStatus(){},
    sendScopedRequest:async url => { requests.push(url); return url.endsWith("preview") ? {revision:"r"} : {snapshot:{},imported:1}; },
    applySnapshot:s=>applied.push(s), invalidateHistoryCaches(){}, renderAll(){}, scheduleSmartPrefetch(){},
    handleWriteRejection:e=>errors.push(e),
  });
  function select(file) { input.files=[file]; input.value=file.name; }
  return {c,state,input,requests,applied,errors,select};
}
function abandonImport(state, transition) {
  if (transition === "system") state.selectedSystemId="b";
  if (transition === "enclosure") state.selectedEnclosureId="two";
  if (transition === "slot") state.selectedSlot=1;
  if (transition === "refresh") state.latestRefreshToken++;
  if (transition === "draft") state.mappingDraftRevision++;
}
const tick = () => new Promise(resolve=>setImmediate(resolve));
for (const phase of ["read", "preview"]) {
  for (const transition of ["system","enclosure","slot","refresh","draft"]) {
    for (const newerName of [null,"newer.json","synthetic.json"]) {
      test(`import ownership ${phase}/${transition}/newer=${newerName}`, async () => {
        const h=importFixture(), pending=deferred();
        const file={name:"synthetic.json",text:()=>phase==="read"?pending.promise:Promise.resolve("{}")};
        h.select(file);
        h.c.sendScopedRequest=async url=>{h.requests.push(url); return pending.promise;};
        const run=h.c.importMappingsFromFile(file);
        await tick();
        assert.equal(h.requests.length,phase==="read"?0:1);
        abandonImport(h.state,transition);
        const newer={name:newerName,text:async()=>"{}"};
        if(newerName) h.select(newer);
        pending.resolve(phase==="read"?"{}":{revision:"r"});
        await run;
        assert.equal(h.input.value,newerName || "");
        if(newerName) assert.equal(h.input.files[0],newer);
        assert.equal(h.requests.length,phase==="read"?0:1);
        assert.equal(h.applied.length,0);
        assert.equal(h.errors.length,0);
      });
    }
  }
}
for (const phase of ["read","preview"]) {
  test(`import ownership stale ${phase} error clears own file silently`, async()=>{
    const h=importFixture(), pending=deferred();
    const file={name:"synthetic.json",text:()=>phase==="read"?pending.promise:Promise.resolve("{}")};
    h.select(file);
    h.c.sendScopedRequest=async url=>{h.requests.push(url);return pending.promise;};
    const run=h.c.importMappingsFromFile(file); await tick();
    abandonImport(h.state,"draft"); pending.reject(new Error("synthetic failure")); await run;
    assert.equal(h.input.value,""); assert.equal(h.errors.length,0); assert.equal(h.applied.length,0);
  });
}
for (const outcome of ["success","cancel","read-error","preview-error","import-error"]) {
  test(`import ownership ${outcome} cleanup`,async()=>{
    const h=importFixture();
    const file={name:"synthetic.json",text:async()=>{if(outcome==="read-error") throw new Error("read");return "{}";}};
    h.select(file); h.c.window.confirm=()=>outcome!=="cancel";
    h.c.sendScopedRequest=async url=>{h.requests.push(url);if(url.endsWith(outcome==="preview-error"?"preview":"import") && outcome.endsWith("error")) throw new Error("request");return {revision:"r",snapshot:{},imported:1};};
    await h.c.importMappingsFromFile(file);
    assert.equal(h.input.value,""); assert.equal(h.applied.length,outcome==="success"?1:0);
    assert.equal(h.errors.length,outcome.endsWith("error")?1:0);
  });
}
for (const sameObject of [false,true]) {
  for (const reverse of [false,true]) {
    test(`import ownership overlapping operations sameObject=${sameObject} reverse=${reverse}`,async()=>{
      const h=importFixture(), first=deferred(), second=deferred(); let reads=0;
      const a={name:"synthetic.json",text:()=>reads++===0?first.promise:second.promise};
      const b=sameObject?a:{name:"synthetic.json",text:()=>second.promise};
      h.select(a);const old=h.c.importMappingsFromFile(a);await tick();
      h.state.selectedEnclosureId="two"; h.state.snapshot.selected_enclosure_id="two";
      h.select(b);const newer=h.c.importMappingsFromFile(b);await tick();
      if(reverse) {
        second.resolve("{}"); await newer; assert.equal(h.input.value,"");
        const third={name:"synthetic.json",text:async()=>"{}"}; h.select(third);
        first.resolve("{}");await old;assert.equal(h.input.value,"synthetic.json");assert.equal(h.input.files[0],third);
      } else {
        first.resolve("{}");await old;assert.equal(h.input.value,"synthetic.json");assert.equal(h.input.files[0],b);
        second.resolve("{}");await newer;assert.equal(h.input.value,"");
      }
      assert.equal(h.requests.length,2);assert.equal(h.applied.length,1);
    });
  }
}

test("runtime removal of selected view rerenders the now-live grid", async () => {
 const state={snapshotMode:false,selectedSystemId:"a",selectedEnclosureId:"one",selectedStorageViewRuntimeId:"removed",storageViewsRuntimeRequestToken:0};let rendered=0;
 const c=load(["currentUiScopeKey","fetchStorageViewRuntime"],{state,renderSelectors(){},renderAll(){rendered++;},renderStorageViewRuntimeStatus(){},buildSelectionParams:()=>new URLSearchParams(),fetchJson:async()=>({system_id:"a",views:[]}),applyStorageViewRuntime(){state.selectedStorageViewRuntimeId="";},setStatus(){}});
 await c.fetchStorageViewRuntime(false,true);
 assert.equal(rendered,1);
});
