"use strict";

// #1203: a sudoers preview request that was in flight when the setup form
// changed must not replace the guidance for the form as it is now. Local
// guidance (bootstrap disabled, ESXi, BMC-only) and payload edits retire every
// earlier request, for both its success and its error render.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.resolve(__dirname, "../../admin_service/static/admin.js"), "utf8");
const BMC_GUIDANCE = "This system is managed through its BMC. No host login is needed.";

// Extract entire declarations, not the condition or message being asserted.
function declaration(name) {
  const start = SOURCE.search(new RegExp(`  (?:async )?function ${name}\\(`));
  assert.notEqual(start, -1, `missing function ${name}`);
  const next = SOURCE.slice(start + 1).search(/\n  (?:async )?function /);
  assert.notEqual(next, -1, `missing end of ${name}`);
  return SOURCE.slice(start, start + 1 + next);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function fixture(platform = "linux") {
  const field = (value = "") => ({ value, checked: true, disabled: false, textContent: "", classList: { toggle() {} } });
  const elements = {
    setupSshEnabled: { checked: true },
    setupBootstrapEnabled: { checked: true },
    setupBootstrapInstallSudo: field(),
    setupBootstrapSudoersPreview: field(),
    setupBootstrapSudoersName: field(),
    setupBootstrapSudoersDetail: field(),
    setupPlatform: field(platform),
    setupSshUser: field("synthetic-service"),
  };
  const requests = [];
  const timers = [];
  const context = vm.createContext({
    elements,
    state: { sudoersPreviewRequestSeq: 0, sudoersPreviewTimerId: null },
    window: {
      setTimeout: (callback) => {
        timers.push(callback);
        return timers.length;
      },
      clearTimeout: () => {},
    },
    currentSetupPlatform: () => elements.setupPlatform.value,
    recommendedSshUserForPlatform: () => "synthetic-service",
    collectBootstrapSudoCommandPayload: () => ({ sudo_commands: ["synthetic-read-only"] }),
    fetchJson: (url, options) => {
      const reply = deferred();
      requests.push({ url, payload: JSON.parse(options.body), reply });
      return reply.promise;
    },
  });
  const names = [
    "setupPlatformUsesBmcOnlyHost",
    "platformSupportsBootstrap",
    "bootstrapEnabledForSession",
    "collectSudoersPreviewPayload",
    "renderSudoersPreview",
    "scheduleSudoersPreviewRefresh",
    "refreshSudoersPreview",
  ];
  vm.runInContext(`const BMC_ONLY_BOOTSTRAP_NOTE = ${JSON.stringify(BMC_GUIDANCE)};\n` + names.map(declaration).join("\n"), context);
  const preview = () => elements.setupBootstrapSudoersPreview.textContent;
  const detail = () => elements.setupBootstrapSudoersDetail.textContent;
  return { context, elements, requests, timers, preview, detail };
}

const OLD_REPLY = { filename: "synthetic-old", detail: "Old enabled preview", content: "OLD-ENABLED-PERMISSIONS" };

// Each case changes the form while an enabled preview is pending, renders the
// local guidance for the new form, then lets the old request finish.
const LOCAL_GUIDANCE_CASES = [
  {
    name: "disabling One-Time Bootstrap",
    change: ({ elements }) => { elements.setupBootstrapEnabled.checked = false; },
    expect: ({ preview, detail }) => {
      assert.match(preview(), /^# One-Time Bootstrap is disabled for this edit session\./);
      assert.match(detail(), /^Enable One-Time Bootstrap/);
    },
  },
  {
    name: "switching to VMware ESXi",
    change: ({ elements }) => { elements.setupPlatform.value = "esxi"; },
    expect: ({ preview, detail }) => {
      assert.match(preview(), /^# VMware ESXi does not use the Linux sudoers\/bootstrap flow\./);
      assert.match(detail(), /^VMware ESXi/);
    },
  },
  {
    name: "switching to a BMC-only system",
    change: ({ elements }) => { elements.setupPlatform.value = "ipmi"; },
    expect: ({ preview, detail }) => {
      assert.equal(preview(), `# ${BMC_GUIDANCE}`);
      assert.equal(detail(), BMC_GUIDANCE);
    },
  },
];

for (const scenario of LOCAL_GUIDANCE_CASES) {
  test(`${scenario.name} keeps its guidance when the older preview succeeds`, async () => {
    const view = fixture();
    const earlier = view.context.refreshSudoersPreview();
    assert.equal(view.requests.length, 1);
    scenario.change(view);
    await view.context.refreshSudoersPreview();
    assert.equal(view.requests.length, 1, "local guidance must not send a request");
    scenario.expect(view);

    view.requests[0].reply.resolve(OLD_REPLY);
    await earlier;
    scenario.expect(view);
    assert.doesNotMatch(view.preview(), /OLD-ENABLED-PERMISSIONS/);
    assert.notEqual(view.elements.setupBootstrapSudoersName.textContent, "synthetic-old");
  });

  test(`${scenario.name} keeps its guidance when the older preview fails`, async () => {
    const view = fixture();
    const earlier = view.context.refreshSudoersPreview();
    scenario.change(view);
    await view.context.refreshSudoersPreview();
    scenario.expect(view);

    view.requests[0].reply.reject(new Error("synthetic old failure"));
    await earlier;
    scenario.expect(view);
    assert.doesNotMatch(view.preview(), /Unable to build|synthetic old failure/);
    assert.doesNotMatch(view.detail(), /Unable to build|synthetic old failure/);
  });
}

test("a pending preview is retired as soon as a form change schedules a new one", async () => {
  const view = fixture();
  const earlier = view.context.refreshSudoersPreview();
  view.elements.setupSshUser.value = "synthetic-renamed";
  view.context.scheduleSudoersPreviewRefresh();
  assert.equal(view.timers.length, 1);

  // The old answer arrives before the debounce timer runs the new request.
  view.requests[0].reply.resolve(OLD_REPLY);
  await earlier;
  assert.equal(view.preview(), "", "the retired answer must not render for the edited form");

  view.timers[0]();
  assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].payload.service_user, "synthetic-renamed");
  view.requests[1].reply.resolve({ filename: "synthetic-new", content: "NEW-PERMISSIONS" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(view.preview(), "NEW-PERMISSIONS");
});

// Controls: the current request still renders, and a newer enabled request
// still wins over an older one that finishes later.
test("control: the current enabled preview renders its success", async () => {
  const view = fixture();
  const current = view.context.refreshSudoersPreview();
  view.requests[0].reply.resolve({ filename: "synthetic-current", detail: "Current", content: "CURRENT-PERMISSIONS" });
  await current;
  assert.equal(view.preview(), "CURRENT-PERMISSIONS");
  assert.equal(view.elements.setupBootstrapSudoersName.textContent, "synthetic-current");
});

test("control: the current enabled preview renders its error", async () => {
  const view = fixture();
  const current = view.context.refreshSudoersPreview();
  view.requests[0].reply.reject(new Error("synthetic current failure"));
  await current;
  assert.match(view.preview(), /^# Unable to build the sudoers preview\n# synthetic current failure/);
});

test("control: a newer enabled preview wins over an older one that finishes later", async () => {
  const view = fixture();
  const earlier = view.context.refreshSudoersPreview();
  const later = view.context.refreshSudoersPreview();
  view.requests[1].reply.resolve({ content: "NEWER-PERMISSIONS" });
  await later;
  view.requests[0].reply.resolve(OLD_REPLY);
  await earlier;
  assert.equal(view.preview(), "NEWER-PERMISSIONS");
});

test("re-enabling bootstrap after a disabled render sends a fresh request that renders", async () => {
  const view = fixture();
  const earlier = view.context.refreshSudoersPreview();
  view.elements.setupBootstrapEnabled.checked = false;
  await view.context.refreshSudoersPreview();
  view.elements.setupBootstrapEnabled.checked = true;
  const current = view.context.refreshSudoersPreview();
  assert.equal(view.requests.length, 2);
  view.requests[0].reply.resolve(OLD_REPLY);
  await earlier;
  assert.match(view.preview(), /One-Time Bootstrap is disabled/);
  view.requests[1].reply.resolve({ content: "RE-ENABLED-PERMISSIONS" });
  await current;
  assert.equal(view.preview(), "RE-ENABLED-PERMISSIONS");
});
