"use strict";

// findings.json + index.html for a crawl (#ui-coverage). The index groups
// native-resolution tiles by target and state with the findings beside them,
// so a reviewer scrolls real pixels instead of a downscaled 4K screenshot.

const fs = require("node:fs");
const path = require("node:path");

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
    unknownErrors: findings.filter((f) => f.severity === "error" && !f.known).length,
    byType: Object.fromEntries(Object.entries(byType).sort().map(([key, value]) => [key, { severity: value.severity, count: value.count, states: value.states.size }])),
    ...extra,
  };
}

function writeReport(outDir, { meta, records, actuations = [], guardLog = [], stats = {} }) {
  const summary = summarize(records, { blockedRequests: (stats.blocked || []).length, writesAdmitted: (stats.writes || []).length });
  const doc = { meta, summary, actuations, stats, guard: guardLog, records };
  fs.writeFileSync(path.join(outDir, "findings.json"), JSON.stringify(doc, null, 1));
  fs.writeFileSync(path.join(outDir, "index.html"), renderHtml(doc));
  return summary;
}

function findingList(findings) {
  if (!findings.length) return '<p class="ok">No findings.</p>';
  return `<ul class="findings">${findings.map((f) => `<li class="sev-${escapeHtml(f.severity)}${f.known ? " known" : ""}"><b>${escapeHtml(f.type)}</b>${f.known ? ` <i>known #${escapeHtml(f.known)}</i>` : ""} ${f.selector ? `<code>${escapeHtml(f.selector)}</code>` : ""} ${f.text ? `&ldquo;${escapeHtml(f.text)}&rdquo;` : ""} ${f.rect ? `<span class="rect">@${f.rect.x},${f.rect.y} ${f.rect.w}x${f.rect.h}</span>` : ""} ${f.detail ? `<span class="detail">${escapeHtml(JSON.stringify(f.detail))}</span>` : ""}</li>`).join("")}</ul>`;
}

function tileHtml(width, findings) {
  // Mark the tiles that contain a finding's rectangle.
  const hits = (tile) => findings.filter((f) => f.rect && f.rect.x < tile.x + tile.width && f.rect.x + f.rect.w > tile.x && f.rect.y < tile.y + tile.height && f.rect.y + f.rect.h > tile.y);
  return `<div class="tiles">${(width.tiles || []).map((tile) => {
    const marks = hits(tile);
    return `<figure class="${marks.length ? "flagged" : ""}"><img loading="lazy" src="${escapeHtml(tile.file)}" width="${tile.width}" height="${tile.height}" alt="tile at ${tile.x},${tile.y}"><figcaption>x${tile.x} y${tile.y}${marks.length ? ` &middot; ${marks.map((m) => escapeHtml(m.type)).join(", ")}` : ""}</figcaption></figure>`;
  }).join("")}</div>`;
}

