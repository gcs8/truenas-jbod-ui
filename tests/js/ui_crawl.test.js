"use strict";

// Unit tests for the UI coverage crawler's safety, inventory, scanner and
// PNG code (#ui-coverage). No browser.

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const { RequestGuard, forcesRefresh } = require(path.join(root, "qa/ui-crawl/guard"));
const { runState } = require(path.join(root, "qa/ui-crawl/actuation"));
const inventoryModule = require(path.join(root, "qa/ui-crawl/inventory"));
const controls = require(path.join(root, "qa/ui-crawl/controls"));
const png = require(path.join(root, "qa/ui-crawl/png"));
const cli = require(path.join(root, "scripts/ui_crawl"));

const ORIGIN = "http://ui.example.test";
const READ_POSTS = [{ method: "POST", path: "/api/slots/smart-batch" }];

function guard() {
  return new RequestGuard({ origins: [ORIGIN], readPosts: READ_POSTS });
}

test("guard admits reads and aborts foreign origins, writes and forced refreshes", () => {
  const g = guard();
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=a` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=false` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=true` }).allowed, false);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/sas-fabric?refresh=1` }).allowed, false);
  assert.equal(g.admit({ method: "GET", url: "https://elsewhere.example.test/x" }).allowed, false);
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/smart-batch`, body: "{}" }).allowed, true);
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/3/led`, body: '{"action":"IDENTIFY"}' }).allowed, false);
  assert.equal(g.admit({ method: "DELETE", url: `${ORIGIN}/api/slots/3/mapping` }).allowed, false);
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/systems/a/disk-inventory-sync` }).allowed, false);
  assert.equal(g.blocked().length, 6);
});

test("guard admits only declared writes inside the window, within budget", () => {
  const g = guard();
  g.openWrites("led", [
    { method: "POST", path: "/api/slots/*/led", body_includes: "IDENTIFY", max: 1 },
    { method: "POST", path: "/api/slots/*/led", body_includes: "CLEAR", max: 2 },
  ]);
  const led = (action) => g.admit({ method: "POST", url: `${ORIGIN}/api/slots/3/led?system_id=a`, body: JSON.stringify({ action }) });
  assert.equal(led("IDENTIFY").allowed, true);
  assert.equal(led("IDENTIFY").allowed, false, "IDENTIFY budget is one");
  assert.equal(led("CLEAR").allowed, true);
  assert.equal(led("CLEAR").allowed, true);
  assert.equal(led("CLEAR").allowed, false, "CLEAR budget is two");
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/3/mapping`, body: "{}" }).allowed, false, "undeclared write in window");
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/x/y/led`, body: '{"action":"CLEAR"}' }).allowed, false, "glob does not cross /");
  assert.equal(g.closeWrites().length, 3);
  assert.equal(led("CLEAR").allowed, false, "window closed");
  assert.throws(() => { g.openWrites("a", []); g.openWrites("b", []); }, /already open/);
});

test("guard honours body_excludes and declared forced refreshes", () => {
  const g = guard();
  g.openWrites("notes", [
    { method: "POST", path: "/api/slots/*/mapping", body_excludes: "\"clear_identify_after_save\":true", max: 1 },
    { method: "GET", path: "/api/inventory", force: true, max: 1 },
  ]);
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/1/mapping`, body: '{"notes":"x", "clear_identify_after_save": true}' }).allowed, false);
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/1/mapping`, body: '{"notes":"x","clear_identify_after_save":false}' }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=true` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=true` }).allowed, false);
  g.closeWrites();
  assert.equal(forcesRefresh(new URL(`${ORIGIN}/x?force=TRUE`)), true);
  assert.equal(forcesRefresh(new URL(`${ORIGIN}/x?force=0`)), false);
});

// A fake driver: steps are functions of the fake page's network.
function fakeDriver({ failSteps = false, failRevert = 0, failVerify = false, writesOnSteps = 1 } = {}) {
  const g = guard();
  const calls = [];
  let revertFailures = failRevert;
  const driver = {
    guard: g,
    vars: {},
    dialogsSeen: 0,
    acceptNextDialog: false,
    calls,
    async pause() {},
    async runSteps(steps) {
      const label = steps[0] && steps[0].label;
      calls.push(label);
      if (label === "steps") {
        for (let i = 0; i < writesOnSteps; i += 1) g.admit({ method: "POST", url: `${ORIGIN}/api/slots/1/led`, body: '{"action":"IDENTIFY"}' });
        if (failSteps) throw new Error("step failed");
      }
      if (label === "revert") {
        g.admit({ method: "POST", url: `${ORIGIN}/api/slots/1/led`, body: '{"action":"CLEAR"}' });
        if (revertFailures > 0) {
          revertFailures -= 1;
          throw new Error("revert failed");
        }
      }
      if (label === "verify") {
        assert.equal(g.window, null, "verify runs with the write window closed");
        if (failVerify) throw new Error("still on");
      }
      if (label === "preview") {
        assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/systems/a/disk-inventory-sync` }).allowed, false);
        driver.dialogsSeen += 1;
      }
    },
  };
  return driver;
}

