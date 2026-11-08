"use strict";

// In-page layout, heat-map and contrast checks for the UI coverage crawler.
//
// `pageChecks` runs inside the browser through page.evaluate, so it must be
// self-contained. It returns raw findings; the crawler adds state, width and
// screenshot references. The checks target the classes of defect that
// slipped past beta.6 review (#1102, #1103): text squeezed to a few characters
// per line or broken inside a word, empty layout tracks beside a squeezed
// sibling, horizontal overflow, clipped or overlapping controls, heat colours
// that turn a 1-degree spread into an alarm, and unreadable tile text.

function pageChecks(options) {
  const opts = Object.assign({
    maxTextElements: 4000,
    narrowLines: 3,
    narrowCharsPerLine: 4,
    emptyTrackPx: 48,
    temperatureAlarmMinC: 50,
    temperatureSmallDeltaC: 3,
    relativeSmallDelta: 0.03,
    contrastMin: 4.5,
    maxFindingsPerType: 25,
    // Elements whose truncated text is shown by a custom hover tooltip.
    tooltipHosts: ".slot-tile",
  }, options || {});
  const findings = [];
  const counts = {};
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const push = (type, severity, element, detail) => {
    counts[type] = (counts[type] || 0) + 1;
    if (counts[type] > opts.maxFindingsPerType) return;
    const rect = element && element.getBoundingClientRect ? element.getBoundingClientRect() : null;
    findings.push({
      type,
      severity,
      selector: element ? describe(element) : null,
      text: element ? String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80) : null,
      rect: rect ? { x: Math.round(rect.left + window.scrollX), y: Math.round(rect.top + window.scrollY), w: Math.round(rect.width), h: Math.round(rect.height) } : null,
      detail,
    });
  };

  function describe(element) {
    const parts = [];
    let node = element;
    for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth += 1) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`${part}#${node.id}`);
        break;
      }
      const cls = Array.from(node.classList || []).filter((c) => !/^(is-|has-|state-)/.test(c)).slice(0, 2);
      if (cls.length) part += `.${cls.join(".")}`;
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  const styleCache = new Map();
  const style = (element) => {
    let value = styleCache.get(element);
    if (!value) {
      value = getComputedStyle(element);
      styleCache.set(element, value);
    }
    return value;
  };

  function visible(element) {
    if (!element || element.nodeType !== 1) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
      const s = style(node);
      if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse" || Number(s.opacity) === 0) return false;
      if (node.tagName === "DIALOG" && !node.open) return false;
      if (node.tagName === "DETAILS" && !node.open && node !== element && !node.querySelector(":scope > summary")?.contains(element)) return false;
    }
    return true;
  }

  // Nearest ancestor that scrolls or clips horizontally.
  function scrollParent(element) {
    for (let node = element.parentElement; node && node !== document.documentElement; node = node.parentElement) {
      const s = style(node);
      if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) return { node, mode: s.overflowX };
    }
    return null;
  }

  // ---- text squeeze and mid-word breaks ---------------------------------
  const lineTops = (rects) => {
    const tops = [];
    for (const r of rects) {
      if (r.width < 0.5 || r.height < 0.5) continue;
      if (!tops.some((t) => Math.abs(t - r.top) < r.height * 0.5)) tops.push(r.top);
    }
    return tops.length;
  };
  const squeezed = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let scanned = 0;
  const range = document.createRange();
  for (let element = walker.currentNode; element && scanned < opts.maxTextElements; element = walker.nextNode()) {
    if (element.nodeType !== 1 || ["SCRIPT", "STYLE", "SVG", "PRE", "CODE", "TEXTAREA", "OPTION", "SELECT"].includes(element.tagName)) continue;
    const textNodes = Array.from(element.childNodes).filter((n) => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (!textNodes.length) continue;
    if (!visible(element)) continue;
    scanned += 1;
    const text = textNodes.map((n) => n.textContent).join(" ").replace(/\s+/g, " ").trim();
    let rects = [];
    for (const node of textNodes) {
      range.selectNodeContents(node);
      rects = rects.concat(Array.from(range.getClientRects()));
    }
    const lines = lineTops(rects);
    const s = style(element);
    const breakAll = /break-all|anywhere/.test(`${s.wordBreak} ${s.overflowWrap}`);
    if (lines >= opts.narrowLines && text.length >= 6 && text.length / lines < opts.narrowCharsPerLine) {
      push("text-narrow-wrap", "error", element, { lines, chars: text.length, charsPerLine: Number((text.length / lines).toFixed(1)) });
      squeezed.push(element);
      continue;
    }
    if (lines < 2 || breakAll) continue;
    // A dictionary-like word rendered on two lines was broken inside the word.
    for (const node of textNodes) {
      const value = node.textContent;
      const re = /[A-Za-z][a-z]{3,}/g;
      let match;
      let checked = 0;
      while ((match = re.exec(value)) && checked < 40) {
        checked += 1;
        range.setStart(node, match.index);
        range.setEnd(node, match.index + match[0].length);
        if (lineTops(Array.from(range.getClientRects())) > 1) {
          push("text-mid-word-break", "error", element, { word: match[0] });
          squeezed.push(element);
          checked = 99;
          break;
        }
      }
      if (checked === 99) break;
    }
  }

  // ---- empty track beside a squeezed sibling ------------------------------
  const reportedContainers = new Set();
  for (const element of squeezed) {
    for (let node = element.parentElement; node && node !== document.body; node = node.parentElement) {
      const s = style(node);
      const isGrid = /grid/.test(s.display);
      const isRowFlex = /flex/.test(s.display) && !/column/.test(s.flexDirection);
      if (!isGrid && !isRowFlex) continue;
      const children = Array.from(node.children).filter(visible);
      if (children.length < 2) continue;
      const box = node.getBoundingClientRect();
      const left = box.left + parseFloat(s.paddingLeft || 0) + parseFloat(s.borderLeftWidth || 0);
      const right = box.right - parseFloat(s.paddingRight || 0) - parseFloat(s.borderRightWidth || 0);
      const spans = children.map((c) => c.getBoundingClientRect()).map((r) => [Math.max(left, r.left), Math.min(right, r.right)]).filter(([a, b]) => b > a).sort((a, b) => a[0] - b[0]);
      let cursor = left;
      let gap = 0;
      for (const [a, b] of spans) {
        gap = Math.max(gap, a - cursor);
        cursor = Math.max(cursor, b);
      }
      gap = Math.max(gap, right - cursor);
      const columnGap = parseFloat(s.columnGap) || 0;
      if (gap - columnGap >= opts.emptyTrackPx && !reportedContainers.has(node)) {
        reportedContainers.add(node);
        push("empty-track-squeeze", "error", node, { emptyPx: Math.round(gap - columnGap), squeezed: describe(element) });
      }
      break;
    }
  }

  // ---- horizontal overflow ----------------------------------------------
  const docWidth = document.documentElement.scrollWidth;
  if (docWidth > vw + 1) {
    const offenders = [];
    for (const element of document.body.querySelectorAll("*")) {
      if (offenders.length >= 5) break;
      const r = element.getBoundingClientRect();
      if (r.right <= vw + 1 || r.width < 1) continue;
      if (scrollParent(element)) continue;
      if (offenders.some((o) => o.contains(element))) continue;
      if (!visible(element)) continue;
      offenders.push(element);
    }
    push("page-horizontal-overflow", "error", offenders[0] || document.body, {
      documentWidth: docWidth,
      viewport: vw,
      offenders: offenders.map(describe),
    });
  }

  // ---- controls: clipped, truncated, overlapping -------------------------
  // With a modal dialog open the page behind it is inert: check the dialog only.
  const modal = Array.from(document.querySelectorAll("dialog[open]")).find((dialog) => dialog.matches(":modal"));
  const controls = Array.from((modal || document).querySelectorAll("button, a[href], select, input:not([type=hidden]), textarea, summary, [role=button]"))
    .filter((element) => visible(element) && style(element).pointerEvents !== "none");
  const textish = Array.from(document.querySelectorAll("button, a, label, h1, h2, h3, h4, th, td, dt, dd, span, p, strong, small, summary, li, div"))
    .filter((element) => element.children.length === 0 && (element.textContent || "").trim().length > 1);
  for (const element of textish) {
    const s = style(element);
    if (element.scrollWidth <= element.clientWidth + 1 || !/(hidden|clip)/.test(s.overflowX)) continue;
    if (!visible(element)) continue;
    const titled = element.title || element.closest("[title], [aria-label], [aria-describedby]") || (opts.tooltipHosts && element.closest(opts.tooltipHosts));
    if (s.textOverflow === "ellipsis") {
      if (!titled) push("text-truncated-no-tooltip", "warn", element, { scrollWidth: element.scrollWidth, clientWidth: element.clientWidth });
    } else if (element.matches("button, a, label, summary, th, dt")) {
      push("control-clipped", "error", element, { scrollWidth: element.scrollWidth, clientWidth: element.clientWidth });
    }
  }
  for (const element of controls) {
    const r = element.getBoundingClientRect();
    const parent = scrollParent(element);
    if (!parent || !/(hidden|clip)/.test(parent.mode)) continue;
    const p = parent.node.getBoundingClientRect();
    const visibleWidth = Math.min(r.right, p.right) - Math.max(r.left, p.left);
    if (visibleWidth > 0 && visibleWidth < r.width * 0.9) {
      push("control-clipped", "error", element, { visibleFraction: Number((visibleWidth / r.width).toFixed(2)), clippedBy: describe(parent.node) });
    }
  }
  const boxes = controls.map((element) => ({ element, r: element.getBoundingClientRect() }));
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i];
      const b = boxes[j];
      if (a.element.contains(b.element) || b.element.contains(a.element)) continue;
      const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
      const h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
      if (w <= 2 || h <= 2) continue;
      const smaller = Math.min(a.r.width * a.r.height, b.r.width * b.r.height);
      if (w * h < smaller * 0.1) continue;
      // A label that wraps its own input is one control.
      if (a.element.closest("label") && a.element.closest("label") === b.element.closest("label")) continue;
      push("control-overlap", "error", a.element, { other: describe(b.element), overlapPx: Math.round(w * h) });
    }
  }

  // ---- heat map ----------------------------------------------------------
  const heat = { active: 0 };
  const tiles = Array.from(document.querySelectorAll(".slot-tile.heatmap-active")).filter(visible);
  if (tiles.length) {
    const parseRgb = (value) => String(value || "").split(",").map((part) => Number(part.trim())).slice(0, 3);
    const metricSelect = document.getElementById("heatmap-metric-select");
    const metricLabel = metricSelect && metricSelect.selectedOptions[0] ? metricSelect.selectedOptions[0].textContent.trim() : "";
    const records = tiles.map((tile) => {
      const badge = tile.querySelector(".slot-heatmap-value");
      const raw = badge ? badge.textContent : "";
      const value = parseFloat(String(raw).replace(/[^0-9.\-]/g, ""));
      const rgb = parseRgb(tile.style.getPropertyValue("--heatmap-rgb"));
      const alpha = Number(tile.style.getPropertyValue("--heatmap-alpha")) || 0.62;
      return { tile, raw, value, rgb, alpha };
    }).filter((record) => Number.isFinite(record.value) && record.rgb.length === 3);
    // Absolute temperature (not a delta from the average): alarm colours need a hot disk.
    const isTemperature = (/temp/i.test(metricLabel) && !/\bvs\b|delta|average/i.test(metricLabel))
      || (!metricLabel && records.some((record) => /^\s*\d+(\.\d+)?\s*(°\s*)?C\s*$/.test(record.raw)));
    const isTemperatureLike = /temp/i.test(metricLabel);
    const alarm = (rgb) => rgb[0] >= 180 && rgb[1] <= 130;
    const values = records.map((record) => record.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const colours = new Set(records.map((record) => record.rgb.join(",")));
    const spread = records.some((record) => alarm(record.rgb)) && records.some((record) => !alarm(record.rgb));
    Object.assign(heat, { active: records.length, metric: metricLabel, min, max, colours: colours.size, temperature: isTemperature });
    const smallDelta = isTemperatureLike ? opts.temperatureSmallDeltaC : Math.max(1, Math.abs(max) * opts.relativeSmallDelta);
    if (records.length >= 2 && max - min <= smallDelta && spread) {
      push("heat-small-range-spread", "error", records[0].tile.closest(".enclosure-face") || records[0].tile, {
        metric: metricLabel, min, max, range: Number((max - min).toFixed(2)), note: "values within a small delta map to alarm and non-alarm colours",
      });
    }
    if (isTemperature) {
      for (const record of records) {
        if (alarm(record.rgb) && record.value < opts.temperatureAlarmMinC) {
          push("heat-alarm-healthy-temp", "error", record.tile, { value: record.value, rgb: record.rgb.join(","), alarmMinC: opts.temperatureAlarmMinC });
        }
      }
    }
    // Text contrast on the heat colour (painted by a ::before overlay that
    // axe cannot see): heat rgb at its alpha over the dark tile base.
    const base = [17, 22, 26];
    const lum = (rgb) => {
      const c = rgb.map((v) => {
        const x = v / 255;
        return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    };
    const ratio = (a, b) => {
      const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    for (const record of records) {
      const bg = record.rgb.map((v, i) => Math.round(v * record.alpha + base[i] * (1 - record.alpha)));
      for (const element of record.tile.querySelectorAll("*")) {
        if (element.children.length || !(element.textContent || "").trim() || !visible(element)) continue;
        const color = style(element).color.match(/[\d.]+/g).slice(0, 3).map(Number);
        const value = ratio(color, bg);
        if (value < opts.contrastMin) {
          push("heat-tile-contrast", "error", element, { ratio: Number(value.toFixed(2)), color: color.join(","), background: bg.join(",") });
        }
      }
    }
  }

  return { findings, counts, scanned, heat, viewport: [vw, vh], document: [docWidth, document.documentElement.scrollHeight] };
}

// axe-core result -> findings (serious/critical only; contrast separately).
function axeFindings(results) {
  const out = [];
  for (const violation of results.violations || []) {
    if (violation.id !== "color-contrast" && !["serious", "critical"].includes(violation.impact)) continue;
    const type = violation.id === "color-contrast" ? "contrast" : `axe-${violation.id}`;
    for (const node of (violation.nodes || []).slice(0, 25)) {
      const data = (node.any || []).concat(node.all || []).map((check) => check.data).find(Boolean) || {};
      out.push({
        type,
        severity: violation.id === "color-contrast" || violation.impact === "critical" ? "error" : "warn",
        selector: (node.target || []).join(" "),
        text: String(node.html || "").replace(/\s+/g, " ").slice(0, 120),
        detail: violation.id === "color-contrast"
          ? { ratio: data.contrastRatio, expected: data.expectedContrastRatio, fg: data.fgColor, bg: data.bgColor }
          : { impact: violation.impact, help: violation.help },
      });
    }
  }
  return out;
}

module.exports = { pageChecks, axeFindings };