function renderHtml(doc) {
  const { meta, summary, records, actuations } = doc;
  const typeRows = Object.entries(summary.byType).map(([type, value]) => `<tr class="sev-${escapeHtml(value.severity)}"><td>${escapeHtml(type)}</td><td>${value.count}</td><td>${value.states}</td></tr>`).join("");
  const actRows = actuations.map((a) => `<tr><td>${escapeHtml(a.target)}</td><td>${escapeHtml(a.state)}</td><td>${escapeHtml(a.safety)}</td><td class="st-${escapeHtml(a.status)}">${escapeHtml(a.status)}</td><td>${escapeHtml((a.admitted || []).map((w) => `${w.method} ${w.path}`).join("; "))}</td><td>${escapeHtml(a.error || a.reason || "")}</td></tr>`).join("");
  const nav = [];
  const sections = [];
  let currentTarget = null;
  for (const record of records) {
    if (record.target !== currentTarget) {
      currentTarget = record.target;
      sections.push(`<h2 id="${escapeHtml(record.target)}">${escapeHtml(record.target)}</h2>`);
      nav.push(`<a href="#${escapeHtml(record.target)}">${escapeHtml(record.target)}</a>`);
    }
    const errorCount = record.widths.reduce((n, w) => n + w.findings.filter((f) => f.severity === "error" && !f.known).length, 0) + record.findings.filter((f) => f.severity === "error").length;
    const widths = record.widths.map((width) => `<details${width.findings.some((f) => f.severity === "error" && !f.known) ? " open" : ""}><summary>${width.width}px &middot; ${width.size ? `${width.size[0]}x${width.size[1]}` : "no capture"} &middot; ${width.findings.length} findings${width.diff && width.diff.changedFraction !== undefined ? ` &middot; diff ${(width.diff.changedFraction * 100).toFixed(1)}%` : ""} &middot; <a href="${escapeHtml(width.full || "")}">full page</a></summary>${findingList(width.findings)}${tileHtml(width, width.findings)}</details>`).join("");
    sections.push(`<section class="state"><h3>${escapeHtml(record.state)} <small>${escapeHtml(record.safety)} &middot; ${escapeHtml(record.status)}${record.reason ? ` &middot; ${escapeHtml(record.reason)}` : ""}${errorCount ? ` &middot; <span class="bad">${errorCount} errors</span>` : ""}</small></h3>${record.findings.length ? findingList(record.findings) : ""}${widths}</section>`);
  }
  return `<!doctype html><html><head><meta charset="utf-8"><title>UI coverage crawl</title><style>
body{font:14px system-ui,sans-serif;margin:16px;background:#111;color:#ddd}a{color:#8cf}code{color:#fc8}
table{border-collapse:collapse}td,th{border:1px solid #444;padding:3px 8px;text-align:left}
.sev-error b,.bad{color:#f77}.sev-warn b{color:#fc6}.sev-info b{color:#8cf}.known{opacity:.6}.ok{color:#7d7}
.tiles{display:flex;flex-direction:column;gap:8px;overflow-x:auto}figure{margin:0}figure img{max-width:none;display:block;border:1px solid #333}
figure.flagged img{border:2px solid #f55}figcaption{font-size:12px;color:#999}.detail,.rect{color:#999;font-size:12px}
section.state{border-top:1px solid #333;margin-top:12px}nav a{margin-right:10px}.st-revert-failed{color:#f55;font-weight:bold}
</style></head><body>
<h1>UI coverage crawl</h1>
<p>${escapeHtml(meta.mode)} &middot; ${escapeHtml(meta.label || "")} &middot; ${escapeHtml(meta.started)} to ${escapeHtml(meta.finished)} &middot; widths ${escapeHtml((meta.widths || []).join(", "))}</p>
<p>${summary.targets} targets, ${summary.states} states, ${summary.captures} captures, ${summary.findings} findings (${summary.unknownErrors} unexplained errors), ${summary.blockedRequests} requests blocked by the guard, ${summary.writesAdmitted} writes admitted. Statuses: ${escapeHtml(JSON.stringify(summary.statuses))}</p>
<table><tr><th>Finding type</th><th>Count</th><th>States</th></tr>${typeRows}</table>
<h2>Actuations</h2><table><tr><th>Target</th><th>State</th><th>Class</th><th>Result</th><th>Admitted</th><th>Note</th></tr>${actRows || "<tr><td colspan=6>none</td></tr>"}</table>
<nav>${nav.join("")}</nav>
${sections.join("\n")}
</body></html>`;
}

// Merge several crawl directories (one per browser connection) under outDir.
function mergeReports(outDir, runDirs) {
  const merged = { records: [], actuations: [], guardLog: [], stats: { blocked: [], writes: [], routeErrors: [] }, runs: [] };
  for (const dir of runDirs) {
    const doc = JSON.parse(fs.readFileSync(path.join(dir, "findings.json"), "utf8"));
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
    merged.runs.push({ dir: prefix, meta: doc.meta, summary: doc.summary });
  }
  const starts = merged.runs.map((run) => run.meta.started).filter(Boolean).sort();
  const ends = merged.runs.map((run) => run.meta.finished).filter(Boolean).sort();
  return writeReport(outDir, {
    meta: { mode: "merged", label: `${merged.runs.length} runs`, started: starts[0] || "", finished: ends[ends.length - 1] || "", widths: [...new Set(merged.runs.flatMap((run) => run.meta.widths || []))], runs: merged.runs.map((run) => ({ dir: run.dir, meta: run.meta })) },
    records: merged.records, actuations: merged.actuations, guardLog: merged.guardLog, stats: merged.stats,
  });
}

module.exports = { writeReport, summarize, allFindings, renderHtml, mergeReports };
