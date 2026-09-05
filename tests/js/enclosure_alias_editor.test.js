"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const STYLES = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

function functionSource(source, name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = source.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = source.indexOf(")", start);
  const bodyStart = source.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
    const next = source[index + 1];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (blockComment) {
      if (character === "*" && next === "/") {
        blockComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "/" && next === "/") {
      lineComment = true;
      index += 1;
      continue;
    }
    if (character === "/" && next === "*") {
      blockComment = true;
      index += 1;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ URLSearchParams, ...context });
  const source = names.map((name) => functionSource(APP_SOURCE, name)).join("\n");
  const exports = names.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "enclosure-alias-editor.behavior.js" });
  return Object.fromEntries(names.map((name) => [name, sandbox[`__${name}`]]));
}

function classList(initial = ["hidden"]) {
  const values = new Set(initial);
  return {
    values,
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    toggle(name, force) {
      if (force === undefined ? !values.has(name) : force) values.add(name);
      else values.delete(name);
    },
    contains(name) { return values.has(name); },
  };
}

test("alias editor availability excludes snapshots and saved storage views", () => {
  const state = { snapshotMode: false, selectedStorageViewRuntimeId: "", selectedEnclosureId: "enc-a" };
  const { enclosureAliasEditorAvailable } = loadFunctions(["enclosureAliasEditorAvailable"], {
    state,
    getSelectedEnclosureOption: () => ({ id: "enc-a" }),
  });

  assert.equal(enclosureAliasEditorAvailable(), true);
  state.snapshotMode = true;
  assert.equal(enclosureAliasEditorAvailable(), false);
  state.snapshotMode = false;
  state.selectedStorageViewRuntimeId = "saved-view";
  assert.equal(enclosureAliasEditorAvailable(), false);
});

test("opening and canceling the editor preserves raw context and restores focus", () => {
  let inputFocused = 0;
  let inputSelected = 0;
  let buttonFocused = 0;
  const state = { snapshotMode: false, selectedStorageViewRuntimeId: "", enclosureAliasEditorOpen: false };
  const formClasses = classList();
  const buttonClasses = classList([]);
  const input = {
    value: "",
    focus() { inputFocused += 1; },
    select() { inputSelected += 1; },
  };
  const rawHint = { textContent: "" };
  const button = { classList: buttonClasses, focus() { buttonFocused += 1; } };
  const form = { classList: formClasses };
  const selected = { id: "enc-a::drawer-top", label: "Archive East · Drawer 1-42 (Top)", raw_label: "Dell Drawer 1-42 (Top)", alias: "Archive East" };
  const fns = loadFunctions(
    [
      "currentEnclosureAliasScopeKey",
      "enclosureAliasEditorAvailable",
      "openEnclosureAliasEditor",
      "closeEnclosureAliasEditor",
    ],
    {
      state,
      getSelectedEnclosureOption: () => selected,
      enclosureAliasForm: form,
      enclosureAliasEditButton: button,
      enclosureAliasInput: input,
      enclosureAliasRawHint: rawHint,
    }
  );

  fns.openEnclosureAliasEditor();
  assert.equal(state.enclosureAliasEditorOpen, true);
  assert.equal(input.value, "Archive East");
  assert.equal(rawHint.textContent, "Raw: Dell Drawer 1-42 (Top)");
  assert.equal(formClasses.contains("hidden"), false);
  assert.equal(buttonClasses.contains("hidden"), true);
  assert.equal(inputFocused, 1);
  assert.equal(inputSelected, 1);

  fns.closeEnclosureAliasEditor(true);
  assert.equal(state.enclosureAliasEditorOpen, false);
  assert.equal(formClasses.contains("hidden"), true);
  assert.equal(buttonClasses.contains("hidden"), false);
  assert.equal(buttonFocused, 1);
});

