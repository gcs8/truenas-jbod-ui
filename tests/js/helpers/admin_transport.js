"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../../admin_service/static/admin.js"), "utf8");
const transportNames = [
  "fetchJson", "fetchOrReportStopped", "sessionRemainingMs", "fetchWithTimeout", "requestTimeoutError",
  "readJsonResponse", "describeApiError", "validatedRequestId", "describeRequestFailure",
  "isMutatingRequest", "browserIsOffline", "adminRequestError", "classifyTransportFailure",
  "describeTransportFailure", "classifyResponseFailure", "describeResponseFailure",
];
const restoreNames = [
  "fetchBackupRestore", "validBackupRestoreResult", "describeBackupRestoreFailure", "requireMutationResult", "isNonEmptyString",
];

function loadAdminFunctions(names, bindings = {}) {
  const context = vm.createContext({
    AbortController, DOMException, setTimeout, clearTimeout,
    DEFAULT_REQUEST_TIMEOUT_MS: 60000, BACKUP_TRANSFER_TIMEOUT_MS: 1800000,
    SERVER_REQUEST_ID_PATTERN: /^[0-9a-f]{32}$/,
    state: { admin: {} }, ...bindings,
  });
  // Optional lookup lets new callers' regressions run on the exact predecessor.
  const definitions = names.map((name) => source.match(new RegExp(`^  (?:async )?function ${name}\\([\\s\\S]*?^  }`, "m"))?.[0]).filter(Boolean);
  vm.runInContext(definitions.join("\n"), context, { filename: "admin.js" });
  return context;
}

function restoreResult(overrides = {}) {
  return {
    ok: true, systems: [{ id: "restored", label: "Restored synthetic system" }], default_system_id: "restored",
    restored_paths: ["/synthetic/config.yaml"], restored_history_database: false,
    stopped_containers: [], restarted_containers: [], restart_failures: {},
    ...overrides,
  };
}

function jsonResponse(status, payload) {
  return { ok: status >= 200 && status < 300, status, headers: { get: () => "0123456789abcdef0123456789abcdef" }, json: async () => payload };
}

module.exports = { loadAdminFunctions, transportNames, restoreNames, restoreResult, jsonResponse };
