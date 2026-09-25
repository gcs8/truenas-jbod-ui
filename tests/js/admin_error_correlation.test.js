"use strict";

// Admin failures show a validated server request id, and the stopped state is
// described by the server rather than by the browser clock (#418).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

function functionSource(name) {
  const marker = `function ${name}(`;
  const start = SOURCE.indexOf(marker);
  assert.notEqual(start, -1, `missing ${name}`);
  const bodyStart = SOURCE.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    if (SOURCE[index] === "{") {
      depth += 1;
    } else if (SOURCE[index] === "}") {
      depth -= 1;
      if (depth === 0) {
        return SOURCE.slice(start, index + 1);
      }
    }
  }
  assert.fail(`unterminated ${name}`);
}

function loadFunctions(names, bindings = {}) {
  const context = vm.createContext({ ...bindings });
  vm.runInContext(
    `const SERVER_REQUEST_ID_PATTERN = /^[0-9a-f]{32}$/;\n` +
      `${names.map(functionSource).join("\n")}\n` +
      `globalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin-error-correlation.js" }
  );
  return context.__tested;
}

function responseWith(status, headers = {}) {
  return {
    status,
    headers: {
      get(name) {
        const key = Object.keys(headers).find(
          (candidate) => candidate.toLowerCase() === String(name).toLowerCase()
        );
        return key === undefined ? null : headers[key];
      },
    },
  };
}

const SERVER_ID = "0123456789abcdef0123456789abcdef";

test("a validated server request id is shown beside the error detail", () => {
  const { describeRequestFailure } = loadFunctions(
    ["validatedRequestId", "describeRequestFailure", "describeApiError"],
    {}
  );

  const message = describeRequestFailure(
    { detail: "Runtime control is unavailable.", request_id: SERVER_ID },
    responseWith(503)
  );

  assert.ok(message.includes("Runtime control is unavailable."));
  assert.ok(message.includes(SERVER_ID));
});

test("the request id header is used when the body omits one", () => {
  const { describeRequestFailure } = loadFunctions(
    ["validatedRequestId", "describeRequestFailure", "describeApiError"],
    {}
  );

  const message = describeRequestFailure(
    { detail: "Admin authentication required." },
    responseWith(401, { "X-Request-ID": SERVER_ID })
  );

  assert.ok(message.includes(SERVER_ID));
});

test("a request id that is not server-shaped is never rendered", () => {
  const { describeRequestFailure, validatedRequestId } = loadFunctions(
    ["validatedRequestId", "describeRequestFailure", "describeApiError"],
    {}
  );

  assert.equal(validatedRequestId("<b>oops</b>"), "");
  assert.equal(validatedRequestId("NOTHEX"), "");
  assert.equal(validatedRequestId(null), "");

  const message = describeRequestFailure(
    { detail: "Saving failed.", request_id: "<script>alert(1)</script>" },
    responseWith(500, { "X-Request-ID": "not-a-request-id" })
  );

  assert.equal(message, "Saving failed.");
  assert.ok(!message.includes("<script>"));
  assert.ok(!message.includes("request id"));
});

test("a failure with no payload still reports the status", () => {
  const { describeRequestFailure } = loadFunctions(
    ["validatedRequestId", "describeRequestFailure", "describeApiError"],
    {}
  );

  assert.equal(describeRequestFailure(null, responseWith(502)), "Request failed with 502");
});

test("an elapsed auto-stop is not reported as a shutdown the page observed", () => {
  const { formatCountdown } = loadFunctions(["formatCountdown", "sessionRemainingMs", "formatClockTime"], {
    state: { admin: { expires_at: "2000-01-01T00:00:00+00:00" } },
  });

  const label = formatCountdown();

  assert.ok(!/stopping/i.test(label), label);
  assert.ok(/auto-stop/i.test(label), label);
});

test("the offline recovery words come from the server state only", () => {
  const { describeOfflineRecovery } = loadFunctions(["describeOfflineRecovery"], {});

  assert.equal(describeOfflineRecovery(undefined), "");
  assert.equal(describeOfflineRecovery({ expired: false, summary: "x", next_step: "y" }), "");
  assert.equal(
    describeOfflineRecovery({
      expired: true,
      summary: "Admin's auto-stop time has passed.",
      next_step: "Run `docker compose up -d enclosure-admin` on the Docker host.",
    }),
    "Admin's auto-stop time has passed. Run `docker compose up -d enclosure-admin` on the Docker host."
  );
});