test("Escape from any alias editor child closes the editor and restores focus", () => {
  let prevented = 0;
  let closed = 0;
  const { handleEnclosureAliasEditorKeydown } = loadFunctions(["handleEnclosureAliasEditorKeydown"], {
    closeEnclosureAliasEditor(restoreFocus) {
      assert.equal(restoreFocus, true);
      closed += 1;
    },
  });

  handleEnclosureAliasEditorKeydown({
    key: "Escape",
    target: { id: "enclosure-alias-clear" },
    preventDefault() { prevented += 1; },
  });
  assert.equal(prevented, 1);
  assert.equal(closed, 1);

  handleEnclosureAliasEditorKeydown({
    key: "Enter",
    target: { id: "enclosure-alias-clear" },
    preventDefault() { prevented += 1; },
  });
  assert.equal(prevented, 1);
  assert.equal(closed, 1);

  assert.match(
    APP_SOURCE,
    /enclosureAliasForm\.addEventListener\("keydown", handleEnclosureAliasEditorKeydown\)/
  );
  assert.doesNotMatch(APP_SOURCE, /enclosureAliasInput\.addEventListener\("keydown"/);
});

test("changing the selected enclosure closes an open alias draft", () => {
  const state = {
    snapshotMode: false,
    selectedStorageViewRuntimeId: "",
    selectedSystemId: "system-a",
    enclosureAliasEditorOpen: false,
    enclosureAliasEditorScopeKey: null,
  };
  const formClasses = classList();
  const buttonClasses = classList([]);
  const form = { classList: formClasses };
  const button = { classList: buttonClasses };
  const input = { value: "", focus() {}, select() {} };
  const rawHint = { textContent: "" };
  let selected = { id: "enc-a", raw_label: "Shelf A", alias: "Archive A" };
  const fns = loadFunctions(
    [
      "currentEnclosureAliasScopeKey",
      "enclosureAliasEditorAvailable",
      "openEnclosureAliasEditor",
      "renderEnclosureAliasEditor",
    ],
    {
      state,
      getSelectedEnclosureOption: () => selected,
      enclosureAliasForm: form,
      enclosureAliasEditButton: button,
      enclosureAliasInput: input,
      enclosureAliasRawHint: rawHint,
    }
  );

  fns.openEnclosureAliasEditor();
  assert.equal(state.enclosureAliasEditorOpen, true);
  selected = { id: "enc-b", raw_label: "Shelf B", alias: null };

  fns.renderEnclosureAliasEditor();

  assert.equal(state.enclosureAliasEditorOpen, false);
  assert.equal(state.enclosureAliasEditorScopeKey, null);
  assert.equal(formClasses.contains("hidden"), true);
  assert.equal(buttonClasses.contains("hidden"), false);
  assert.equal(rawHint.textContent, "Raw: Shelf B");
});

test("live navigation closes an alias draft before changing selection", () => {
  assert.match(
    APP_SOURCE,
    /systemSelect\.addEventListener\("change",[\s\S]*closeEnclosureAliasEditor\(false\);[\s\S]*state\.selectedSystemId = nextSystemId;/
  );
  assert.match(
    APP_SOURCE,
    /enclosureSelect\.addEventListener\("change",[\s\S]*closeEnclosureAliasEditor\(false\);[\s\S]*state\.selectedEnclosureId =/
  );
});

test("blank submit clears the base enclosure alias and refreshes the live snapshot", async () => {
  const requests = [];
  let refreshed = 0;
  let closed = 0;
  const state = {
    snapshotMode: false,
    selectedStorageViewRuntimeId: "",
    selectedSystemId: "system-a",
    selectedEnclosureId: "enc-a::drawer-top",
  };
  const { submitEnclosureAlias } = loadFunctions(["submitEnclosureAlias"], {
    state,
    enclosureAliasInput: { value: "   " },
    getSelectedEnclosureOption: () => ({ id: "enc-a::drawer-top" }),
    currentLiveEnclosureId: () => "enc-a::drawer-top",
    fetchJson: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, cleared: true };
    },
    writeBlockedByPolicy: () => false,
    closeEnclosureAliasEditor() { closed += 1; },
    async refreshSnapshot(force) {
      assert.equal(force, true);
      refreshed += 1;
    },
    setStatus() {},
  });

  await submitEnclosureAlias({ preventDefault() {} });

  assert.equal(requests.length, 1);
  const [request] = requests;
  assert.match(request.url, /^\/api\/sas-fabric\/aliases\?/);
  const params = new URLSearchParams(request.url.split("?", 2)[1]);
  assert.equal(params.get("system_id"), "system-a");
  assert.equal(params.get("enclosure_id"), "enc-a::drawer-top");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), {
    object_id: "enc-a",
    object_kind: "enclosure",
    label: null,
    scope: "system",
  });
  assert.equal(closed, 1);
  assert.equal(refreshed, 1);
});