function labelled(label) {
  return [{ wait: 1, label }];
}

const LED_STATE = {
  id: "main.led", safety: "led", steps: labelled("steps"), revert: labelled("revert"), verify: labelled("verify"),
  writes: [
    { method: "POST", path: "/api/slots/*/led", body_includes: "IDENTIFY", max: 1 },
    { method: "POST", path: "/api/slots/*/led", body_includes: "CLEAR", max: 3 },
  ],
};

test("led and app-write states revert and verify; skipped unless allowed", async () => {
  const skipped = await runState(fakeDriver(), LED_STATE, { allow: [] });
  assert.equal(skipped.status, "skipped");
  const driver = fakeDriver();
  const done = await runState(driver, LED_STATE, { allow: ["led"] });
  assert.equal(done.status, "done");
  assert.deepEqual(driver.calls, ["steps", "revert", "verify"]);
  assert.equal(done.admitted.length, 2);
  assert.equal(driver.guard.window, null);
});

test("a failed step still reverts once anything was written", async () => {
  const driver = fakeDriver({ failSteps: true });
  const outcome = await runState(driver, LED_STATE, { allow: ["led"] });
  assert.deepEqual(driver.calls, ["steps", "revert", "verify"]);
  assert.equal(outcome.status, "failed");
  assert.equal(outcome.verified, true);
});

test("nothing written means no revert", async () => {
  const driver = fakeDriver({ failSteps: true, writesOnSteps: 0 });
  const outcome = await runState(driver, LED_STATE, { allow: ["led"] });
  assert.deepEqual(driver.calls, ["steps"]);
  assert.equal(outcome.status, "failed");
});

test("revert is retried, and a revert or verify failure is loud", async () => {
  const retried = fakeDriver({ failRevert: 1 });
  assert.equal((await runState(retried, LED_STATE, { allow: ["led"] })).status, "done");
  assert.deepEqual(retried.calls, ["steps", "revert", "revert", "verify"]);
  const broken = fakeDriver({ failRevert: 5 });
  assert.equal((await runState(broken, LED_STATE, { allow: ["led"], revertRetries: 1 })).status, "revert-failed");
  const unverified = fakeDriver({ failVerify: true });
  assert.equal((await runState(unverified, LED_STATE, { allow: ["led"] })).status, "revert-failed");
});

test("forbidden states are never actuated, even when allowed", async () => {
  const state = { id: "main.disk-sync", safety: "forbidden", reason: "appliance write", steps: labelled("preview") };
  const driver = fakeDriver();
  assert.equal((await runState(driver, state, { allow: ["forbidden", "led", "app-write-revert"] })).status, "listed");
  assert.deepEqual(driver.calls, []);
  const preview = await runState(driver, { ...state, confirm_preview: true }, { allow: [], confirmPreview: true });
  assert.equal(preview.status, "previewed");
  assert.equal(preview.dialogs, 1);
  assert.equal(driver.acceptNextDialog, false);
});

test("the checked-in inventory is valid", () => {
  const inventory = inventoryModule.loadInventory();
  assert.deepEqual(inventoryModule.validateInventory(inventory), []);
  const states = inventoryModule.expandStates(inventory);
  assert.ok(states.some((state) => state.id === "main.select[empty]"));
  for (const cls of ["read", "app-write-revert", "led", "forbidden"]) assert.ok(states.some((state) => state.safety === cls), cls);
});

test("inventory validation rejects unsafe shapes", () => {
  const base = { pages: { main: {} }, states: [] };
  const check = (state) => inventoryModule.validateInventory({ ...base, states: [{ page: "main", covers: [], ...state }] });
  assert.match(check({ id: "a", safety: "read", writes: [{ method: "POST", path: "/x", max: 1 }] }).join(), /read state cannot declare writes/);
  assert.match(check({ id: "b", safety: "forbidden", steps: [{ click: "#x" }] }).join(), /no steps unless confirm_preview/);
  assert.match(check({ id: "c", safety: "app-write-revert", steps: [{ click: "#x" }] }).join(), /needs a non-empty revert/);
  assert.match(check({ id: "d", safety: "led", steps: [{ click: "#x" }], revert: [{ click: "#y" }], verify: [{ ledOff: true }], writes: [{ method: "POST", path: "/api/slots/*/led", body_includes: "IDENTIFY", max: 1 }] }).join(), /IDENTIFY and CLEAR/);
  assert.match(check({ id: "e", safety: "app-write-revert", steps: [{ click: "#x" }], revert: [{ click: "#y" }], verify: [{ click: "#save" }], writes: [{ method: "POST", path: "/x", max: 1 }] }).join(), /verify must only read/);
  assert.match(check({ id: "f", safety: "nope" }).join(), /safety must be one of/);
});

