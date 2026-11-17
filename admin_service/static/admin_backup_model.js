// Pure model for the admin Backups library page (#398): ids, labels, the
// read-only policy, plan and status wording, and the settings payload. No DOM
// and no requests. admin_backups.js takes its names from here, so base.html
// loads this script first. Free-form text goes through scrubText before the page.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AdminBackupModel = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  const CLASSES = ["config", "full"];
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

  // #722: an FTP copy was checked by size only at upload. Until an explicit
  // Verify downloads it and matches size and SHA-256, say exactly that.
  // #1202: the list's Verified column uses this too, so both views agree. A
  // copy on a target whose upload check was never recorded (catalogued before
  // #722) says so rather than claiming a readback nobody saw.
  function verifiedDetailText(item) {
    if (!item?.verified) return "Not yet";
    const explicitlyVerified = Boolean(item.last_verify && typeof item.last_verify === "object" && item.last_verify.ok === true);
    if (explicitlyVerified) return "Yes";
    if (item.upload_check === "size") {
      return "Size only at upload; Verify re-reads and hashes it";
    }
    // A checksum the storage provider reported is not a read-back by this app.
    if (item.upload_check === "provider_checksum") {
      return "Checked by the provider's checksum at upload; Verify re-reads and hashes it";
    }
    if (item.upload_check === "provider_checksum_or_readback") {
      return "Checked at upload by the provider's checksum or by read-back; Verify re-reads and hashes it";
    }
    if (item.location !== "local" && item.upload_check !== "readback") {
      return "Upload check not recorded; Verify re-reads and hashes it";
    }
    return "Yes";
  }

  function lastRunSummary(lastRun) {
    if (!lastRun || typeof lastRun !== "object") {
      return { tone: "", text: "Not used yet" };
    }
    // Only a boolean true is a pass; a malformed result never reads as Worked.
    const worked = lastRun.ok === true;
    return {
      tone: worked ? "is-ok" : "is-bad",
      text: worked ? "Worked" : `Failed${lastRun.detail ? `: ${scrubText(lastRun.detail)}` : ""}`,
      at: lastRun.at || "",
    };
  }

  // #1202: a Verify that could not open or read the copy checked nothing and
  // changed nothing; it is not a failed check. A result with no outcome (from
  // an earlier release) keeps the Worked/Failed wording.
  function lastCheckSummary(lastVerify) {
    if (lastVerify.outcome === "not_read" && lastVerify.ok !== true) {
      const reason = lastVerify.detail ? `: ${scrubText(lastVerify.detail)}` : "";
      return { tone: "", text: `Could not read the copy, nothing checked${reason}; earlier status unchanged`, at: lastVerify.at || "" };
    }
    return lastRunSummary(lastVerify);
  }

  // The last scheduled clean-up, for the status line: a summary when it
  // worked, otherwise each place it could not finish and why.
  function cleanupSummary(grooming, targets, formatTime = (value) => String(value)) {
    if (!grooming || typeof grooming !== "object") return null;
    const when = grooming.at ? formatTime(grooming.at) : "";
    if (grooming.ok) {
      const text = `Last clean-up: ${when || "-"}, ${Number(grooming.deleted) || 0} removed.`;
      // A finished clean-up can still have found damaged kept copies (#1188).
      if (grooming.detail) return { tone: "is-bad", text: `${text} ${scrubText(grooming.detail)}` };
      return { tone: "is-ok", text };
    }
    const failed = grooming.failed_locations && typeof grooming.failed_locations === "object" ? grooming.failed_locations : {};
    const places = Object.keys(failed).map((location) => `${locationLabel(location, targets)}: ${scrubText(failed[location] || "no detail")}`);
    const reason = places.length ? places.join("; ") : scrubText(grooming.detail || "no detail");
    return { tone: "is-bad", text: `Last clean-up${when ? ` ${when}` : ""} didn't finish: ${reason}.` };
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
        const entry = { values: { ...target.values }, secrets };
        if (target.original_target_id) entry.original_target_id = target.original_target_id;
        return entry;
      });
    }
    return payload;
  }

  return {
    CLASSES, STATE_LABELS, scrubText, validId, providerLabel, locationLabel, isRestorable, canDownload, stateLabel,
    groupArtifacts, humanKey, describePolicy, humanizePlanReason, normalizePlan, verifiedDetailText, lastRunSummary,
    lastCheckSummary, cleanupSummary, shortHash, SECRET_LABELS, BOOLEAN_TARGET_FIELDS, NUMBER_TARGET_FIELDS,
    NULLABLE_CLASS_FIELDS, secretStateText, buildPolicyPayload,
  };
});