test("a rejected alias write reports the server detail and keeps the editor open", async () => {
  const statuses = [];
  const rejections = [];
  let focused = 0;
  let closed = 0;
  let refreshed = 0;
  const state = {
    snapshotMode: false,
    selectedStorageViewRuntimeId: "",
    selectedSystemId: "system-a",
    selectedEnclosureId: "enc-a",
    writePolicy: { enabled: true, mode: "basic", reason: "" },
  };
  const { submitEnclosureAlias, writeBlockedByPolicy, writePolicyAllowsWrites, writePolicyReason } = loadFunctions(
    ["submitEnclosureAlias", "writeBlockedByPolicy", "writePolicyAllowsWrites", "writePolicyReason"],
    {
      state,
      enclosureAliasInput: { value: "Archive East", focus() { focused += 1; } },
      getSelectedEnclosureOption: () => ({ id: "enc-a" }),
      currentLiveEnclosureId: () => "enc-a",
      fetchJson: async () => {
        const denied = new Error("Read UI authentication required.");
        denied.status = 401;
        denied.detail = "Read UI authentication required.";
        throw denied;
      },
      handleWriteRejection(error) {
        rejections.push(error);
        state.writePolicy = { enabled: false, mode: "basic", reason: error.detail };
        return true;
      },
      closeEnclosureAliasEditor() { closed += 1; },
      async refreshSnapshot() { refreshed += 1; },
      setStatus(message, tone) { statuses.push({ message, tone }); },
    }
  );

  await submitEnclosureAlias({ preventDefault() {} });

  assert.equal(rejections.length, 1);
  assert.equal(rejections[0].status, 401);
  assert.deepEqual(statuses, [{ message: "Read UI authentication required.", tone: "error" }]);
  assert.equal(focused, 1);
  assert.equal(closed, 0);
  assert.equal(refreshed, 0);

  // The next attempt is refused before any request is sent.
  await submitEnclosureAlias({ preventDefault() {} });
  assert.equal(rejections.length, 1);
  assert.equal(statuses.length, 2);
  assert.equal(statuses[1].message, "Read UI authentication required.");
  assert.equal(writePolicyAllowsWrites(), false);
  assert.equal(writeBlockedByPolicy(), true);
  assert.equal(writePolicyReason(), "Read UI authentication required.");
});

test("clear control clears the draft and submits the clear operation", async () => {
  const input = { value: "Archive East" };
  let submissions = 0;
  const { clearEnclosureAlias } = loadFunctions(["clearEnclosureAlias"], {
    enclosureAliasInput: input,
    async submitEnclosureAlias() { submissions += 1; },
  });

  await clearEnclosureAlias();

  assert.equal(input.value, "");
  assert.equal(submissions, 1);
});

test("multi-enclosure live title prefers the selected option label", () => {
  const state = { snapshot: { selected_system_label: "System A", enclosures: [{ id: "enc-a" }, { id: "enc-b" }], layout_slot_count: 1 } };
  const { buildViewProfile } = loadFunctions(["buildViewProfile"], {
    state,
    getSelectedStorageViewRuntime: () => null,
    getSelectedProfile: () => ({ id: "profile", label: "Profile", panel_title: "Raw Profile Title", slot_layout: [[0]] }),
    getSelectedSystemOption: () => ({ label: "System A" }),
    getSelectedEnclosureOption: () => ({ id: "enc-b", label: "Archive East" }),
    currentLiveEnclosureLabel: () => "Archive East",
    activeLayoutRows: () => [[0]],
    countLayoutSlots: () => 1,
  });

  assert.equal(buildViewProfile().enclosureTitle, "Archive East");
});

test("template and stylesheet expose an accessible inline alias editor", () => {
  assert.match(TEMPLATE, /id="enclosure-alias-edit-button"/);
  assert.match(TEMPLATE, /aria-label="Edit enclosure name"/);
  assert.match(TEMPLATE, /id="enclosure-alias-form"/);
  assert.match(TEMPLATE, /id="enclosure-alias-input"/);
  assert.match(TEMPLATE, /id="enclosure-alias-clear"/);
  assert.match(TEMPLATE, /id="enclosure-alias-raw-hint"/);
  assert.match(STYLES, /\.enclosure-alias-editor/);
  assert.match(STYLES, /\.enclosure-alias-edit-button:focus-visible/);
  const mobileBoundary = STYLES.indexOf("@media (max-width: 640px)");
  assert.notEqual(mobileBoundary, -1);
  assert.match(STYLES.slice(0, mobileBoundary), /\.enclosure-alias-editor-row\s*\{[^}]*flex-wrap:\s*wrap;/s);
  assert.match(TEMPLATE, /class="meta-card meta-select-card enclosure-select-card"/);
  assert.match(STYLES, /\.enclosure-select-card\s*\{[^}]*flex:\s*1 1 18rem;/s);
});