test("control scanner keys template and script controls", () => {
  const template = `<div id="panel"><button id="go">Go</button><button class="button small" type="submit">Save</button>
    <select data-mode-select>{% if x %}<option>1</option>{% endif %}</select><input type="hidden" name="h"><input name="serial"></div>`;
  const keys = controls.scanTemplate("t.html", template).map((c) => c.key);
  assert.deepEqual(keys, ["#go", 'button"Save"@#panel', "[data-mode-select]", "[name=serial]"]);
  const script = "const b = document.createElement(\"button\");\n b.dataset.fooAction = 'x';\n" +
    "el.innerHTML = `<button type=\"button\" data-row-key=\"${k}\">Row</button>`;\n" +
    "document.addEventListener('click', (e) => e.target.closest('[data-pick-id]'));";
  assert.deepEqual(controls.scanScript("s.js", script).map((c) => c.key).sort(), ["[data-foo-action]", "[data-pick-id]", "[data-row-key]"]);
});

test("coverage report finds uncovered and stale keys", () => {
  const found = [{ key: "#a", tag: "button", file: "f", line: 1 }, { key: "#b", tag: "button", file: "f", line: 2 }];
  const report = controls.coverageReport(found, { states: [{ id: "s", covers: ["#a", "#gone"] }], ignored_controls: [] });
  assert.deepEqual(report.uncovered.map((c) => c.key), ["#b"]);
  assert.deepEqual(report.stale, ["#gone"]);
});

test("png round trip, crop, tiles and diff", () => {
  const width = 300;
  const height = 200;
  const data = Buffer.alloc(width * height * 4);
  for (let i = 0; i < width * height; i += 1) {
    data[i * 4] = i % 251;
    data[i * 4 + 1] = (i * 7) % 253;
    data[i * 4 + 2] = (i * 13) % 255;
    data[i * 4 + 3] = 255;
  }
  const image = { width, height, data };
  const encoded = png.encode(image);
  assert.deepEqual(png.size(encoded), { width, height });
  const decoded = png.decode(encoded);
  assert.ok(decoded.data.equals(data));
  const part = png.crop(decoded, 250, 150, 100, 100);
  assert.equal(part.width, 50);
  assert.equal(part.height, 50);
  const tiles = png.tiles(decoded, 128, 72);
  assert.equal(tiles.length, 3 * 3);
  assert.deepEqual(png.size(tiles[8].png), { width: 300 - 256, height: 200 - 144 });
  assert.equal(png.diff(decoded, decoded).changedFraction, 0);
  const changed = { width, height, data: Buffer.from(data) };
  for (let i = 0; i < width * 20 * 4; i += 4) changed.data[i] = (changed.data[i] + 128) % 256;
  assert.ok(Math.abs(png.diff(changed, decoded).changedFraction - 0.1) < 0.01);
});

test("CLI refuses forbidden and live writes without a write system", () => {
  assert.throws(() => cli.parseArgs(["--ui", ORIGIN, "--out", "x", "--allow", "forbidden"]), /never actuated/);
  assert.throws(() => cli.parseArgs(["--ui", ORIGIN, "--out", "x", "--allow", "led"]), /--write-system/);
  const args = cli.parseArgs(["--ui", ORIGIN, "--out", "x", "--allow", "led", "--write-system", "sys-a"]);
  const states = [
    { id: "r", page: "main", safety: "read" },
    { id: "all", page: "main", safety: "read", scope: "all" },
    { id: "led", page: "main", safety: "led" },
    { id: "w", page: "main", safety: "app-write-revert" },
    { id: "load", page: "main", safety: "read", appliance_load: true },
  ];
  const ids = (target) => cli.statesFor(target, states, args, {}).map((s) => s.id);
  assert.deepEqual(ids({ page: "main", system: "sys-a", scopeIndex: 0 }), ["r", "all", "led"]);
  assert.deepEqual(ids({ page: "main", system: "sys-b", scopeIndex: 0 }), ["r", "all"]);
  assert.deepEqual(ids({ page: "main", system: "sys-a", scopeIndex: 1 }), ["all"]);
});
