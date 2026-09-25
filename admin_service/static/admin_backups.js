// Backups library page for the admin (#398).
//
// Lists the backup copies the server keeps (here and on remote targets) and
// drives verify / download / keep / restore-from-server / back up now / clean
// up against /api/admin/backups*. Vanilla JS, no framework. admin.js mounts it
// with its own request and wording helpers so errors, timeouts and the restore
// confirmation read the same as the rest of the page.
//
// Never render a filesystem path or a credential: the API returns opaque ids,
// and anything free-form (error details, reasons, change subjects) goes
// through scrubText before it reaches the page. The one exception is the
// settings editor (#573), which edits the operator's own target settings
// (host, share, root folder). Even there, credentials stay file-only: the
// server reports each *_file secret as present or missing, never its path or
// contents, and a secret is only ever replaced by naming a file path.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AdminBackupLibrary = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const API_ROOT = "/api/admin/backups";
  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  const CLASSES = ["config", "full"];
  const CLASS_LABELS = { config: "Settings backups", full: "Full backups" };
  const CLASS_SHORT = { config: "Settings", full: "Full" };
  const CLASS_HELP = {
    config: "Everything except the history database. Taken after settings change.",
    full: "Settings plus the history database. Taken on a schedule.",
  };
  const PROVIDER_LABELS = {
    filesystem: "Folder",
    ftp: "FTP",
    sftp: "SFTP",
    smb: "SMB share",
    nfs: "NFS share",
    s3: "S3 bucket",
  };
  const STATE_LABELS = {
    ok: "Ready",
    incomplete: "Incomplete",
    unsupported: "Unsupported",
    missing: "Missing",
  };
  const LONG_TIMEOUT_MS = 15 * 60 * 1000;
  const SENSITIVE_KEY = /path|file|dir|root|password|passphrase|secret|token|credential|key_id|access_key|private|username|user$|host|endpoint|url|share|bucket|export|mount/i;

  // ---------------------------------------------------------------- model --

  function scrubText(value) {
    return String(value ?? "")
      // user:password@ in any URL-ish text
      .replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi, "$1")
      // the path part of a URL (a remote folder is still a path)
      .replace(/([a-z][a-z0-9+.-]*:\/\/[^\s/'"]+)\/[^\s'"),;]*/gi, "$1")
      // absolute POSIX paths with at least two segments, and Windows paths
      .replace(/(^|[\s'"(=:])(?:\/[^\s'"():,;/]+){2,}\/?/g, "$1[path]")
      .replace(/\b[A-Za-z]:\\[^\s'"]+/g, "[path]")
      .replace(/\\\\[^\s'"]+/g, "[path]")
      .trim();
  }

  function validId(value) {
    return typeof value === "string" && ID_PATTERN.test(value);
  }

  function artifactUrl(id, suffix = "") {
    if (!validId(id)) {
      throw new Error("That backup id is not valid.");
    }
    return `${API_ROOT}/${encodeURIComponent(id)}${suffix}`;
  }

  function targetUrl(id) {
    if (!validId(id)) {
      throw new Error("That target id is not valid.");
    }
    return `${API_ROOT}/targets/${encodeURIComponent(id)}/test`;
  }

  function providerLabel(provider) {
    return PROVIDER_LABELS[String(provider || "")] || scrubText(provider) || "Unknown";
  }

  function locationLabel(location, targets) {
    const key = String(location || "");
    if (key === "local") {
      return "This server";
    }
    const target = (Array.isArray(targets) ? targets : []).find((item) => item && item.id === key);
    return scrubText(target?.label || key) || "Unknown";
  }

  function isRestorable(artifact) {
    return Boolean(artifact) && artifact.restorable === true && artifact.state === "ok";
  }

  function canDownload(artifact) {
    return Boolean(artifact) && artifact.state !== "missing";
  }

  function stateLabel(artifact) {
    const label = STATE_LABELS[artifact?.state] || "Unknown";
    if (artifact?.state === "ok" && artifact.restorable !== true) {
      return "Can't restore";
    }
    return artifact?.state === "ok" ? label : `${label}, can't restore`;
  }

  function timeValue(value) {
    const parsed = Date.parse(String(value || ""));
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function groupArtifacts(artifacts) {
    const groups = { config: [], full: [] };
    (Array.isArray(artifacts) ? artifacts : []).forEach((artifact) => {
      if (artifact && groups[artifact.backup_class]) {
        groups[artifact.backup_class].push(artifact);
      }
    });
    CLASSES.forEach((backupClass) => {
      groups[backupClass].sort((a, b) => timeValue(b.created_at) - timeValue(a.created_at)
        || String(b.id).localeCompare(String(a.id)));
    });
    return groups;
  }

  function humanKey(key) {
    const labels = {
      enabled: "On",
      schedule: "Schedule",
      trigger: "Taken",
      keep_count: "Copies kept",
      local_keep: "Copies kept here",
      remote_keep: "Copies kept on targets",
      remote_max_age_days: "Oldest copy kept on targets (days)",
      max_delay_seconds: "Longest wait after a change (seconds)",
      pending_changes: "Changes waiting for a backup",
      next_run_at: "Next run",
      local_keep_count: "Copies kept here",
      remote_keep_count: "Copies kept on targets",
      max_age: "Oldest copy kept",
      remote_max_age: "Oldest copy kept on targets",
      max_age_days: "Oldest copy kept (days)",
      targets: "Sent to",
      encrypt: "Encrypted",
      encrypted: "Encrypted",
      packaging: "File format",
      debounce_seconds: "Waits after a change (seconds)",
      retention: "Keeps",
    };
    return labels[key] || String(key).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  }

  function looksLikePath(value) {
    return typeof value === "string" && (/^(?:\/|~|[A-Za-z]:\\|\\\\)/.test(value.trim()) || /[a-z]+:\/\/[^\s]*@/i.test(value));
  }

  function describeScalar(value) {
    if (value === true) return "Yes";
    if (value === false) return "No";
    if (value === null || value === undefined || value === "") return "Not set";
    return scrubText(value);
  }

  // Read-only policy summary. Unknown keys render generically, but anything
  // that could name a path or credential is dropped rather than shown.
  function describePolicy(policy, targets, formatTime = (value) => String(value)) {
    const rows = [];
    if (!policy || typeof policy !== "object") {
      return rows;
    }
    Object.keys(policy).forEach((key) => {
      if (SENSITIVE_KEY.test(key)) {
        return;
      }
      const value = policy[key];
      if (looksLikePath(value)) {
        return;
      }
      if (key === "enabled") {
        rows.push(["Turned on", value ? "Yes" : "No"]);
        return;
      }
      if (key === "last_run") {
        const run = lastRunSummary(value);
        rows.push(["Last backup", run.at ? `${run.text} (${formatTime(run.at)})` : run.text]);
        return;
      }
      if (key === "next_run_at" || key.endsWith("_at")) {
        rows.push([humanKey(key), value ? formatTime(value) : "Not scheduled"]);
        return;
      }
      if (value === null && /keep|max_age/.test(key)) {
        rows.push([humanKey(key), "No limit"]);
        return;
      }
      if (key === "targets" && Array.isArray(value)) {
        rows.push([humanKey(key), value.length
          ? value.map((id) => locationLabel(id, targets)).join(", ")
          : "This server only"]);
        return;
      }
      if (Array.isArray(value)) {
        const items = value.filter((item) => item === null || typeof item !== "object").filter((item) => !looksLikePath(item));
        rows.push([humanKey(key), items.length ? items.map(describeScalar).join(", ") : "None"]);
        return;
      }
      if (value && typeof value === "object") {
        Object.keys(value).forEach((subKey) => {
          const subValue = value[subKey];
          if (SENSITIVE_KEY.test(subKey) || looksLikePath(subValue)) {
            return;
          }
          if (subValue && typeof subValue === "object") {
            const parts = Object.keys(subValue)
              .filter((k) => !SENSITIVE_KEY.test(k) && (subValue[k] === null || typeof subValue[k] !== "object") && !looksLikePath(subValue[k]))
              .map((k) => `${humanKey(k).toLowerCase()} ${describeScalar(subValue[k])}`);
            if (parts.length) {
              rows.push([`${humanKey(key)}: ${locationLabel(subKey, targets)}`, parts.join(", ")]);
            }
            return;
          }
          rows.push([`${humanKey(key)}: ${humanKey(subKey).toLowerCase()}`, describeScalar(subValue)]);
        });
        return;
      }
      rows.push([humanKey(key), describeScalar(value)]);
    });
    return rows;
  }

  function humanizePlanReason(reason) {
    const text = scrubText(reason);
    return text
      .replace(/^(retention|unverified):\s*/i, "")
      .replace(/beyond keep_count (\d+) \(newest #(\d+)\)/gi, (_m, keep, rank) => `only ${keep} copies are kept and this is number ${rank}`)
      .replace(/older than max_age (\S+) \(age (\S+)\)/gi, (_m, max, age) => `older than ${describeSpan(max)} (it is ${describeSpan(age)} old)`)
      .replace(/unverified for longer than grace (\S+) \(age (\S+)\)/gi, (_m, grace, age) => `never verified, and older than ${describeSpan(grace)} (it is ${describeSpan(age)} old)`)
      .replace(/;\s*/g, "; ");
  }

  function describeSpan(token) {
    const match = String(token || "").match(/^(\d+)([dhs])$/);
    if (!match) return String(token || "");
    const count = Number(match[1]);
    const unit = { d: "day", h: "hour", s: "second" }[match[2]];
    return `${count} ${unit}${count === 1 ? "" : "s"}`;
  }

  function normalizePlan(payload) {
    const items = Array.isArray(payload)
      ? payload
      : Array.isArray(payload?.items) ? payload.items
        : Array.isArray(payload?.plan) ? payload.plan : [];
    const token = typeof payload?.plan_token === "string" ? payload.plan_token : "";
    const guarded = Array.isArray(payload?.guarded) ? payload.guarded.filter((item) => item && validId(item.id)) : [];
    return {
      token,
      guarded: guarded.length,
      items: items.filter((item) => item && validId(item.id)).map((item) => ({
        id: item.id,
        location: String(item.location || ""),
        reason: String(item.reason || ""),
      })),
    };
  }

  function lastRunSummary(lastRun) {
    if (!lastRun || typeof lastRun !== "object") {
      return { tone: "", text: "Not used yet" };
    }
    return {
      tone: lastRun.ok ? "is-ok" : "is-bad",
      text: lastRun.ok ? "Worked" : `Failed${lastRun.detail ? `: ${scrubText(lastRun.detail)}` : ""}`,
      at: lastRun.at || "",
    };
  }

  function shortHash(value) {
    const text = String(value || "");
    return /^[0-9a-f]{64}$/i.test(text) ? `${text.slice(0, 12)}…` : "";
  }

  const SECRET_LABELS = {
    password_file: "Password file",
    private_key_file: "Private key file",
    private_key_passphrase_file: "Private key passphrase file",
    access_key_id_file: "Access key id file",
    secret_access_key_file: "Secret access key file",
  };
  const BOOLEAN_TARGET_FIELDS = new Set(["use_tls", "trust_on_first_use", "smb_encrypt"]);
  const NUMBER_TARGET_FIELDS = new Set(["port", "timeout_seconds"]);
  const NULLABLE_CLASS_FIELDS = new Set(["remote_keep", "remote_max_age_days"]);

  function secretStateText(secret) {
    if (!secret || !secret.configured) return "Not set";
    if (secret.present === true) return "Secret file present";
    if (secret.present === false) return "Secret file missing or not private (chmod 600)";
    return "Set (file is outside the shared secrets folder, so it can't be checked here)";
  }

  // Build the PUT body from the editor view plus the operator's edits. Locked
  // (environment) values and untouched secrets are left out, so the server
  // keeps them as they are.
  function buildPolicyPayload(view, edits) {
    const payload = { revision: view.revision, classes: {} };
    ["config", "full"].forEach((backupClass) => {
      const locked = view.classes?.[backupClass]?.locked || {};
      const values = edits.classes?.[backupClass] || {};
      const out = {};
      Object.keys(values).forEach((key) => {
        if (!Object.prototype.hasOwnProperty.call(locked, key)) out[key] = values[key];
      });
      payload.classes[backupClass] = out;
    });
    if (!view.targets_locked_by && Array.isArray(edits.targets)) {
      payload.targets = edits.targets.map((target) => {
        const secrets = {};
        Object.keys(target.secretChanges || {}).forEach((name) => {
          const change = target.secretChanges[name];
          if (change && change.clear) secrets[name] = null;
          else if (change && typeof change.path === "string" && change.path.trim()) secrets[name] = change.path.trim();
        });
        return { values: { ...target.values }, secrets };
      });
    }
    return payload;
  }

  const model = {
    buildPolicyPayload,
    secretStateText,
    scrubText,
    validId,
    artifactUrl,
    targetUrl,
    providerLabel,
    locationLabel,
    isRestorable,
    canDownload,
    stateLabel,
    groupArtifacts,
    describePolicy,
    humanizePlanReason,
    normalizePlan,
    lastRunSummary,
  };

  // ------------------------------------------------------------ controller --

  function createBackupLibrary(deps) {
    const doc = deps.document;
    const els = deps.elements || {};
    const fmtBytes = deps.formatBytes || ((value) => `${value} B`);
    const fmtTime = deps.formatLocalTimestamp || ((value) => String(value || "-"));
    const setBanner = deps.setBanner || (() => {});
    const confirmFn = deps.confirm || (() => false);
    const isStopped = deps.isStopped || (() => false);
    const state = {
      data: null,
      loading: false,
      loadError: "",
      pending: new Map(),
      targetResults: new Map(),
      dialogReturnFocus: null,
      loadPromise: null,
    };

    function el(tag, attrs = {}, ...children) {
      const node = doc.createElement(tag);
      Object.keys(attrs).forEach((key) => {
        const value = attrs[key];
        if (value === undefined || value === null || value === false) return;
        if (key === "className") node.className = value;
        else if (key === "text") node.textContent = String(value);
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key === "disabled") node.disabled = Boolean(value);
        else node.setAttribute(key, value === true ? "" : String(value));
      });
      children.flat(Infinity).forEach((child) => {
        if (child === null || child === undefined || child === false) return;
        node.append(typeof child === "string" ? doc.createTextNode(child) : child);
      });
      return node;
    }

    function button(label, { action, id, cls = "secondary small", title, disabled, extra = {} } = {}) {
      return el("button", {
        type: "button",
        className: `button ${cls}`,
        text: label,
        title,
        disabled: disabled || isStopped(),
        dataset: { backupAction: action, backupId: id || "", ...extra },
      });
    }

    // False when the backup service is not deployed or not reachable; the
    // list is then empty and nothing can be run.
    function available() {
      return Boolean(state.data) && state.data.available !== false;
    }

    function targets() {
      return Array.isArray(state.data?.targets) ? state.data.targets : [];
    }

    function artifacts() {
      return Array.isArray(state.data?.artifacts) ? state.data.artifacts : [];
    }

    function findArtifact(id) {
      return artifacts().find((item) => item && item.id === id) || null;
    }

    function setStatus(text) {
      if (els.status) els.status.textContent = text;
    }

    // One in-flight request per key; a second click returns the same promise.
    function once(key, work) {
      if (state.pending.has(key)) return state.pending.get(key);
      const promise = Promise.resolve().then(work).finally(() => {
        state.pending.delete(key);
        render();
      });
      state.pending.set(key, promise);
      render();
      return promise;
    }

    function isPending(key) {
      return state.pending.has(key);
    }

    function errorText(error) {
      // The admin answers 503 for backup actions when the backup scheduler
      // service isn't deployed; say that plainly instead of a bare status.
      if (error?.status === 503) {
        return "The backup scheduler isn't running on this server.";
      }
      return scrubText(error?.message || String(error));
    }

    // ------------------------------------------------------------- load --

    function load({ quiet = false } = {}) {
      if (state.loadPromise) return state.loadPromise;
      state.loading = true;
      if (!quiet) setStatus("Loading backups...");
      state.loadPromise = (async () => {
        try {
          const payload = await deps.fetchJson(API_ROOT);
          state.data = {
            classes: payload.classes && typeof payload.classes === "object" ? payload.classes : {},
            targets: Array.isArray(payload.targets) ? payload.targets : [],
            artifacts: Array.isArray(payload.artifacts) ? payload.artifacts : [],
            storage: payload.storage && typeof payload.storage === "object" ? payload.storage : {},
            available: payload.available !== false,
            detail: typeof payload.detail === "string" ? payload.detail : "",
            running: payload.running && CLASSES.includes(payload.running.backup_class) ? payload.running : null,
          };
          state.loadError = "";
          const count = state.data.artifacts.length;
          const parts = [];
          if (!state.data.available) {
            parts.push(state.data.detail ? `Backups aren't available: ${scrubText(state.data.detail)}` : "Backups aren't set up on this server.");
          } else {
            parts.push(count ? `${count} backup ${count === 1 ? "copy" : "copies"}.` : "No backups yet.");
          }
          if (state.data.running) {
            parts.push(`A ${CLASS_SHORT[state.data.running.backup_class].toLowerCase()} backup is running.`);
          }
          setStatus(parts.join(" "));
          scheduleRunningPoll();
        } catch (error) {
          state.loadError = error?.status === 404
            ? "This admin version has no backup list."
            : `Couldn't load backups: ${errorText(error)}`;
          setStatus(state.loadError);
        } finally {
          state.loading = false;
          state.loadPromise = null;
          render();
        }
      })();
      return state.loadPromise;
    }

    // While a backup runs, re-read the list now and then so the result shows
    // without a manual reload. Stops once nothing is running.
    const POLL_MS = deps.pollMs || 5000;
    function scheduleRunningPoll() {
      if (state.pollTimer || !state.data?.running || isStopped() || !deps.setTimeout) return;
      state.pollTimer = deps.setTimeout(() => {
        state.pollTimer = null;
        if (deps.isVisible && !deps.isVisible()) return;
        void load({ quiet: true });
      }, POLL_MS);
    }

    // ----------------------------------------------------------- render --

    function render() {
      renderPolicies();
      renderTargets();
      renderStorage();
      renderArtifacts();
      if (els.refreshButton) els.refreshButton.disabled = state.loading || isStopped();
      if (els.cleanupButton) els.cleanupButton.disabled = !available() || isPending("cleanup") || isStopped();
      if (els.editButton) els.editButton.disabled = isStopped();
    }

    function renderPolicies() {
      if (!els.policies) return;
      const classes = state.data?.classes || {};
      const cards = CLASSES.map((backupClass) => {
        const policy = classes[backupClass];
        const rows = describePolicy(policy, targets(), fmtTime);
        const list = rows.length
          ? el("dl", { className: "backup-policy-list" }, rows.map(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })]))
          : el("p", { className: "subtle", text: policy ? "No settings." : "Not set up." });
        const runKey = `run:${backupClass}`;
        const running = isPending(runKey) || state.data?.running?.backup_class === backupClass;
        return el("section", { className: "preview-card backup-policy-card", "aria-labelledby": `backup-policy-${backupClass}` },
          el("div", { className: "preview-card-header" },
            el("h3", { id: `backup-policy-${backupClass}`, text: CLASS_LABELS[backupClass] }),
            button(running ? "Backing up..." : "Back up now", {
              action: "run",
              extra: { backupClass },
              cls: "small",
              disabled: !available() || Boolean(state.data?.running) || isPending(runKey),
            })),
          el("p", { className: "subtle action-note", text: CLASS_HELP[backupClass] }),
          list);
      });
      els.policies.replaceChildren(...cards);
    }

    function renderTargets() {
      if (!els.targets) return;
      const list = targets();
      if (!state.data) {
        els.targets.replaceChildren();
        return;
      }
      if (!list.length) {
        els.targets.replaceChildren(el("p", { className: "subtle", text: "No remote targets. Backups stay on this server." }));
        return;
      }
      const rows = list.map((target) => {
        const lastRun = lastRunSummary(target.last_run);
        const tested = state.targetResults.get(target.id);
        const testKey = `test:${target.id}`;
        return el("tr", { dataset: { targetId: target.id } },
          el("th", { scope: "row", text: scrubText(target.label || target.id) }),
          el("td", {},
            providerLabel(target.provider),
            target.transport_encrypted === false ? [" ", el("span", { className: "badge backup-plain-badge", text: "Unencrypted" })] : null),
          el("td", { text: target.enabled === false ? "Off" : "On" }),
          el("td", {},
            el("span", { className: `backup-run-status ${lastRun.tone}`, text: lastRun.text }),
            lastRun.at ? el("span", { className: "subtle", text: ` (${fmtTime(lastRun.at)})` }) : null),
          el("td", {},
            button(isPending(testKey) ? "Testing..." : "Test", { action: "test-target", id: target.id, disabled: isPending(testKey) }),
            tested ? el("span", { className: `backup-test-result ${tested.ok ? "is-ok" : "is-bad"}`, role: "status", text: ` ${tested.text}` }) : null));
      });
      els.targets.replaceChildren(el("table", { className: "backup-table" },
        el("caption", { className: "visually-hidden", text: "Backup targets" }),
        el("thead", {}, el("tr", {},
          ["Target", "Type", "Status", "Last run", ""].map((label) => el("th", { scope: "col", text: label })))),
        el("tbody", {}, rows)));
    }

    function renderStorage() {
      if (!els.storage) return;
      const storage = state.data?.storage || {};
      const keys = Object.keys(storage).filter((key) => validId(key));
      if (!keys.length) {
        els.storage.replaceChildren(state.data ? el("p", { className: "subtle", text: "Nothing stored yet." }) : "");
        return;
      }
      keys.sort((a, b) => (a === "local" ? -1 : b === "local" ? 1 : a.localeCompare(b)));
      els.storage.replaceChildren(el("table", { className: "backup-table" },
        el("caption", { className: "visually-hidden", text: "Space used per location" }),
        el("thead", {}, el("tr", {}, ["Location", "Settings", "Full", "Copies"].map((label) => el("th", { scope: "col", text: label })))),
        el("tbody", {}, keys.map((key) => {
          const usage = storage[key] || {};
          return el("tr", {},
            el("th", { scope: "row", text: locationLabel(key, targets()) }),
            el("td", { text: fmtBytes(usage.config_bytes) }),
            el("td", { text: fmtBytes(usage.full_bytes) }),
            el("td", { text: String(Number(usage.count) || 0) }));
        }))));
    }

    function renderArtifacts() {
      if (!els.artifacts) return;
      if (!state.data) {
        els.artifacts.replaceChildren(state.loadError ? el("p", { className: "subtle", text: state.loadError }) : "");
        return;
      }
      const groups = groupArtifacts(artifacts());
      els.artifacts.replaceChildren(...CLASSES.map((backupClass) => {
        const rows = groups[backupClass];
        const headingId = `backup-artifacts-${backupClass}`;
        const body = rows.length
          ? el("table", { className: "backup-table backup-artifact-table", "aria-labelledby": headingId },
            el("thead", {}, el("tr", {},
              ["Date", "Size", "Location", "Verified", "State", "Kept", "Actions"].map((label) => el("th", { scope: "col", text: label })))),
            el("tbody", {}, rows.map(renderArtifactRow)))
          : el("p", { className: "subtle", text: "None yet." });
        return el("section", { className: "backup-artifact-group", dataset: { backupClass } },
          el("h3", { id: headingId, text: `${CLASS_LABELS[backupClass]} (${rows.length})` }),
          body);
      }));
    }

    function renderArtifactRow(artifact) {
      const restorable = isRestorable(artifact);
      const id = validId(artifact.id) ? artifact.id : "";
      const busy = (key) => isPending(`${key}:${id}`);
      const kept = artifact.preserved
        ? el("span", { className: "badge backup-kept-badge", title: artifact.preserve_reason ? scrubText(artifact.preserve_reason) : undefined, text: "Kept" },
        )
        : null;
      const keptReason = artifact.preserved && artifact.preserve_reason
        ? el("span", { className: "subtle backup-kept-reason", text: ` ${scrubText(artifact.preserve_reason)}` })
        : null;
      const actions = el("div", { className: "backup-row-actions" },
        button("Details", { action: "details", id, disabled: !id }),
        button(busy("verify") ? "Verifying..." : "Verify", { action: "verify", id, disabled: !id || busy("verify") || artifact.state === "missing" }),
        canDownload(artifact) && id
          ? el("a", { className: "button secondary small", href: artifactUrl(id, "/download"), download: "", dataset: { backupAction: "download", backupId: id } }, "Download")
          : null,
        artifact.preserved
          ? button("Stop keeping", { action: "unpreserve", id, disabled: !id || busy("preserve") })
          : button("Keep", { action: "preserve", id, disabled: !id || busy("preserve"), title: "Keep this copy when cleaning up" }),
        button("Restore", {
          action: "restore",
          id,
          cls: "danger small",
          disabled: !id || !restorable,
          title: restorable ? undefined : "This copy can't be restored",
        }));
      return el("tr", { dataset: { artifactId: id, restorable: restorable ? "true" : "false" }, className: restorable ? "" : "is-not-restorable" },
        el("td", { text: fmtTime(artifact.created_at) }),
        el("td", { text: fmtBytes(artifact.size) }),
        el("td", { text: locationLabel(artifact.location, targets()) }),
        el("td", { text: artifact.verified ? "Yes" : "Not yet" }),
        el("td", { className: `backup-state is-${STATE_LABELS[artifact.state] ? artifact.state : "unknown"}`, text: stateLabel(artifact) }),
        el("td", {}, kept, keptReason),
        el("td", {}, actions));
    }

    // ---------------------------------------------------------- dialogs --

    function openDialog(title, bodyNodes, footerNodes, trigger) {
      const dialog = els.dialog;
      if (!dialog) return null;
      state.dialogGeneration = (state.dialogGeneration || 0) + 1;
      state.dialogReturnFocus = trigger || doc.activeElement || null;
      const titleId = "backup-dialog-title";
      dialog.setAttribute("aria-labelledby", titleId);
      dialog.replaceChildren(
        el("h2", { id: titleId, text: title }),
        el("div", { className: "backup-dialog-body" }, bodyNodes),
        el("p", { className: "subtle action-note backup-dialog-result", role: "status", "aria-live": "polite" }),
        el("div", { className: "button-row backup-dialog-actions" }, footerNodes));
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      }
      const focusTarget = dialog.querySelector("input, textarea, [data-dialog-primary]") || dialog.querySelector("button");
      focusTarget?.focus?.();
      return dialog;
    }

    function dialogResult(text) {
      const node = els.dialog?.querySelector(".backup-dialog-result");
      if (node) node.textContent = text;
      return node;
    }

    function setDialogActions(...nodes) {
      const row = els.dialog?.querySelector(".backup-dialog-actions");
      row?.replaceChildren(...nodes);
      (row?.querySelector("[data-dialog-primary]") || row?.querySelector("button"))?.focus?.();
    }

    // A slow response must only touch the dialog that asked for it. Each open
    // or close starts a new generation; a scope from an older one is inert.
    function dialogScope() {
      const generation = state.dialogGeneration || 0;
      const live = () => generation === (state.dialogGeneration || 0) && Boolean(els.dialog?.open);
      return {
        live,
        result: (text) => (live() ? dialogResult(text) : null),
        actions: (...nodes) => {
          if (live()) setDialogActions(...nodes);
        },
        body: () => (live() ? els.dialog.querySelector(".backup-dialog-body") : null),
      };
    }

    function closeDialog() {
      const dialog = els.dialog;
      if (!dialog) return;
      state.dialogGeneration = (state.dialogGeneration || 0) + 1;
      dialog.querySelectorAll("input[type=password]").forEach((input) => {
        input.value = "";
      });
      if (dialog.open) {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.removeAttribute("open");
      }
      dialog.replaceChildren();
      const target = state.dialogReturnFocus;
      state.dialogReturnFocus = null;
      if (target && target.isConnected !== false && typeof target.focus === "function") {
        target.focus();
      } else {
        els.heading?.focus?.();
      }
    }

    function cancelButton(label = "Cancel") {
      const node = button(label, { action: "dialog-cancel", cls: "ghost" });
      node.disabled = false;
      return node;
    }

    function primary(label, action, cls = "") {
      const node = button(label, { action, cls: cls || "" });
      node.dataset.dialogPrimary = "true";
      return node;
    }

    function describeArtifactLine(artifact) {
      return `${CLASS_SHORT[artifact.backup_class] || "Backup"} backup from ${fmtTime(artifact.created_at)}, ${fmtBytes(artifact.size)}, on ${locationLabel(artifact.location, targets())}.`;
    }

    // ---------------------------------------------------------- editor --

    const POLICY_URL = `${API_ROOT}/policy`;

    function inputFor(id, value, { type = "text", disabled = false, placeholder } = {}) {
      const input = el("input", { id, type, autocomplete: "off", disabled, placeholder });
      if (type === "checkbox") input.checked = Boolean(value);
      else input.value = value === null || value === undefined ? "" : String(value);
      return input;
    }

    function fieldLabel(id, text, input, note) {
      return el("label", { className: input.type === "checkbox" ? "toggle" : "field", for: id },
        input.type === "checkbox" ? [input, el("span", { text })] : [el("span", { text }), input],
        note ? el("span", { className: "subtle", text: note }) : null);
    }

    function classFieldset(view, backupClass) {
      const entry = view.classes?.[backupClass] || { values: {}, locked: {} };
      const rows = Object.keys(entry.values).map((key) => {
        const id = `backup-edit-${backupClass}-${key}`;
        const lockedBy = entry.locked?.[key];
        const value = entry.values[key];
        const type = typeof value === "boolean" ? "checkbox" : (key === "schedule" ? "text" : "number");
        const input = inputFor(id, value, { type, disabled: Boolean(lockedBy), placeholder: NULLABLE_CLASS_FIELDS.has(key) ? "No limit" : undefined });
        input.dataset.policyClass = backupClass;
        input.dataset.policyKey = key;
        return fieldLabel(id, key === "enabled" ? "Turned on" : humanKey(key), input, lockedBy ? `Set by ${lockedBy} in the environment; change it there.` : "");
      });
      return el("fieldset", { className: "backup-edit-class" }, el("legend", { text: CLASS_LABELS[backupClass] }), rows);
    }

    function targetFieldset(view, target, index) {
      const locked = Boolean(view.targets_locked_by);
      const values = target.values || {};
      const prefix = `backup-edit-target-${index}`;
      const plain = ["target_id", "label", "enabled", ...view.target_fields.filter((key) => key !== "target_id")];
      const rows = plain.map((key) => {
        const id = `${prefix}-${key}`;
        let input;
        if (key === "provider") {
          input = el("select", { id, disabled: locked }, view.providers.map((provider) => {
            const option = el("option", { value: provider, text: providerLabel(provider) });
            if (provider === (values.provider || "filesystem")) option.setAttribute("selected", "");
            return option;
          }));
          input.value = values.provider || "filesystem";
        } else {
          const type = key === "enabled" || BOOLEAN_TARGET_FIELDS.has(key) ? "checkbox" : (NUMBER_TARGET_FIELDS.has(key) ? "number" : "text");
          input = inputFor(id, key === "enabled" ? values.enabled !== false : values[key], { type, disabled: locked });
        }
        input.dataset.targetIndex = String(index);
        input.dataset.targetKey = key;
        return fieldLabel(id, key === "enabled" ? "Turned on" : humanKey(key), input);
      });
      const secretRows = view.secret_fields.map((name) => {
        const id = `${prefix}-${name}`;
        const input = inputFor(id, "", { disabled: locked, placeholder: "/run/backup-secrets/..." });
        input.dataset.targetIndex = String(index);
        input.dataset.secretKey = name;
        const clear = inputFor(`${id}-clear`, false, { type: "checkbox", disabled: locked || !target.secrets?.[name]?.configured });
        clear.dataset.targetIndex = String(index);
        clear.dataset.secretClear = name;
        return el("div", { className: "backup-edit-secret", dataset: { secret: name } },
          el("p", {}, el("strong", { text: `${SECRET_LABELS[name] || humanKey(name)}: ` }), el("span", { className: "backup-secret-state", text: secretStateText(target.secrets?.[name]) })),
          fieldLabel(id, "Use this secret file instead (path inside the backup scheduler)", input),
          fieldLabel(`${id}-clear`, "Stop using a secret file here", clear));
      });
      return el("fieldset", { className: "backup-edit-target", dataset: { targetIndex: String(index) } },
        el("legend", { text: scrubText(values.label || values.target_id || `Target ${index + 1}`) }),
        rows,
        el("details", {}, el("summary", { text: "Credentials (secret files)" }),
          el("p", { className: "subtle", text: "Secrets are never shown or sent back. Put the secret in a file under config/backup-secrets on the host (mode 600), then name it here." }),
          secretRows),
        locked ? null : button("Remove this target", { action: "policy-remove-target", id: String(index), cls: "ghost small" }));
    }

    function renderEditor(view) {
      const body = [
        el("p", { className: "subtle", text: "Changes are checked, then written to config.yaml. The backup scheduler reads them when it starts." }),
        view.problems?.length ? el("ul", { className: "backup-edit-problems" }, view.problems.map((problem) => el("li", { text: scrubText(problem) }))) : null,
        classFieldset(view, "config"),
        classFieldset(view, "full"),
        el("h3", { text: "Targets" }),
        view.targets_locked_by
          ? el("p", { className: "subtle", text: `Targets are set by ${view.targets_locked_by} in the environment; change them there.` })
          : null,
        el("div", { className: "backup-edit-targets" }, view.targets.map((target, index) => targetFieldset(view, target, index))),
        view.targets_locked_by ? null : button("Add a target", { action: "policy-add-target", cls: "secondary small" }),
      ];
      return body;
    }

    function readEdits(view) {
      const dialog = els.dialog;
      const edits = { classes: { config: {}, full: {} }, targets: view.targets.map((target) => ({ values: {}, secretChanges: {} })) };
      dialog.querySelectorAll("[data-policy-key]").forEach((input) => {
        if (input.disabled) return;
        const key = input.dataset.policyKey;
        let value;
        if (input.type === "checkbox") value = input.checked;
        else if (input.type === "number") value = input.value.trim() === "" ? null : Number(input.value);
        else value = input.value.trim();
        edits.classes[input.dataset.policyClass][key] = value;
      });
      dialog.querySelectorAll("[data-target-key]").forEach((input) => {
        const target = edits.targets[Number(input.dataset.targetIndex)];
        if (!target) return;
        const key = input.dataset.targetKey;
        if (input.type === "checkbox") target.values[key] = input.checked;
        else if (input.type === "number") {
          if (input.value.trim() !== "") target.values[key] = Number(input.value);
        } else if (input.value.trim() !== "") target.values[key] = input.value.trim();
      });
      dialog.querySelectorAll("[data-secret-key]").forEach((input) => {
        const target = edits.targets[Number(input.dataset.targetIndex)];
        if (target && input.value.trim()) target.secretChanges[input.dataset.secretKey] = { path: input.value.trim() };
      });
      dialog.querySelectorAll("[data-secret-clear]").forEach((input) => {
        const target = edits.targets[Number(input.dataset.targetIndex)];
        if (target && input.checked) target.secretChanges[input.dataset.secretClear] = { clear: true };
      });
      return edits;
    }

    async function openPolicyEditor(trigger) {
      openDialog("Backup settings", [el("p", { className: "subtle", text: "Loading..." })], [cancelButton("Close")], trigger);
      const scope = dialogScope();
      try {
        const view = await deps.fetchJson(POLICY_URL);
        state.policyView = view;
        showPolicyView(scope, view);
      } catch (error) {
        scope.result(`Couldn't load the backup settings: ${errorText(error)}`);
      }
    }

    function showPolicyView(scope, view) {
      const body = scope.body();
      if (!body) return;
      body.replaceChildren(...renderEditor(view).filter(Boolean));
      scope.actions(cancelButton(), primary("Save", "policy-save"));
    }

    function editorWithTargets(mutate) {
      const view = state.policyView;
      if (!view) return;
      // Keep what was typed: fold the current form back into the view first.
      const edits = readEdits(view);
      const targets = view.targets.map((target, index) => ({
        ...target,
        values: { ...edits.targets[index].values },
      }));
      state.policyView = { ...view, targets: mutate(targets) };
      showPolicyView(dialogScope(), state.policyView);
    }

    function addPolicyTarget() {
      editorWithTargets((targets) => [...targets, { values: { provider: "filesystem", enabled: true }, secrets: {} }]);
    }

    function removePolicyTarget(index) {
      editorWithTargets((targets) => targets.filter((_target, position) => position !== Number(index)));
    }

    function savePolicy() {
      const view = state.policyView;
      if (!view) return Promise.resolve();
      const scope = dialogScope();
      const payload = buildPolicyPayload(view, readEdits(view));
      return once("policy-save", async () => {
        scope.result("Saving...");
        try {
          const saved = await deps.fetchJson(POLICY_URL, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          state.policyView = saved;
          showPolicyView(scope, saved);
          scope.result(`Saved. Restart the backup scheduler to use the new settings: ${saved.restart_command || "restart enclosure-backup-scheduler"}`);
          setBanner("Backup settings saved. Restart the backup scheduler to apply them.", "success");
        } catch (error) {
          scope.result(error?.status === 409
            ? "config.yaml changed since this editor opened. Close it and open it again."
            : `Not saved: ${errorText(error)}`);
        }
      });
    }

    // --------------------------------------------------------- actions --

    async function showDetails(id, trigger) {
      const artifact = findArtifact(id);
      openDialog("Backup details", [el("p", { className: "subtle", text: "Loading..." })], [cancelButton("Close")], trigger);
      const scope = dialogScope();
      try {
        const payload = await deps.fetchJson(artifactUrl(id));
        const item = payload.artifact && typeof payload.artifact === "object" ? { ...payload.artifact, ...payload } : payload;
        const facts = [
          ["Type", CLASS_LABELS[item.backup_class] || "Unknown"],
          ["Taken", fmtTime(item.created_at)],
          ["Size", fmtBytes(item.size)],
          ["Location", locationLabel(item.location, targets())],
          ["Verified", item.verified ? "Yes" : "Not yet"],
          ["State", stateLabel(item)],
          ["App version", item.app_version ? scrubText(item.app_version) : "Unknown"],
          ["Checksum", shortHash(item.sha256) || "Unknown"],
        ];
        if (item.preserved) {
          facts.push(["Kept", [item.preserve_reason, item.preserved_by ? `by ${item.preserved_by}` : ""].filter(Boolean).map(scrubText).join(" ") || "Yes"]);
        }
        const inspection = item.inspection || item.inspect || null;
        if (inspection && typeof inspection === "object") {
          const mode = inspection.encryption_mode || inspection.encryption;
          if (mode) facts.push(["Encryption", mode === "encrypted" ? "Encrypted" : mode === "plaintext" ? "Not encrypted" : scrubText(mode)]);
          else if (typeof inspection.encrypted === "boolean") facts.push(["Encryption", inspection.encrypted ? "Encrypted" : "Not encrypted"]);
          if (inspection.packaging) facts.push(["File format", scrubText(inspection.packaging)]);
          if (inspection.schema_version !== undefined) facts.push(["Format version", scrubText(inspection.schema_version)]);
          if (inspection.app_version) facts.push(["Made by app version", scrubText(inspection.app_version)]);
          const groups = Array.isArray(inspection.groups) ? inspection.groups : Array.isArray(inspection.included_groups) ? inspection.included_groups : [];
          if (groups.length) facts.push(["Contains", groups.map((group) => scrubText(typeof group === "object" ? group.label || group.key : group)).join(", ")]);
        }
        if (item.last_verify && typeof item.last_verify === "object") {
          const verify = lastRunSummary(item.last_verify);
          facts.push(["Last check", verify.at ? `${verify.text} (${fmtTime(verify.at)})` : verify.text]);
        }
        const body = [
          el("dl", { className: "backup-policy-list" }, facts.map(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })])),
        ];
        const changes = Array.isArray(item.changes) ? item.changes : null;
        if (item.backup_class === "config" || changes) {
          body.push(el("h3", { text: "Changes in this backup" }));
          body.push(changes && changes.length
            ? el("ul", { className: "backup-change-list" }, changes.map((change) => (change.at || change.action || change.subject
              ? el("li", {},
                el("span", { className: "subtle", text: `${fmtTime(change.at)} ` }),
                `${scrubText(change.action || "Changed")}${change.subject ? `: ${scrubText(change.subject)}` : ""}`)
              : el("li", { className: "subtle", text: "A change whose details are no longer kept" }))))
            : el("p", { className: "subtle", text: "No changes recorded." }));
        }
        scope.body()?.replaceChildren(...body);
      } catch (error) {
        scope.result(`Couldn't load details: ${errorText(error)}`);
        if (!artifact) return;
      }
    }

    function verify(id) {
      return once(`verify:${id}`, async () => {
        try {
          const payload = await deps.fetchJson(artifactUrl(id, "/verify"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
            timeoutMs: LONG_TIMEOUT_MS,
          });
          const item = payload.artifact || payload;
          const ok = item.verified !== false && payload.verified !== false;
          setBanner(ok ? "Backup verified." : `Backup did not verify${payload.detail ? `: ${scrubText(payload.detail)}` : "."}`, ok ? "success" : "error");
        } catch (error) {
          setBanner(`Verify failed: ${errorText(error)}`, "error");
        }
        await load({ quiet: true });
      });
    }

    function openPreserve(id, trigger) {
      const artifact = findArtifact(id);
      if (!artifact) return;
      const input = el("input", { id: "backup-preserve-reason", type: "text", maxlength: "200", required: true, autocomplete: "off" });
      openDialog("Keep this backup", [
        el("p", { text: describeArtifactLine(artifact) }),
        el("p", { className: "subtle", text: "Clean up never deletes a kept copy." }),
        el("label", { className: "field", for: "backup-preserve-reason" }, el("span", { text: "Why keep it?" }), input),
      ], [primary("Keep", "preserve-confirm"), cancelButton()], trigger);
      state.dialogArtifactId = id;
    }

    function submitPreserve() {
      const id = state.dialogArtifactId;
      const scope = dialogScope();
      const input = els.dialog?.querySelector("#backup-preserve-reason");
      const reason = String(input?.value || "").trim();
      if (!reason) {
        scope.result("Add a short reason first.");
        input?.focus?.();
        return Promise.resolve();
      }
      return once(`preserve:${id}`, async () => {
        try {
          await deps.fetchJson(artifactUrl(id, "/preserve"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason }),
          });
          if (scope.live()) closeDialog();
          setBanner("Backup kept.", "success");
          await load({ quiet: true });
        } catch (error) {
          scope.result(`Couldn't keep it: ${errorText(error)}`);
        }
      });
    }

    function unpreserve(id) {
      const artifact = findArtifact(id);
      if (!artifact) return Promise.resolve();
      if (!confirmFn(`Stop keeping this backup? Clean up may delete it later.\n\n${describeArtifactLine(artifact)}`)) {
        return Promise.resolve();
      }
      return once(`preserve:${id}`, async () => {
        try {
          await deps.fetchJson(artifactUrl(id, "/preserve"), { method: "DELETE" });
          setBanner("Backup no longer kept.", "success");
        } catch (error) {
          setBanner(`Couldn't change it: ${errorText(error)}`, "error");
        }
        await load({ quiet: true });
      });
    }

    function runNow(backupClass) {
      if (!CLASSES.includes(backupClass)) return Promise.resolve();
      return once(`run:${backupClass}`, async () => {
        try {
          const payload = await deps.fetchJson(`${API_ROOT}/run`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ backup_class: backupClass }),
            timeoutMs: LONG_TIMEOUT_MS,
          });
          setBanner(payload.state === "started"
            ? `${CLASS_SHORT[backupClass]} backup started. This list updates when it finishes.`
            : payload.detail ? scrubText(payload.detail) : `${CLASS_SHORT[backupClass]} backup done.`, "success");
        } catch (error) {
          if (error?.status === 409) {
            setBanner("A backup or clean up is already running. Try again when it finishes.", "info");
          } else {
            setBanner(`Backup failed: ${errorText(error)}`, "error");
          }
        }
        await load({ quiet: true });
      });
    }

    function testTarget(id) {
      return once(`test:${id}`, async () => {
        try {
          const result = await deps.fetchJson(targetUrl(id), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          const ms = Number(result.duration_ms);
          state.targetResults.set(id, result.ok === false
            ? { ok: false, text: `Failed: ${scrubText(result.detail || "no detail")}` }
            : { ok: true, text: `Works${Number.isFinite(ms) ? ` (${ms} ms)` : ""}.` });
        } catch (error) {
          state.targetResults.set(id, { ok: false, text: `Failed: ${errorText(error)}` });
        }
      });
    }

    // Restore reuses the upload path's wording, passphrase header and
    // inspection receipt; only the archive comes from the server, not a file.
    function openRestore(id, trigger) {
      const artifact = findArtifact(id);
      if (!artifact || !isRestorable(artifact)) return;
      state.dialogArtifactId = id;
      state.restoreInspection = null;
      openDialog("Restore from this backup", [
        el("p", { text: describeArtifactLine(artifact) }),
        el("label", { className: "field", for: "backup-restore-passphrase" },
          el("span", { text: "Passphrase" }),
          el("input", { id: "backup-restore-passphrase", type: "password", autocomplete: "off", placeholder: "Only needed for encrypted backups" })),
        el("p", { className: "subtle action-note", text: "The backup is checked first, then you confirm what it replaces. Passphrases keep all spaces." }),
        el("div", { className: "toggle-group" },
          el("label", { className: "toggle" }, el("input", { id: "backup-restore-stop", type: "checkbox", checked: true }), el("span", { text: "Pause the main UI and history while importing (recommended)" })),
          el("label", { className: "toggle" }, el("input", { id: "backup-restore-restart", type: "checkbox", checked: true }), el("span", { text: "Start them again afterwards" }))),
      ], [primary("Check backup", "restore-inspect"), cancelButton()], trigger);
      els.dialog?.querySelectorAll("input[type=checkbox]").forEach((box) => {
        box.checked = true;
      });
    }

    function restoreHeaders() {
      const field = els.dialog?.querySelector("#backup-restore-passphrase");
      const value = field?.value;
      return value === undefined || value === null || value === ""
        ? {}
        : { "X-Backup-Passphrase-Base64": deps.encodeUtf8Base64(value) };
    }

    function restoreInspect() {
      const id = state.dialogArtifactId;
      const scope = dialogScope();
      return once(`restore:${id}`, async () => {
        scope.result("Checking the backup...");
        try {
          const inspection = await deps.fetchJson(artifactUrl(id, "/restore/inspect"), {
            method: "POST",
            headers: restoreHeaders(),
            timeoutMs: LONG_TIMEOUT_MS,
          });
          if (!["encrypted", "plaintext"].includes(inspection?.encryption_mode) || !inspection?.inspection_receipt) {
            throw new Error("The backup check did not finish. Try the restore again.");
          }
          if (!scope.live()) return;
          state.restoreInspection = inspection;
          scope.result("");
          const body = scope.body();
          body?.append(el("p", { className: "backup-restore-confirmation", text: deps.describeBackupRestoreConfirmation(inspection) }));
          scope.actions(primary("Restore", "restore-import", "danger"), cancelButton());
        } catch (error) {
          scope.result(`Check failed: ${errorText(error)}`);
        }
      });
    }

    function restoreImport() {
      const id = state.dialogArtifactId;
      const scope = dialogScope();
      const inspection = state.restoreInspection;
      if (!inspection) return Promise.resolve();
      return once(`restore:${id}`, async () => {
        const stopServices = Boolean(els.dialog?.querySelector("#backup-restore-stop")?.checked);
        const restartServices = stopServices && Boolean(els.dialog?.querySelector("#backup-restore-restart")?.checked);
        scope.result("Restoring...");
        scope.actions(cancelButton("Close"));
        try {
          const payload = await deps.fetchJson(
            `${artifactUrl(id, "/restore/import")}?stop_services=${String(stopServices)}&restart_services=${String(restartServices)}`,
            {
              method: "POST",
              headers: {
                ...restoreHeaders(),
                "X-Backup-Expected-Encryption": inspection.encryption_mode,
                "X-Backup-Inspection-Receipt": inspection.inspection_receipt,
              },
              timeoutMs: LONG_TIMEOUT_MS,
            }
          );
          state.restoreInspection = null;
          const outcome = deps.describeMaintenanceOutcome({
            stopped: payload.stopped_containers,
            restarted: payload.restarted_containers,
            failures: payload.restart_failures && typeof payload.restart_failures === "object"
              ? Object.keys(payload.restart_failures).join(",")
              : "",
          });
          const node = scope.result("");
          if (node) deps.renderMaintenanceResult(node, "Restored", outcome);
          setBanner(outcome.ok ? "Backup restored." : `Backup restored, but ${outcome.sentence}`, outcome.ok ? "success" : "error");
          const passphrase = els.dialog?.querySelector("#backup-restore-passphrase");
          if (passphrase) passphrase.value = "";
          await deps.refreshAdminState?.();
          await load({ quiet: true });
        } catch (error) {
          scope.result(`Restore failed: ${errorText(error)}`);
          setBanner(`Restore failed: ${errorText(error)}`, "error");
        }
      });
    }

    // Clean up shows the server's dry-run plan and applies exactly that plan
    // by its token; nothing is deleted that the operator did not see.
    function openCleanup(trigger) {
      if (isPending("cleanup")) return state.pending.get("cleanup");
      state.cleanupPlan = null;
      openDialog("Clean up old backups", [el("p", { className: "subtle", text: "Working out what would be deleted..." })], [cancelButton()], trigger);
      const scope = dialogScope();
      return once("cleanup", async () => {
        try {
          const plan = normalizePlan(await deps.fetchJson(`${API_ROOT}/lifecycle/plan`));
          if (!scope.live()) return;
          state.cleanupPlan = plan;
          const body = scope.body();
          const guardedNote = plan.guarded
            ? el("p", { className: "subtle", text: `${plan.guarded} more ${plan.guarded === 1 ? "is" : "are"} over a limit but kept, because ${plan.guarded === 1 ? "it is" : "each is"} the newest verified copy in its place.` })
            : null;
          if (!plan.items.length) {
            body?.replaceChildren(el("p", { text: "Nothing to clean up. Every copy is within its limits." }), guardedNote || "");
            scope.actions(cancelButton("Close"));
            return;
          }
          if (!plan.token) {
            throw new Error("The plan came back without a token, so it can't be applied safely.");
          }
          body?.replaceChildren(
            el("p", { text: `These ${plan.items.length} ${plan.items.length === 1 ? "copy" : "copies"} will be deleted. Kept copies and the newest verified copy in each place are never deleted.` }),
            el("ul", { className: "backup-change-list backup-cleanup-list" }, plan.items.map((item) => {
              const artifact = findArtifact(item.id);
              const what = artifact
                ? `${CLASS_SHORT[artifact.backup_class] || "Backup"} from ${fmtTime(artifact.created_at)} (${fmtBytes(artifact.size)})`
                : "Backup";
              return el("li", { dataset: { artifactId: item.id } },
                `${what} on ${locationLabel(item.location || artifact?.location, targets())}`,
                item.reason ? el("span", { className: "subtle", text: `: ${humanizePlanReason(item.reason)}` }) : null);
            })),
            guardedNote || "");
          scope.actions(primary(`Delete ${plan.items.length}`, "cleanup-apply", "danger"), cancelButton());
        } catch (error) {
          scope.result(`Couldn't work out the plan: ${errorText(error)}`);
        }
      });
    }

    function applyCleanup() {
      const plan = state.cleanupPlan;
      const scope = dialogScope();
      if (!plan || !plan.token) return Promise.resolve();
      return once("cleanup", async () => {
        scope.result("Deleting...");
        scope.actions(cancelButton("Close"));
        try {
          const payload = await deps.fetchJson(`${API_ROOT}/lifecycle/apply`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ plan_token: plan.token }),
            timeoutMs: LONG_TIMEOUT_MS,
          });
          state.cleanupPlan = null;
          const count = (value) => (Array.isArray(value) ? value.length : 0);
          const deleted = count(payload.deleted) + count(payload.already_missing);
          const stopped = Boolean(payload.failed) || count(payload.not_attempted) > 0 || payload.complete === false;
          const failedDetail = payload.failed?.error || payload.error;
          const text = stopped
            ? `Clean up stopped after ${deleted} of ${deleted + count(payload.not_attempted) + (payload.failed ? 1 : 0)}${failedDetail ? `: ${scrubText(failedDetail)}` : "."}`
            : `Deleted ${deleted} ${deleted === 1 ? "copy" : "copies"}.`;
          scope.result(text);
          setBanner(text, stopped ? "error" : "success");
        } catch (error) {
          state.cleanupPlan = null;
          const text = error?.status === 409
            ? "The list changed since this plan was made, so nothing was deleted. Open Clean up again."
            : `Clean up failed: ${errorText(error)} Some copies may already be gone; the list has been reloaded.`;
          scope.result(text);
          setBanner(text, "error");
        }
        await load({ quiet: true });
      });
    }

    // -------------------------------------------------------- binding --

    function handleClick(event) {
      const target = event.target?.closest?.("[data-backup-action]");
      if (!target || target.disabled) return;
      const action = target.dataset.backupAction;
      const id = target.dataset.backupId || "";
      switch (action) {
        case "download":
          return; // plain link; the browser streams the file
        case "details":
          void showDetails(id, target);
          break;
        case "verify":
          void verify(id);
          break;
        case "preserve":
          openPreserve(id, target);
          break;
        case "unpreserve":
          void unpreserve(id);
          break;
        case "restore":
          openRestore(id, target);
          break;
        case "run":
          void runNow(target.dataset.backupClass);
          break;
        case "test-target":
          void testTarget(id);
          break;
        case "preserve-confirm":
          void submitPreserve();
          break;
        case "restore-inspect":
          void restoreInspect();
          break;
        case "restore-import":
          void restoreImport();
          break;
        case "cleanup-apply":
          void applyCleanup();
          break;
        case "dialog-cancel":
          closeDialog();
          break;
        case "policy-add-target":
          addPolicyTarget();
          break;
        case "policy-remove-target":
          removePolicyTarget(id);
          break;
        case "policy-save":
          void savePolicy();
          break;
        default:
          return;
      }
      event.preventDefault?.();
    }

    function bind() {
      els.root?.addEventListener("click", handleClick);
      els.dialog?.addEventListener("click", handleClick);
      // Escape (the dialog's native cancel) must also restore focus.
      els.dialog?.addEventListener("cancel", (event) => {
        event.preventDefault?.();
        closeDialog();
      });
      els.dialog?.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && event.target?.id === "backup-preserve-reason") {
          event.preventDefault?.();
          void submitPreserve();
        }
      });
      els.refreshButton?.addEventListener("click", () => void load());
      els.cleanupButton?.addEventListener("click", (event) => void openCleanup(event.currentTarget || els.cleanupButton));
      els.editButton?.addEventListener("click", (event) => void openPolicyEditor(event.currentTarget || els.editButton));
    }

    return {
      bind,
      load,
      render,
      state,
      // exposed for tests
      actions: { verify, runNow, testTarget, unpreserve, openPreserve, submitPreserve, openRestore, restoreInspect, restoreImport, openCleanup, applyCleanup, showDetails, closeDialog, openPolicyEditor, savePolicy, addPolicyTarget, removePolicyTarget },
    };
  }

  return { createBackupLibrary, model };
});
