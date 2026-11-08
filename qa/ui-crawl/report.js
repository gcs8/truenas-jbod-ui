"use strict";

// findings.json + index.html for a crawl (#ui-coverage). findings.json keeps
// every raw finding. The index groups repeated findings (same type, same
// element pattern) into one row with a count and example tiles, with filters
// by severity, known/new and type, so thousands of instances of one known
// defect read as one item. Each state's section links to its groups and shows
// its native-resolution tiles, flagged where a finding's rectangle falls.

const fs = require("node:fs");
const path = require("node:path");
const { evaluateGate } = require("./gate");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function allFindings(records) {
  const out = [];
  for (const record of records) {
    for (const finding of record.findings) out.push({ ...finding, target: record.target, state: record.state, width: null });
    for (const width of record.widths) for (const finding of width.findings) out.push({ ...finding, target: record.target, state: record.state, width: width.width });
  }
  return out;
}

// Errors that no known issue or declared fixture expectation explains. The
// CI spec refuses any of these.
function unexplainedErrors(records) {
  return allFindings(records).filter((finding) => finding.severity === "error" && !finding.known && !finding.expected);
}

// An element pattern: the selector (or text) with positions and numbers
// generalised, so the same defect on every bay is one group.
function patternOf(finding) {
  const source = finding.selector || finding.text || "";
  return String(source)
    .replace(/:nth-(child|of-type)\(\d+\)/g, ":nth-$1(n)")
    .replace(/(\[[\w-]+=)(["']?)[^\]"']*\2\]/g, "$1…]")
    .replace(/\d+(\.\d+)?/g, "#")
    .replace(/\s+/g, " ")
    .slice(0, 120);
}

function groupKey(finding) {
  return [finding.type, finding.severity, finding.known || "", finding.expected ? "expected" : "", patternOf(finding)].join("\u0001");
}

function groupFindings(records, { examples = 3 } = {}) {
  const groups = new Map();
  for (const record of records) {
    const visit = (finding, width) => {
      const key = groupKey(finding);
      let group = groups.get(key);
      if (!group) {
        group = {
          id: null, type: finding.type, severity: finding.severity, known: finding.known || null, expected: Boolean(finding.expected),
          pattern: patternOf(finding), count: 0, states: new Set(), targets: new Set(), widths: new Set(), examples: [],
        };
        groups.set(key, group);
      }
      group.count += 1;
      group.states.add(record.state);
      group.targets.add(record.target);
      if (width) group.widths.add(width.width);
      if (group.examples.length < examples) {
        const tile = width && finding.rect ? (width.tiles || []).find((t) => finding.rect.x < t.x + t.width && finding.rect.x + finding.rect.w > t.x && finding.rect.y < t.y + t.height && finding.rect.y + finding.rect.h > t.y) : null;
        group.examples.push({
          target: record.target, state: record.state, width: width ? width.width : null, selector: finding.selector || null,
          text: finding.text || null, tile: tile ? tile.file : ((width && (width.tiles || [])[0]) || {}).file || null,
        });
      }
      return group;
    };
    for (const finding of record.findings) finding.group = visit(finding, null);
    for (const width of record.widths) for (const finding of width.findings) finding.group = visit(finding, width);
  }
  const order = { error: 0, warn: 1, info: 2 };
  const list = [...groups.values()].sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3) || Boolean(a.known) - Boolean(b.known) || b.count - a.count);
  list.forEach((group, index) => { group.id = `G${index + 1}`; });
  return list.map((group) => ({ ...group, states: group.states.size, targets: group.targets.size, widths: [...group.widths].sort((a, b) => a - b) }));
}

function summarize(records, extra = {}) {
  const findings = allFindings(records);
  const byType = {};
  for (const finding of findings) {
    const key = `${finding.type}${finding.known ? ` (known #${finding.known})` : ""}`;
    byType[key] = byType[key] || { severity: finding.severity, count: 0, states: new Set() };
    byType[key].count += 1;
    byType[key].states.add(finding.state);
  }
  const statuses = {};
  for (const record of records) statuses[record.status] = (statuses[record.status] || 0) + 1;
  return {
    targets: new Set(records.map((r) => r.target)).size,
    states: records.length,
    captures: records.reduce((sum, r) => sum + r.widths.filter((w) => w.full).length, 0),
    statuses,
    findings: findings.length,
    unknownErrors: unexplainedErrors(records).length,
    byType: Object.fromEntries(Object.entries(byType).sort().map(([key, value]) => [key, { severity: value.severity, count: value.count, states: value.states.size }])),
    ...extra,
  };
}

function writeReport(outDir, { meta, records, actuations = [], guardLog = [], stats = {}, ledCleanup = null, problems = [] }) {
  const safety = evaluateGate({ actuations, stats, records, ledCleanup, problems });
  const summary = summarize(records, { blockedRequests: (stats.blocked || []).length, writesAdmitted: (stats.writes || []).length, safety: safety.status });
  const groups = groupFindings(records);
  const doc = { meta, summary, safety, actuations, stats, guard: guardLog, groups: groups.map(({ examples, ...rest }) => ({ ...rest, examples })), records };
  fs.writeFileSync(path.join(outDir, "findings.json"), JSON.stringify(doc, (key, value) => (key === "group" ? value && value.id : value), 1));
  fs.writeFileSync(path.join(outDir, "index.html"), renderHtml({ ...doc, groups }));
  return summary;
}

function groupRows(groups) {
  return groups.map((g) => `<tr id="g-${escapeHtml(g.id)}" class="grp sev-${escapeHtml(g.severity)}" data-severity="${escapeHtml(g.severity)}" data-known="${g.known || g.expected ? "known" : "new"}" data-type="${escapeHtml(g.type)}">`
    + `<td>${escapeHtml(g.id)}</td><td><b>${escapeHtml(g.type)}</b></td><td>${escapeHtml(g.severity)}</td>`
    + `<td>${g.known ? `known #${escapeHtml(g.known)}` : g.expected ? "expected (fixture)" : "<span class=\"bad\">new</span>"}</td>`
    + `<td>${g.count}</td><td>${g.states} states / ${g.targets} targets${g.widths.length ? ` @ ${escapeHtml(g.widths.join(", "))}` : ""}</td>`
    + `<td><code>${escapeHtml(g.pattern)}</code></td>`
    + `<td>${g.examples.map((e) => `${e.tile ? `<a href="${escapeHtml(e.tile)}">` : ""}${escapeHtml(e.target)} ${escapeHtml(e.state)}${e.width ? ` ${e.width}px` : ""}${e.tile ? "</a>" : ""}`).join("<br>")}</td></tr>`).join("");
}

function groupRefs(findings) {
  if (!findings.length) return '<p class="ok">No findings.</p>';
  const counts = new Map();
  for (const finding of findings) {
    const id = finding.group ? finding.group.id : "?";
    counts.set(id, (counts.get(id) || 0) + 1);
  }
  return `<p class="refs">Findings: ${[...counts].map(([id, n]) => `<a class="ref" data-group="${escapeHtml(id)}" href="#g-${escapeHtml(id)}">${escapeHtml(id)}</a>${n > 1 ? ` &times;${n}` : ""}`).join(", ")}</p>`;
}

function tileHtml(width, findings) {
  // Mark the tiles that contain a finding's rectangle, with its group ids.
  const hits = (tile) => findings.filter((f) => f.rect && f.rect.x < tile.x + tile.width && f.rect.x + f.rect.w > tile.x && f.rect.y < tile.y + tile.height && f.rect.y + f.rect.h > tile.y);
  return `<div class="tiles">${(width.tiles || []).map((tile) => {
    const marks = [...new Set(hits(tile).map((m) => (m.group ? m.group.id : "?")))];
    return `<figure class="${marks.length ? "flagged" : ""}"><img loading="lazy" src="${escapeHtml(tile.file)}" width="${tile.width}" height="${tile.height}" alt="tile at ${tile.x},${tile.y}"><figcaption>x${tile.x} y${tile.y}${marks.length ? ` &middot; ${marks.map((id) => `<a href="#g-${escapeHtml(id)}">${escapeHtml(id)}</a>`).join(", ")}` : ""}</figcaption></figure>`;
  }).join("")}</div>`;
}

const FILTER_SCRIPT = `(() => {
  const sev = document.getElementById("filter-severity");
  const known = document.getElementById("filter-known");
  const type = document.getElementById("filter-type");
  const apply = () => {
    const visible = new Set();
    for (const row of document.querySelectorAll("tr.grp")) {
      const show = (!sev.value || row.dataset.severity === sev.value) && (!known.value || row.dataset.known === known.value)
        && (!type.value || row.dataset.type.includes(type.value.trim()));
      row.hidden = !show;
      if (show) visible.add(row.id.slice(2));
    }
    const filtering = Boolean(sev.value || known.value || type.value.trim());
    for (const section of document.querySelectorAll("section.state")) {
      const refs = Array.from(section.querySelectorAll("a.ref")).map((a) => a.dataset.group);
      section.hidden = filtering && !refs.some((id) => visible.has(id));
    }
  };
  for (const el of [sev, known, type]) el.addEventListener("input", apply);
})();`;

function renderHtml(doc) {
  const { meta = {}, summary, records, actuations = [], safety = null } = doc;
  const groups = doc.groups && doc.groups.length && typeof doc.groups[0].states === "number" ? doc.groups : groupFindings(records);
  const typeRows = Object.entries(summary.byType || {}).map(([type, value]) => `<tr class="sev-${escapeHtml(value.severity)}"><td>${escapeHtml(type)}</td><td>${value.count}</td><td>${value.states}</td></tr>`).join("");
  const actRows = actuations.map((a) => `<tr><td>${escapeHtml(a.target)}</td><td>${escapeHtml(a.state)}</td><td>${escapeHtml(a.safety)}</td><td class="st-${escapeHtml(a.status)}">${escapeHtml(a.status)}</td><td>${escapeHtml((a.admitted || []).map((w) => `${w.method} ${w.path}`).join("; "))}</td><td>${escapeHtml(a.error || a.reason || "")}</td></tr>`).join("");
  const nav = [];
  const sections = [];
  let currentTarget = null;
  for (const record of records) {
    if (record.target !== currentTarget) {
      currentTarget = record.target;
      sections.push(`<h2 id="t-${escapeHtml(record.target)}">${escapeHtml(record.target)}</h2>`);
      nav.push(`<a href="#t-${escapeHtml(record.target)}">${escapeHtml(record.target)}</a>`);
    }
    const errorCount = record.widths.reduce((n, w) => n + w.findings.filter((f) => f.severity === "error" && !f.known && !f.expected).length, 0) + record.findings.filter((f) => f.severity === "error" && !f.known && !f.expected).length;
    const widths = record.widths.map((width) => `<details${width.findings.some((f) => f.severity === "error" && !f.known && !f.expected) ? " open" : ""}><summary>${width.width}px &middot; ${width.size ? `${width.size[0]}x${width.size[1]}` : "no capture"} &middot; ${width.findings.length} findings${width.diff && width.diff.changedFraction !== undefined ? ` &middot; diff ${(width.diff.changedFraction * 100).toFixed(1)}%` : ""} &middot; <a href="${escapeHtml(width.full || "")}">full page</a></summary>${groupRefs(width.findings)}${tileHtml(width, width.findings)}</details>`).join("");
    sections.push(`<section class="state"><h3>${escapeHtml(record.state)} <small>${escapeHtml(record.safety)} &middot; ${escapeHtml(record.status)}${record.reason ? ` &middot; ${escapeHtml(record.reason)}` : ""}${errorCount ? ` &middot; <span class="bad">${errorCount} new errors</span>` : ""}</small></h3>${record.findings.length ? groupRefs(record.findings) : ""}${widths}</section>`);
  }
  const safetyHtml = safety
    ? `<h2>Safety gate: <span class="${safety.status === "ok" ? "ok" : "bad"}">${escapeHtml(safety.status)}</span></h2>${safety.failures && safety.failures.length ? `<ul>${safety.failures.map((f) => `<li class="bad">${escapeHtml(f)}</li>`).join("")}</ul>` : ""}${safety.ledCleanup ? `<p>Identify cleanup: ${escapeHtml(JSON.stringify(safety.ledCleanup))}</p>` : ""}`
    : "";
  return `<!doctype html><html><head><meta charset="utf-8"><title>UI coverage crawl</title><style>
body{font:14px system-ui,sans-serif;margin:16px;background:#111;color:#ddd}a{color:#8cf}code{color:#fc8}
table{border-collapse:collapse}td,th{border:1px solid #444;padding:3px 8px;text-align:left;vertical-align:top}
.sev-error b,.bad{color:#f77}.sev-warn b{color:#fc6}.sev-info b{color:#8cf}.ok{color:#7d7}
.tiles{display:flex;flex-direction:column;gap:8px;overflow-x:auto}figure{margin:0}figure img{max-width:none;display:block;border:1px solid #333}
figure.flagged img{border:2px solid #f55}figcaption{font-size:12px;color:#999}.refs{color:#bbb}
section.state{border-top:1px solid #333;margin-top:12px}nav a{margin-right:10px}.st-revert-failed,.st-preview-failed,.st-precheck-failed{color:#f55;font-weight:bold}
.filters{margin:8px 0}.filters label{margin-right:12px}
</style></head><body>
<h1>UI coverage crawl</h1>
<p>${escapeHtml(meta.mode)} &middot; ${escapeHtml(meta.label || "")} &middot; ${escapeHtml(meta.started)} to ${escapeHtml(meta.finished)} &middot; widths ${escapeHtml((meta.widths || []).join(", "))}</p>
<p>${summary.targets} targets, ${summary.states} states, ${summary.captures} captures, ${summary.findings} findings in ${groups.length} groups (${summary.unknownErrors} unexplained errors), ${summary.blockedRequests ?? 0} requests blocked by the guard, ${summary.writesAdmitted ?? 0} writes admitted. Statuses: ${escapeHtml(JSON.stringify(summary.statuses))}</p>
${safetyHtml}
<h2>Grouped findings</h2>
<div class="filters"><label>Severity <select id="filter-severity"><option value="">all</option><option value="error">error</option><option value="warn">warn</option><option value="info">info</option></select></label>
<label>Known <select id="filter-known"><option value="">all</option><option value="new">new only</option><option value="known">known / expected</option></select></label>
<label>Type <input id="filter-type" placeholder="contains"></label></div>
<table><tr><th>Group</th><th>Type</th><th>Severity</th><th>Issue</th><th>Count</th><th>Where</th><th>Element pattern</th><th>Examples</th></tr>${groupRows(groups) || "<tr><td colspan=8>none</td></tr>"}</table>
<details><summary>Totals by finding type</summary><table><tr><th>Finding type</th><th>Count</th><th>States</th></tr>${typeRows}</table></details>
<h2>Actuations</h2><table><tr><th>Target</th><th>State</th><th>Class</th><th>Result</th><th>Admitted</th><th>Note</th></tr>${actRows || "<tr><td colspan=6>none</td></tr>"}</table>
<nav>${nav.join("")}</nav>
${sections.join("\n")}
<script>${FILTER_SCRIPT}</script>
</body></html>`;
}

// Merge several crawl directories (one per browser connection) under outDir.
// A missing or unreadable chunk report, or a chunk whose own gate failed, is
// a merge problem: the merged gate fails and the CLI exits non-zero.
function mergeReports(outDir, runDirs) {
  fs.mkdirSync(outDir, { recursive: true });
  const merged = { records: [], actuations: [], guardLog: [], stats: { blocked: [], writes: [], applianceLoads: [], routeErrors: [] }, runs: [], problems: [], ledCleanups: [] };
  for (const dir of runDirs) {
    let doc;
    try {
      doc = JSON.parse(fs.readFileSync(path.join(dir, "findings.json"), "utf8"));
      if (!doc || !Array.isArray(doc.records) || !Array.isArray(doc.actuations)) throw new Error("not a crawl report");
    } catch (error) {
      merged.problems.push(`report ${path.relative(outDir, dir) || dir}: ${String(error.code === "ENOENT" ? "missing" : error.message).slice(0, 120)}`);
      continue;
    }
    const prefix = path.relative(outDir, dir);
    const fix = (file) => (file ? path.join(prefix, file) : file);
    for (const record of doc.records) {
      for (const width of record.widths) {
        width.full = fix(width.full);
        for (const tile of width.tiles || []) tile.file = fix(tile.file);
      }
      record.downloads = (record.downloads || []).map(fix);
      merged.records.push(record);
    }
    merged.actuations.push(...doc.actuations);
    merged.guardLog.push(...(doc.guard || []));
    for (const key of Object.keys(merged.stats)) merged.stats[key].push(...((doc.stats || {})[key] || []));
    if (doc.safety && doc.safety.status !== "ok" && doc.safety.status !== "crawl-errors") merged.problems.push(`run ${prefix}: safety ${doc.safety.status}`);
    if (doc.safety && doc.safety.ledCleanup) merged.ledCleanups.push(doc.safety.ledCleanup);
    merged.runs.push({ dir: prefix, meta: doc.meta || {}, summary: doc.summary, safety: doc.safety ? doc.safety.status : null });
  }
  if (!merged.runs.length) merged.problems.push("no chunk report was merged");
  const starts = merged.runs.map((run) => run.meta.started).filter(Boolean).sort();
  const ends = merged.runs.map((run) => run.meta.finished).filter(Boolean).sort();
  const ledCleanup = merged.ledCleanups.length ? { ok: merged.ledCleanups.every((c) => c.ok !== false), runs: merged.ledCleanups.length } : null;
  const summary = writeReport(outDir, {
    meta: { mode: "merged", label: `${merged.runs.length} runs`, started: starts[0] || "", finished: ends[ends.length - 1] || "", widths: [...new Set(merged.runs.flatMap((run) => run.meta.widths || []))], runs: merged.runs.map((run) => ({ dir: run.dir, meta: run.meta, safety: run.safety })) },
    records: merged.records, actuations: merged.actuations, guardLog: merged.guardLog, stats: merged.stats, ledCleanup, problems: merged.problems,
  });
  return { summary, problems: merged.problems };
}

module.exports = { writeReport, summarize, allFindings, unexplainedErrors, groupFindings, patternOf, renderHtml, mergeReports };
