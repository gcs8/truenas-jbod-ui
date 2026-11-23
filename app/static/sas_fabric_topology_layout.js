/* Storage Fabric Topology: layout, trace and SVG drawing. */
(function () {
  window.SasFabricTopologyLayout = function (context) {
function wrap(text, max, whole = false) {
    return wrapWords(String(text ?? "").split(/\s+/).filter(Boolean), max, whole);
  }

function wrapWords(given, max, whole = false) {
    const words = given.flatMap((word) => (whole && word.length > max ? word.match(new RegExp(`.{1,${max}}`, "g")) : [word]));
    const lines = [];
    let line = "";
    for (const word of words) {
      const piece = word.length > max ? `${word.slice(0, max - 1)}…` : word;
      if (!line) line = piece;
      else if (line.length + 1 + piece.length <= max) line = `${line} ${piece}`;
      else {
        lines.push(line);
        line = piece;
      }
    }
    if (line) lines.push(line);
    return lines;
  }

function sizeNode(entry) {
    const parts = [entry.label, ...entry.facts];
    if (entry.phyGroups.length) parts.push(`PHYs ${entry.phyGroups.map((group) => context.phyRanges(group.phys)).join(" | ")}`);
    if (entry.parentUnknown && entry.kind !== "host") parts.push("parent link not reported");
    const longest = Math.max(...parts.map((part) => String(part).length));
    const chars = Math.min(context.NODE_MAX_CHARS, Math.max(context.NODE_MIN_CHARS, longest));
    entry.titleLines = wrap(entry.label, chars, entry.wholeTitle).length || 1;
    entry.lines = parts.flatMap((part, index) => wrap(part, chars, index === 0 && entry.wholeTitle));
    entry.w = Math.max(entry.w, Math.max(...entry.lines.map((line) => line.length)) * context.TEXT_W + 20);
    entry.h = Math.max(context.NODE_H, 14 + entry.lines.length * context.LINE_H + 8);
  }

function distinctTitles(model) {
    const title = (entry) => entry.lines.slice(0, entry.titleLines).join(" ");
    // Only what is drawn: a second HBA's row of a merged chip has no box.
    const drawn = model.nodes.filter((entry) => !entry.mergedInto);
    const generated = new Set();
    for (let pass = 0; pass < 3; pass += 1) {
      const byTitle = new Map();
      for (const entry of drawn) {
        const key = `${entry.tier}\u0000${title(entry)}`;
        if (!byTitle.has(key)) byTitle.set(key, []);
        byTitle.get(key).push(entry);
      }
      const clashes = [...byTitle.values()].filter((group) => group.length > 1);
      if (!clashes.length) return;
      for (const entry of clashes.flat()) {
        const via = model.nodeById.get(entry.controllerId)?.label;
        if (pass === 0) entry.wholeTitle = true;
        else if (pass === 1 && via && via !== entry.label) entry.label = `${entry.label} via ${via}`;
        else if (pass === 2) entry.label = `${entry.label} (${entry.id})`;
        generated.add(entry);
        sizeNode(entry);
      }
    }
    // A generated title can equal another box's title. Titles the payload
    // gave are taken first, then every box in identifier order keeps its
    // title or gets the next free number, checked against every title in
    // its tier: the payload's order changes nothing, and a real " #2" stays.
    const key = (item) => `${item.tier}\u0000${title(item)}`;
    const byId = (left, right) => (left.id < right.id ? -1 : left.id > right.id ? 1 : 0);
    const order = [...drawn.filter((entry) => !generated.has(entry)).sort(byId), ...[...generated].sort(byId)];
    const taken = new Set();
    for (const entry of order) {
      if (taken.has(key(entry))) {
        const base = entry.label;
        entry.wholeTitle = true;
        for (let number = 2; taken.has(key(entry)); number += 1) {
          entry.label = `${base} #${number}`;
          sizeNode(entry);
        }
      }
      taken.add(key(entry));
    }
  }

function groupText(group) {
    // A physical enclosure's block is headed by the enclosure itself.
    if (group.enclosure && !group.collapsed) {
      return [{ text: group.enclosure.node.label, title: true }, ...group.enclosure.node.facts.map((text) => ({ text }))];
    }
    const lines = [{ text: groupHeading(group), title: true }];
    const summary = pathSummary(group.summary);
    if (group.collapsed) {
      lines.push({ text: `${group.summary.withDisk} with a disk · ${group.summary.empty} empty · ${group.summary.unknown} not read` });
      lines.push({ text: summary || "no path state in payload" });
    } else if (summary) {
      lines.push({ text: summary });
    }
    return lines;
  }

function groupSize(group) {
    const texts = groupText(group);
    // An expandable group's control sits at its top left in both states,
    // its text below the control.
    group.textTop = group.expandable ? context.TOGGLE_H : 0;
    if (group.collapsed) {
      const width = Math.max(context.SUMMARY_W, ...texts.map((line) => line.text.length * context.TEXT_W + 20));
      group.lines = texts.flatMap((line) => wrap(line.text, Math.floor((width - 20) / context.TEXT_W)).map((text) => ({ ...line, text })));
      group.headH = 10 + group.textTop + group.lines.length * context.LINE_H;
      return [width, group.headH + 10];
    }
    if (group.rows) {
      // The enclosure view's rows: columns as the view has them, with the
      // profile's column groups set apart.
      const columns = Math.max(...group.rows.map((row) => Math.max(...row.map((cell) => cell.column)) + 1));
      const breaks = context.list(group.breakColumns).filter((column) => column < columns);
      group.pitch = group.cellWidth + context.PHYS_GAP;
      group.breaks = breaks;
      group.cellH = group.cellWidth >= context.PHYS_CELL_NAMED ? context.PHYS_CELL_TALL : context.PHYS_CELL_H;
      // A board's chips stand in a strip. A board of two bay rows has it
      // between them (a row of connectors on each side of it). Any other
      // grid is drawn whole, as the enclosure view has it, with the strip
      // under it. Where every row is next to the strip ("wired"), each chip's
      // bus runs along that edge of it and each bay's ports stand on it.
      const chips = context.list(group.chips);
      const count = group.rows.length;
      group.split = chips.length && count === 2 ? 1 : count;
      const [above, below] = [group.split, count - group.split];
      group.wired = chips.length > 0 && above <= 1 && below <= 1;
      const zone = 2 * context.BUS_PAD + Math.max(0, chips.length - 1) * context.BUS_STEP;
      group.zoneTop = chips.length ? (above ? zone : 6) : 0;
      group.zoneBottom = chips.length ? (below ? zone : 6) : 0;
      group.stripH = chips.length ? group.zoneTop + chips[0].h + group.zoneBottom : 0;
      group.cellsW = columns * group.pitch - context.PHYS_GAP + breaks.length * context.GROUP_GAP;
      group.gridW = Math.max(group.cellsW, chips.reduce((sum, chip) => sum + chip.w, 0) + Math.max(0, chips.length - 1) * context.CHIP_GAP);
      group.gridH = count * (group.cellH + context.PHYS_GAP) - context.PHYS_GAP + (chips.length ? group.stripH + context.PHYS_GAP : 0);
      const width = Math.max(context.NODE_W, group.gridW + 20);
      group.lines = texts.flatMap((line) => wrap(line.text, Math.floor((width - 20) / context.TEXT_W)).map((text) => ({ ...line, text })));
      group.headH = 12 + group.textTop + group.lines.length * context.LINE_H;
      group.captionH = group.caption ? context.CAPTION_H : 0;
      return [width, group.headH + group.gridH + 12 + group.captionH];
    }
    const count = group.cells.length;
    const pitch = group.cellWidth + (context.CELL - context.CELL_BOX);
    const perRow = pitch === context.CELL ? context.CELLS_PER_ROW : Math.max(1, Math.floor((context.CELLS_PER_ROW * context.CELL) / pitch));
    const columns = Math.max(1, Math.min(perRow, count));
    const rows = Math.max(1, Math.ceil(count / perRow));
    group.perRow = perRow;
    group.pitch = pitch;
    // An unlinked or logical group is wide enough to say so in full.
    const minimum = Math.max(context.NODE_W, texts[0].text.length * context.TEXT_W + 20);
    const width = Math.max(minimum, columns * pitch + 16);
    group.lines = texts.flatMap((line) => wrap(line.text, Math.floor((width - 20) / context.TEXT_W)).map((text) => ({ ...line, text })));
    group.headH = 12 + group.textTop + group.lines.length * context.LINE_H;
    return [width, group.headH + rows * context.CELL + 10];
  }

function centreOf(item) {
    return item.x + item.w / 2;
  }

function placeRow(items, desired, gap, lo, hi) {
    const offsets = [];
    let offset = 0;
    for (const item of items) {
      offsets.push(offset);
      offset += item.w + gap;
    }
    const blocks = [];
    items.forEach((item, index) => {
      blocks.push({ sum: desired[index] - offsets[index], count: 1, start: index });
      while (blocks.length > 1) {
        const last = blocks[blocks.length - 1];
        const previous = blocks[blocks.length - 2];
        if (previous.sum / previous.count <= last.sum / last.count) break;
        previous.sum += last.sum;
        previous.count += last.count;
        blocks.pop();
      }
    });
    const lastIndex = items.length - 1;
    const upper = Math.max(lo, hi - offsets[lastIndex] - items[lastIndex].w);
    for (const [index, block] of blocks.entries()) {
      const end = index + 1 < blocks.length ? blocks[index + 1].start : items.length;
      const value = Math.min(upper, Math.max(lo, block.sum / block.count));
      for (let position = block.start; position < end; position += 1) items[position].x = value + offsets[position];
    }
  }

function linkWidth(link) {
    return link.labelLines.length ? Math.max(...link.labelLines.map((line) => line.length)) * context.TEXT_W + 14 : context.JUNCTION;
  }

function linkHeight(link) {
    return link.labelLines.length ? link.labelLines.length * 15 + 5 : context.JUNCTION;
  }

function properCross(p, q, r, s) {
    const d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0]);
    if (Math.abs(d) < 1e-9) return false;
    const t = ((r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])) / d;
    const u = ((r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])) / d;
    return t > 0.01 && t < 0.99 && u > 0.01 && u < 0.99;
  }

function tick(work, key, count = 1) {
    work[key] = (work[key] || 0) + count;
    work.total += count;
  }

function lowerBound(values, value, key = (item) => item) {
    let lo = 0;
    let hi = values.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (key(values[mid]) < value) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

function segmentsOf(links) {
    const horizontals = [];
    const verticals = [];
    const others = [];
    let order = 0;
    for (const link of links) {
      const points = link.points || [];
      for (let index = 0; index + 1 < points.length; index += 1) {
        const [a, b] = [points[index], points[index + 1]];
        const segment = { link, a, b, order: order += 1 };
        if (a[1] === b[1] && a[0] !== b[0]) horizontals.push(segment);
        else if (a[0] === b[0] && a[1] !== b[1]) verticals.push(segment);
        else if (a[0] !== b[0] || a[1] !== b[1]) others.push(segment);
      }
    }
    return { horizontals, verticals, others };
  }

function meetingPairs(segments, work) {
    const events = [];
    for (const segment of segments.verticals) {
      const [top, bottom] = [Math.min(segment.a[1], segment.b[1]), Math.max(segment.a[1], segment.b[1])];
      events.push({ y: top, kind: 0, segment }, { y: bottom, kind: 2, segment });
    }
    for (const segment of segments.horizontals) events.push({ y: segment.a[1], kind: 1, segment });
    events.sort((left, right) => left.y - right.y || left.kind - right.kind);
    tick(work, "crossingWork", events.length * Math.max(1, Math.ceil(Math.log2(events.length + 1))));
    const active = [];
    const pairs = [];
    const xOf = (segment) => segment.a[0];
    for (const event of events) {
      const { segment } = event;
      if (event.kind === 0) {
        active.splice(lowerBound(active, xOf(segment), xOf), 0, segment);
      } else if (event.kind === 2) {
        for (let index = lowerBound(active, xOf(segment), xOf); index < active.length; index += 1) {
          if (active[index] === segment) {
            active.splice(index, 1);
            break;
          }
        }
      } else {
        const [left, right] = [Math.min(segment.a[0], segment.b[0]), Math.max(segment.a[0], segment.b[0])];
        for (let index = lowerBound(active, left, xOf); index < active.length && xOf(active[index]) <= right; index += 1) {
          tick(work, "crossingWork");
          if (active[index].link !== segment.link) pairs.push([segment, active[index]]);
        }
      }
    }
    return pairs;
  }

function countCrossings(links, work) {
    const segments = segmentsOf(links);
    let count = 0;
    for (const [horizontal, vertical] of meetingPairs(segments, work)) {
      const [first, second] = horizontal.order < vertical.order ? [horizontal, vertical] : [vertical, horizontal];
      if (properCross(first.a, first.b, second.a, second.b)) count += 1;
    }
    // A piece that is neither horizontal nor vertical (routes never make
    // one) is checked against every other piece.
    const all = [...segments.horizontals, ...segments.verticals, ...segments.others].sort((left, right) => left.order - right.order);
    for (const other of segments.others) {
      tick(work, "crossingWork", all.length);
      for (const segment of all) {
        if (segment.link === other.link || (segments.others.includes(segment) && segment.order <= other.order)) continue;
        const [first, second] = segment.order < other.order ? [segment, other] : [other, segment];
        if (properCross(first.a, first.b, second.a, second.b)) count += 1;
      }
    }
    return count;
  }

function crossingMarks(links, work) {
    const marks = [];
    const pairs = meetingPairs(segmentsOf(links), work).sort(([h1, v1], [h2, v2]) => h1.order - h2.order || v1.order - v2.order);
    for (const [{ link: under, a, b }, { link: over, a: c, b: d }] of pairs) {
      const [left, right] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])];
      const [top, bottom] = [Math.min(c[1], d[1]), Math.max(c[1], d[1])];
      if (c[0] > left + 1 && c[0] < right - 1 && a[1] > top + 1 && a[1] < bottom - 1) marks.push({ x: c[0], y: a[1], over: over.id, under: under.id });
    }
    return marks;
  }

function orderCandidates(rows, links, rowIndex, work, sweeps = 8) {
    const parents = new Map();
    const children = new Map();
    for (const link of links) {
      if (!parents.has(link.target)) parents.set(link.target, []);
      if (!children.has(link.source)) children.set(link.source, []);
      parents.get(link.target).push(link.source);
      children.get(link.source).push(link.target);
    }
    let order = rows.map((row) => row.items.slice());
    const snapshot = () => order.map((items) => items.slice());
    const candidates = [snapshot()];
    tick(work, "orderWork", links.length);
    const position = () => {
      const result = new Map();
      for (const items of order) items.forEach((item, index) => result.set(item.id, (index + 0.5) / items.length));
      tick(work, "orderWork", result.size);
      return result;
    };
    for (let iteration = 0; iteration < sweeps; iteration += 1) {
      const down = iteration % 2 === 0;
      const sequence = down ? order.map((_, index) => index).slice(1) : order.map((_, index) => index).reverse().slice(1);
      for (const r of sequence) {
        const at = position();
        const neighbours = down ? parents : children;
        const key = new Map(order[r].map((item) => {
          const ids = context.list(neighbours.get(item.id)).filter((id) => (down ? rowIndex.get(id) < r : rowIndex.get(id) > r));
          tick(work, "orderWork", 1 + context.list(neighbours.get(item.id)).length);
          return [item.id, ids.length ? ids.reduce((sum, id) => sum + at.get(id), 0) / ids.length : at.get(item.id)];
        }));
        tick(work, "orderWork", order[r].length * Math.max(1, Math.ceil(Math.log2(order[r].length + 1))));
        order[r] = order[r].slice().sort((left, right) => key.get(left.id) - key.get(right.id));
      }
      candidates.push(snapshot());
    }
    return candidates;
  }

function placeRows(rows, links, rowIndex, available, work) {
    const parents = new Map();
    const children = new Map();
    for (const link of links) {
      if (rowIndex.get(link.target) <= rowIndex.get(link.source)) continue;
      if (!parents.has(link.target)) parents.set(link.target, []);
      if (!children.has(link.source)) children.set(link.source, []);
      parents.get(link.target).push(link.source);
      children.get(link.source).push(link.target);
    }
    const byId = new Map(rows.flatMap((row) => row.items.map((item) => [item.id, item])));
    const rowWidth = (row) => row.items.reduce((sum, item) => sum + item.w, 0) + Math.max(0, row.items.length - 1) * context.H_GAP;
    const content = Math.max(context.NODE_W, ...rows.map(rowWidth), ...rows.map((row) => row.bandWidth || 0));
    const width = Math.max(available, context.LEFT + content + context.MARGIN);
    const lo = context.LEFT;
    const hi = width - context.MARGIN;
    let anchor = 0;
    rows.forEach((row, index) => {
      if (rowWidth(row) >= rowWidth(rows[anchor])) anchor = index;
    });
    const spread = (row) => {
      const extra = Math.max(0, hi - lo - rowWidth(row));
      const step = row.items.length ? extra / row.items.length : 0;
      let x = lo + step / 2;
      return row.items.map((item) => {
        const left = x;
        x += item.w + context.H_GAP + step;
        return left;
      });
    };
    for (const row of rows) spread(row).forEach((x, index) => { row.items[index].x = x; });
    const place = (r, wanted, bounds = [lo, hi]) => {
      const row = rows[r];
      if (!row.items.length) return;
      tick(work, "placeWork", row.items.length);
      placeRow(row.items, row.items.map((item) => wanted(item) ?? item.x), context.H_GAP, bounds[0], bounds[1]);
    };
    for (let r = anchor + 1; r < rows.length; r += 1) {
      place(r, (item) => {
        const ids = context.list(parents.get(item.id)).filter((id) => byId.has(id));
        return ids.length ? ids.reduce((sum, id) => sum + centreOf(byId.get(id)), 0) / ids.length - item.w / 2 : null;
      });
    }
    const overChildren = (item) => {
      const ids = context.list(children.get(item.id)).filter((id) => byId.has(id));
      if (!ids.length) return null;
      const left = Math.min(...ids.map((id) => byId.get(id).x));
      const right = Math.max(...ids.map((id) => byId.get(id).x + byId.get(id).w));
      return (left + right) / 2 - item.w / 2;
    };
    for (let r = rows.length - 2; r >= 0; r -= 1) place(r, overChildren);
    tick(work, "placeWork", links.length);
    if (!spanViolations(links, byId, rowIndex)) return width;
    tick(work, "placeWork", links.length);
    for (let r = rows.length - 2; r >= 0; r -= 1) place(r, overChildren, [-Infinity, Infinity]);
    const items = [...byId.values()];
    const shift = Math.max(0, lo - Math.min(...items.map((item) => item.x)));
    for (const item of items) item.x += shift;
    return Math.max(width, Math.max(...items.map((item) => item.x + item.w)) + context.MARGIN);
  }

function spanViolations(links, byId, rowIndex) {
    const spans = new Map();
    for (const link of links) {
      if (rowIndex.get(link.target) <= rowIndex.get(link.source)) continue;
      const child = byId.get(link.target);
      const span = spans.get(link.source) || [Infinity, -Infinity];
      spans.set(link.source, [Math.min(span[0], child.x), Math.max(span[1], child.x + child.w)]);
    }
    let count = 0;
    for (const [id, [left, right]] of spans) {
      const centre = centreOf(byId.get(id));
      if (centre < left - 0.5 || centre > right + 0.5) count += 1;
    }
    return count;
  }

function routeLinks(model, rows, links, rowIndex, width) {
    const { work } = model;
    tick(work, "routePasses");
    const byId = new Map(rows.flatMap((row) => row.items.map((item) => [item.id, item])));
    const gaps = rows.map(() => ({ horizontals: [], labels: [], verticals: [] }));
    // Each item's links out and in, collected once.
    const outOf = new Map();
    const inTo = new Map();
    for (const link of links) {
      if (!outOf.has(link.source)) outOf.set(link.source, []);
      if (!inTo.has(link.target)) inTo.set(link.target, []);
      outOf.get(link.source).push(link);
      inTo.get(link.target).push(link);
    }
    tick(work, "routeWork", links.length + byId.size);
    // Exits: spread on the parent's bottom edge in the order of their targets.
    const exits = new Map();
    const arrivals = new Map();
    for (const item of byId.values()) {
      const out = context.list(outOf.get(item.id)).sort((a, b) => centreOf(byId.get(a.target)) - centreOf(byId.get(b.target)));
      out.forEach((link, index) => exits.set(link, item.x + item.w * (index + 1) / (out.length + 1)));
      const into = context.list(inTo.get(item.id)).sort((a, b) => centreOf(byId.get(a.source)) - centreOf(byId.get(b.source)));
      arrivals.set(item.id, into);
      tick(work, "routeWork", (out.length + into.length) * Math.max(1, Math.ceil(Math.log2(out.length + into.length + 1))));
    }
    // The exits that leave into each gap, sorted, for the drop and corridor searches.
    const exitsInto = rows.map(() => []);
    for (const link of links) {
      const below = rowIndex.get(link.source) + 1;
      if (below < rows.length) exitsInto[below].push(exits.get(link));
    }
    for (const xs of exitsInto) xs.sort((a, b) => a - b);
    // Drops: spread on the child's top edge, kept 10 px from every exit in the
    // same gap and from the labels of other drops.
    // Stacked enclosure blocks: a link drops straight down under its own exit
    // where the child's top edge is there, and a straight link takes no track.
    const straight = Boolean(model.physical?.stacked);
    const drops = new Map();
    rows.forEach((row, r) => {
      const exitXs = exitsInto[r];
      // The drops placed so far in this gap, sorted by x, and the widest half label.
      const taken = [];
      let widest = 0;
      const clear = (x, half, own = null) => {
        const at = lowerBound(exitXs, x);
        tick(work, "dropWork", 1 + Math.ceil(Math.log2(exitXs.length + taken.length + 2)));
        // A drop under its own exit is one straight line: only other exits count.
        const near = (index) => index >= 0 && index < exitXs.length && Math.abs(exitXs[index] - x) < context.TRACK && !(own !== null && x === own && exitXs[index] === own);
        if (own !== null ? near(at) || near(at - 1) || near(at + 1) : (at < exitXs.length && exitXs[at] - x < context.TRACK) || (at > 0 && x - exitXs[at - 1] < context.TRACK)) return false;
        const reach = Math.max(context.TRACK, widest + 4, half + 4);
        for (let index = lowerBound(taken, x - reach, (other) => other.x); index < taken.length && taken[index].x <= x + reach; index += 1) {
          tick(work, "dropWork");
          const other = taken[index];
          if (Math.abs(other.x - x) < Math.max(context.TRACK, other.half + 4, half + 4)) return false;
        }
        return true;
      };
      for (const item of row.items) {
        const into = arrivals.get(item.id) || [];
        into.forEach((link, index) => {
          const exit = exits.get(link);
          const under = straight && rowIndex.get(link.source) === r - 1 && exit >= item.x + 10 && exit <= item.x + item.w - 10;
          const ideal = under ? exit : item.x + item.w * (index + 1) / (into.length + 1);
          const half = link.labelLines.length ? linkWidth(link) / 2 : context.JUNCTION / 2;
          let best = ideal;
          for (let step = 0; step <= item.w; step += 2) {
            const options = [ideal - step, ideal + step].filter((x) => x >= item.x + 10 && x <= item.x + item.w - 10);
            const found = options.find((x) => clear(x, half, under ? exit : null));
            if (found !== undefined) {
              best = found;
              break;
            }
          }
          drops.set(link, best);
          taken.splice(lowerBound(taken, best, (other) => other.x), 0, { x: best, half });
          widest = Math.max(widest, half);
        });
      }
    });
    // Vertical sizes: tracks per gap, label band, row heights.
    const spans = links.map((link) => {
      const from = rowIndex.get(link.source);
      const to = rowIndex.get(link.target);
      return { link, from, to, skip: to > from + 1, up: to <= from };
    });
    for (const span of spans) {
      const first = Math.min(span.from + 1, rows.length - 1);
      span.straight = straight && !span.skip && !span.up && exits.get(span.link) === drops.get(span.link);
      if (!span.straight) gaps[first].horizontals.push({ span, part: "first" });
      if (span.skip || span.up) gaps[span.to].horizontals.push({ span, part: "last" });
      gaps[span.to].labels.push(span.link);
    }
    let y = context.MARGIN + 8;
    rows.forEach((row, r) => {
      if (r > 0) {
        const gap = gaps[r];
        gap.top = y;
        gap.trackTop = y + context.GAP_PAD;
        const labelH = Math.max(0, ...gap.labels.map(linkHeight));
        y += context.GAP_PAD + gap.horizontals.length * context.TRACK + context.LABEL_GAP + labelH + context.LABEL_CLEAR;
        y = Math.max(y, gap.top + 32);
        // Two stacked enclosure blocks with nothing between them sit together.
        const tight = !gap.horizontals.length && !gap.labels.length && row.tier.stack && rows[r - 1].tier.stack;
        if (tight) y = gap.top + context.STACK_GAP;
        // Room the boards' own cables need above the first board.
        if (row.tier.gapAbove) y = Math.max(y, gap.top + row.tier.gapAbove);
      }
      row.y = y;
      const height = Math.max(row.items.length ? Math.max(...row.items.map((item) => item.h)) : 36, row.labelH || 0, row.bandH || 0);
      row.height = height;
      for (const item of row.items) item.y = y;
      row.tier.y = y;
      row.tier.h = height;
      y += height;
    });
    const height = y + context.MARGIN + (spans.some((span) => span.up) ? 40 : 0);
    // A "not read" or "not collected" band and its note: placed where no
    // straight drop has to pass, and an obstacle for every corridor.
    rows.forEach((row, r) => {
      row.band = null;
      if (!row.bandLines) return;
      const avoid = spans
        .filter((span) => (span.skip && span.from < r && span.to > r) || (span.up && span.from < r))
        .flatMap((span) => (span.up ? [width - context.MARGIN - 4] : [exits.get(span.link), drops.get(span.link)]));
      const fits = (x) => x + row.bandW <= width - context.MARGIN && avoid.every((v) => v < x - 16 || v > x + row.bandW + 16);
      const start = model.bandX ?? context.LEFT;
      const x = [start, ...avoid.map((v) => v + 17).filter((v) => v >= start).sort((a, b) => a - b)].find(fits) ?? start;
      row.band = { id: `band:${row.tier.id}`, x, y: row.y, w: row.bandW, h: row.height };
    });
    // Corridors for links that skip rows: a free x through every row and label
    // band in between, 10 px from every other vertical there.
    const corridors = [];
    const dropsInto = rows.map(() => []);
    for (const link of links) dropsInto[rowIndex.get(link.target)]?.push(drops.get(link));
    const verticalsIn = (r) => [...(exitsInto[r] || []), ...(dropsInto[r] || [])];
    for (const span of spans) {
      if (!span.skip && !span.up) continue;
      const exit = exits.get(span.link);
      const drop = drops.get(span.link);
      const top = span.up ? span.from + 1 : span.from + 1;
      const bottom = span.up ? rows.length : span.to;
      const blocked = [];
      for (let r = top; r < bottom; r += 1) {
        for (const item of rows[r]?.items || []) blocked.push([item.x - 12, item.x + item.w + 12]);
        if (rows[r]?.band) blocked.push([rows[r].band.x - 12, rows[r].band.x + rows[r].band.w + 12]);
        for (const link of gaps[r]?.labels || []) {
          const x = drops.get(link);
          const half = linkWidth(link) / 2;
          blocked.push([x - half - 8, x + half + 8]);
        }
      }
      for (let r = top; r <= Math.min(bottom, rows.length - 1); r += 1) {
        for (const x of verticalsIn(r)) if (x !== exit && x !== drop) blocked.push([x - context.TRACK, x + context.TRACK]);
      }
      for (const other of corridors) if (other.top <= bottom && other.bottom >= top) blocked.push([other.x - context.TRACK, other.x + context.TRACK]);
      const target = span.up ? width - context.MARGIN - 4 : (exit + drop) / 2;
      // Never through the tier label column on the left.
      const free = (candidate) => {
        tick(work, "corridorWork", 1 + blocked.length);
        return candidate >= context.LEFT + 4 && candidate <= width - 4 && blocked.every(([left, right]) => candidate <= left || candidate >= right);
      };
      // Straight down under the drop or the exit when that is free: one bend fewer.
      let x = span.up ? null : [drop, exit].find(free) ?? null;
      for (let step = 0; step <= width && x === null; step += 2) {
        for (const candidate of [target - step, target + step]) {
          if (free(candidate)) {
            x = candidate;
            break;
          }
        }
      }
      span.corridor = x === null ? width - 4 : x;
      corridors.push({ x: span.corridor, top, bottom });
    }
    // Tracks: one per horizontal, ordered in each gap to cross as little as possible.
    rows.forEach((row, r) => {
      const gap = gaps[r];
      if (!gap.horizontals.length) return;
      const ends = gap.horizontals.map((piece) => {
        const { span, part } = piece;
        const into = part === "first" ? exits.get(span.link) : span.corridor;
        const out = part === "first" && !span.skip && !span.up ? drops.get(span.link) : part === "first" ? span.corridor : drops.get(span.link);
        return { piece, into, out, lo: Math.min(into, out), hi: Math.max(into, out) };
      });
      // cost(a, b): crossings when track a lies above track b. Inserting an
      // end at each position in turn changes the total by one pair, so each
      // insertion is linear and a gap is quadratic in its lines.
      const cost = (a, b) => {
        tick(model.work, "trackCosts");
        return (b.into > a.lo && b.into < a.hi ? 1 : 0) + (a.out > b.lo && a.out < b.hi ? 1 : 0);
      };
      const ordered = [];
      for (const end of ends.sort((a, b) => a.lo - b.lo)) {
        let total = 0;
        for (const other of ordered) total += cost(end, other);
        let bestIndex = 0;
        let bestCost = total;
        for (let index = 0; index < ordered.length; index += 1) {
          total += cost(ordered[index], end) - cost(end, ordered[index]);
          if (total < bestCost) {
            bestCost = total;
            bestIndex = index + 1;
          }
        }
        ordered.splice(bestIndex, 0, end);
      }
      ordered.forEach((end, index) => {
        end.piece.y = gap.trackTop + index * context.TRACK;
        end.piece.span[end.piece.part] = end.piece.y;
      });
    });
    // Points and labels.
    for (const span of spans) {
      const { link } = span;
      const parent = byId.get(link.source);
      const child = byId.get(link.target);
      const exit = exits.get(link);
      const drop = drops.get(link);
      const start = parent.y + parent.h;
      if (span.straight) {
        link.points = [[exit, start], [drop, child.y]];
      } else if (span.skip || span.up) {
        link.points = [[exit, start], [exit, span.first], [span.corridor, span.first], [span.corridor, span.last], [drop, span.last], [drop, child.y]];
      } else {
        link.points = [[exit, start], [exit, span.first], [drop, span.first], [drop, child.y]];
      }
      // Drop repeated points and the middle of three in a straight line.
      const points = link.points.filter((point, index, all) => index === 0 || point[0] !== all[index - 1][0] || point[1] !== all[index - 1][1]);
      link.points = points.filter((point, index, all) => {
        if (index === 0 || index === all.length - 1) return true;
        const [a, b] = [all[index - 1], all[index + 1]];
        return !((a[0] === point[0] && point[0] === b[0]) || (a[1] === point[1] && point[1] === b[1]));
      });
      const w = linkWidth(link);
      const h = linkHeight(link);
      link.labelBox = { x: drop - w / 2, y: child.y - context.LABEL_CLEAR - h, w, h };
    }
    return height;
  }

function tierLines(tier, narrow = false) {
    // "level 1" stays on one line: a number never sits alone on a line.
    const words = String(tier.label ?? "").split(/\s+/).filter(Boolean);
    const joined = words.reduce((all, word) => {
      if (/^[0-9]+$/.test(word) && all.length) all[all.length - 1] = `${all[all.length - 1]} ${word}`;
      else all.push(word);
      return all;
    }, []);
    // Beside boards the label column is narrower: shorter lines.
    tier.labelLines = wrapWords(joined, narrow ? 13 : context.TIER_LABEL_CHARS);
    tier.noteLines = wrap(tier.note, narrow ? 17 : context.TIER_NOTE_CHARS);
    return 10 + (tier.labelLines.length + tier.noteLines.length) * 17;
  }

function chainsOf(model) {
    const chainOf = new Map();
    const controllers = model.nodes.filter((entry) => entry.kind === "controller");
    controllers.forEach((entry) => chainOf.set(entry.id, entry.id));
    const pending = controllers.map((entry) => entry.id);
    while (pending.length) {
      const id = pending.shift();
      for (const link of context.list(model.outgoing.get(id))) {
        if (!chainOf.has(link.target)) {
          chainOf.set(link.target, chainOf.get(id));
          pending.push(link.target);
        }
      }
    }
    return chainOf;
  }

function placeStacked(model, rows, links, rowIndex, available) {
    const blocks = model.groups.filter((group) => group.stack);
    const blockW = Math.max(...blocks.map((group) => group.w));
    const chainOf = chainsOf(model);
    const chains = model.nodes.filter((entry) => entry.kind === "controller").map((entry) => entry.id);
    // Columns: evenly spread over the blocks' width, and at least a box and
    // a gap apart, so every row's boxes stand in the same columns.
    const chained = model.nodes.filter((entry) => !entry.physical && chainOf.has(entry.id));
    const pitch = Math.max(blockW / (chains.length + 1), Math.max(0, ...chained.map((entry) => entry.w)) + context.H_GAP);
    const share = (id) => {
      const index = chains.indexOf(chainOf.get(id));
      return index < 0 ? 0.5 : 0.5 + ((index - (chains.length - 1) / 2) * pitch) / blockW;
    };
    for (const link of links) {
      link.rail = null;
      if (link.bundle && model.groupById.get(link.target)?.stack && rowIndex.get(link.target) > rowIndex.get(link.source) + 1) {
        link.rail = { side: share(link.source) <= 0.5 ? "left" : "right", offset: 0 };
      }
    }
    // Rails on each side: the one into the highest block nearest the blocks.
    let room = 0;
    for (const side of ["left", "right"]) {
      const rails = links.filter((link) => link.rail?.side === side).sort((a, b) => rowIndex.get(a.target) - rowIndex.get(b.target));
      const half = Math.max(0, ...rails.map((link) => linkWidth(link) / 2));
      rails.forEach((link, index) => {
        link.rail.offset = half + context.RAIL_CLEAR + index * context.RAIL_STEP;
      });
      if (rails.length) room = Math.max(room, 2 * half + context.RAIL_CLEAR + (rails.length - 1) * context.RAIL_STEP + 8);
    }
    const rowWidth = (row) => row.items.reduce((sum, item) => sum + item.w, 0) + Math.max(0, row.items.length - 1) * context.H_GAP;
    // The boxes of one HBA's column in a row are as wide as the widest there.
    for (const row of rows) {
      const boxes = row.items.filter((item) => !item.stack && chainOf.has(item.id));
      const widest = Math.max(0, ...boxes.map((item) => item.w));
      for (const item of boxes) item.w = widest;
    }
    const content = Math.max(blockW + 2 * room, ...rows.map(rowWidth), ...rows.map((row) => row.bandWidth || 0));
    const width = Math.max(available, context.LEFT + content + context.MARGIN);
    // The tier labels stay beside the drawing: labels and drawing are centred
    // in a wide box together, not the labels at its far left edge.
    const shift = Math.max(0, Math.round((width - (context.LEFT + content + context.MARGIN)) / 2));
    model.labelX = context.MARGIN + shift;
    const lo = context.LEFT + shift;
    const hi = lo + content;
    const blockX = Math.round(lo + (content - blockW) / 2);
    for (const row of rows) {
      const wanted = new Map();
      for (const item of row.items) {
        if (item.stack) {
          item.w = blockW;
          wanted.set(item, blockX);
        } else {
          wanted.set(item, blockX + blockW * share(item.id) - item.w / 2);
        }
      }
      row.items.sort((left, right) => wanted.get(left) + left.w / 2 - wanted.get(right) - right.w / 2);
      if (row.items.length) placeRow(row.items, row.items.map((item) => wanted.get(item)), context.H_GAP, lo, hi);
      for (const item of row.items) item.x = Math.round(item.x);
      tick(model.work, "placeWork", row.items.length);
    }
    return width;
  }

function routeRails(model, rails, byId) {
    for (const link of rails) {
      const source = byId.get(link.source);
      const group = byId.get(link.target);
      const left = link.rail.side === "left";
      const startX = left ? source.x : source.x + source.w;
      const startY = Math.round(source.y + source.h / 2);
      const railX = Math.round(left ? group.x - link.rail.offset : group.x + group.w + link.rail.offset);
      const gridTop = group.headAt === "bottom" ? group.y + 10 : group.y + group.headH;
      const endY = Math.round(gridTop + (group.gridH || group.h - group.headH) / 2);
      link.points = [[startX, startY], [railX, startY], [railX, endY], [left ? group.x : group.x + group.w, endY]];
      const w = linkWidth(link);
      const h = linkHeight(link);
      link.labelBox = { x: railX - w / 2, y: endY - context.RAIL_CLEAR - h, w, h };
      tick(model.work, "routeWork");
    }
  }

function placeCells(model) {
    const hbas = model.nodes.filter((entry) => entry.kind === "controller").map((entry) => entry.label);
    for (const group of model.groups) {
      if (!group.rows) {
        group.cells.forEach((bay, index) => {
          bay.x = group.x + 8 + (index % group.perRow) * group.pitch;
          bay.y = group.y + group.headH + Math.floor(index / group.perRow) * context.CELL;
        });
        continue;
      }
      // The head is above the bays, or below them on the last stacked block.
      const below = group.headAt === "bottom";
      const top = below ? group.y + 10 : group.y + group.headH;
      group.gridTop = top;
      if (below) group.textTop = group.gridH + 12;
      const left = group.x + Math.round((group.w - group.gridW) / 2);
      const cellsLeft = left + Math.round((group.gridW - group.cellsW) / 2);
      const pitchY = group.cellH + context.PHYS_GAP;
      const chips = context.list(group.chips);
      group.gridLeft = left;
      group.stripTop = top + group.split * pitchY;
      const lowerTop = chips.length ? group.stripTop + group.stripH + context.PHYS_GAP : group.stripTop;
      chips.forEach((chip, index) => {
        chip.x = chips.length > 1 ? Math.round(left + ((group.gridW - chip.w) * index) / (chips.length - 1)) : Math.round(left + (group.gridW - chip.w) / 2);
        chip.y = group.stripTop + group.zoneTop;
      });
      const chipAt = (port) => chips.indexOf(context.chipOfNode(model, port.expanderId));
      // Each chip's bus: a line along the strip's edge on every side that has
      // bays, joined to the chip. Where the grid is whole above the strip,
      // the line stands under the grid for a chip some disk is attached to.
      // Its place is set here and its shape below, once the ports have theirs.
      const buses = new Map();
      chips.forEach((chip, index) => {
        const used = group.wired || group.bays.some((bay) => bay.ports.some((port) => chipAt(port) === index));
        buses.set(chip.id, {
          top: group.split && used ? group.stripTop + context.BUS_PAD + index * context.BUS_STEP : null,
          bottom: group.rows.length > group.split ? group.stripTop + group.stripH - context.BUS_PAD - index * context.BUS_STEP : null,
        });
      });
      // One mark per path, grouped by HBA: each HBA has as many places in a
      // cell as the most paths it has to one bay of the block, so the marks
      // of one HBA line up down the block.
      const perHba = Math.max(1, ...group.bays.flatMap((bay) => hbas.map((name) => bay.ports.filter((port) => port.controller === name).length)));
      const slots = Math.max(1, hbas.length * perHba, ...group.bays.map((bay) => bay.ports.length));
      group.rows.forEach((row, r) => row.forEach(({ bay, column }) => {
        bay.x = cellsLeft + column * group.pitch + group.breaks.filter((at) => at <= column).length * context.GROUP_GAP;
        bay.y = r < group.split ? top + r * pitchY : lowerTop + (r - group.split) * pitchY;
        bay.h = group.cellH;
        bay.spread = true;
        // A bay under the strip has its ports at its top edge, next to it.
        bay.flip = group.wired && r >= group.split;
        // The device name as the operator says it: "multipath/disk45" is disk45.
        // Only where it fits whole: a long by-id name cut to a few letters says nothing.
        const said = context.deviceShort(bay.deviceName);
        bay.short = group.cellH === context.PHYS_CELL_TALL && said && said.length * context.CHAR_W <= bay.w ? said : "";
        bay.tag = group.cellH === context.PHYS_CELL_TALL ? bay.protocol : "";
        const used = new Set();
        bay.ports.forEach((port, index) => {
          const owner = hbas.indexOf(port.controller);
          let slot = owner < 0 ? -1 : owner * perHba + bay.ports.slice(0, index).filter((other) => other.controller === port.controller).length;
          if (slot < 0 || slot >= slots || used.has(slot)) slot = [...Array(slots).keys()].find((at) => !used.has(at)) ?? index;
          used.add(slot);
          const chip = chips[chipAt(port)];
          const bus = group.wired && chip ? buses.get(chip.id) : null;
          port.x = Math.round(bay.x + (bay.w * (slot + 1)) / (slots + 1));
          port.y = bay.flip ? bay.y + 11 : bay.y + bay.h - 11;
          port.busY = bus ? (bay.flip ? bus.bottom : bus.top) : null;
          port.flip = bay.flip;
          port.hitW = Math.max(12, Math.floor(bay.w / (slots + 1)));
        });
      }));
      // Chips known only from their disks stand under the ports that name
      // them, in their order, as near as their widths allow.
      if (group.wired && chips.length && chips.every((chip) => chip.reported)) {
        const wanted = chips.map((chip, index) => {
          const xs = group.bays.flatMap((bay) => bay.ports.filter((port) => chipAt(port) === index).map((port) => port.x));
          return xs.length ? xs.reduce((sum, x) => sum + x, 0) / xs.length - chip.w / 2 : chip.x;
        });
        placeRow(chips, wanted, context.CHIP_GAP, left, left + group.gridW);
        for (const chip of chips) chip.x = Math.round(chip.x);
      }
      chips.forEach((chip, index) => {
        const link = model.linkById.get(chip.busId);
        const bus = buses.get(chip.id);
        const centre = Math.round(chip.x + chip.w / 2);
        // A chip known only from its disks has a bus as far as the ports
        // that name it: nothing is said of the other bays.
        const mine = chip.reported && group.wired ? group.bays.flatMap((bay) => bay.ports.filter((port) => chipAt(port) === index).map((port) => port.x)) : null;
        const span = mine ? [Math.min(centre, ...mine), Math.max(centre, ...mine)] : [cellsLeft + 3, cellsLeft + group.cellsW - 3];
        const shape = [];
        if (bus.top !== null) shape.push([[span[0], bus.top], [span[1], bus.top]], [[centre, chip.y], [centre, bus.top]]);
        if (bus.bottom !== null) shape.push([[span[0], bus.bottom], [span[1], bus.bottom]], [[centre, chip.y + chip.h], [centre, bus.bottom]]);
        if (!link) return;
        link.points = null;
        link.shape = shape.length ? shape : null;
        const at = bus.top ?? bus.bottom;
        link.labelBox = shape.length ? { x: centre - context.JUNCTION / 2, y: at - context.JUNCTION / 2, w: context.JUNCTION, h: context.JUNCTION } : null;
      });
    }
  }

function placeBoards(model, rows, links, rowIndex, available) {
    const blocks = model.groups.filter((group) => group.stack);
    const blockW = Math.max(...blocks.map((group) => group.w));
    const chainOf = chainsOf(model);
    const chains = model.nodes.filter((entry) => entry.kind === "controller").map((entry) => entry.id);
    const chainFor = (id) => chainOf.get(id) ?? headChain(model, chainOf, id);
    const chained = model.nodes.filter((entry) => !entry.physical && !entry.chipOf && chainFor(entry.id));
    const pitch = Math.max(blockW / (chains.length + 1), Math.max(0, ...chained.map((entry) => entry.w)) + context.H_GAP);
    const share = (id) => {
      const index = chains.indexOf(chainFor(id));
      return index < 0 ? 0.5 : 0.5 + ((index - (chains.length - 1) / 2) * pitch) / blockW;
    };
    // A chip's board, counted down the stack; -1 for an HBA above it.
    const top = Math.min(...blocks.map((group) => rowIndex.get(group.id)));
    // An expander below the boards counts by its own row.
    const blockAt = (id) => {
      const entry = context.chipOfNode(model, id);
      if (entry?.chipOf) return rowIndex.get(entry.chipOf) - top;
      return entry?.below && rowIndex.has(entry.id) ? rowIndex.get(entry.id) - top : -1;
    };
    const cables = links.filter((link) => link.cable);
    const hba = (link) => chains.indexOf(chainOf.get(link.source));
    let room = 0;
    for (const side of ["left", "right"]) {
      // Nearest the boards first: cascades by their reach, then the cables
      // from above, the one from the HBA on this side outermost.
      const rails = cables.filter((link) => cableSide(model, link) === side).sort((a, b) => {
        const [fromA, fromB] = [blockAt(a.source) < 0, blockAt(b.source) < 0];
        if (fromA !== fromB) return fromA ? 1 : -1;
        const reach = Math.abs(blockAt(a.target) - blockAt(a.source)) - Math.abs(blockAt(b.target) - blockAt(b.source));
        // Two cables down to one row of stubs: the stub farther along the
        // row has the inner rail, so its run passes over the nearer one's.
        const along = (link) => rows[rowIndex.get(link.target)]?.items.findIndex((item) => item.id === link.target) ?? 0;
        const stubs = !context.chipOfNode(model, a.target).chipOf && !context.chipOfNode(model, b.target).chipOf ? (side === "left" ? along(b) - along(a) : along(a) - along(b)) : 0;
        return reach || stubs || (side === "left" ? hba(b) - hba(a) : hba(a) - hba(b));
      });
      let offset = 0;
      let previous = 0;
      for (const link of rails) {
        // A cascade's label is on its rail; an HBA cable's on its run above the boards.
        const onRail = blockAt(link.source) >= 0;
        const half = onRail ? linkWidth(link) / 2 : 0;
        offset += previous + half + context.RAIL_CLEAR;
        link.rail = { side, offset, onRail, level: 0 };
        previous = half;
      }
      if (rails.length) room = Math.max(room, offset + previous + 8);
    }
    const fromAbove = cables.filter((link) => !link.rail.onRail);
    // A cable from above has its label on its run beside its rail: room for it.
    if (fromAbove.length) room = Math.max(room, Math.max(...fromAbove.map((link) => link.rail.offset + 8)));
    const rowWidth = (row) => row.items.reduce((sum, item) => sum + item.w, 0) + Math.max(0, row.items.length - 1) * context.H_GAP;
    const content = Math.max(blockW + 2 * room, ...rows.map(rowWidth), ...rows.map((row) => row.bandWidth || 0));
    const width = Math.max(available, context.LEFT_BOARD + content + context.MARGIN);
    const shift = Math.max(0, Math.round((width - (context.LEFT_BOARD + content + context.MARGIN)) / 2));
    model.labelX = context.MARGIN + shift;
    const lo = context.LEFT_BOARD + shift;
    const hi = lo + content;
    model.bandX = lo;
    const blockX = Math.round(lo + (content - blockW) / 2);
    for (const row of rows) {
      const wanted = new Map();
      for (const item of row.items) {
        if (item.stack) item.w = blockW;
        wanted.set(item, item.stack ? blockX : blockX + blockW * share(item.id) - item.w / 2);
      }
      row.items.sort((left, right) => wanted.get(left) + left.w / 2 - wanted.get(right) - right.w / 2);
      if (row.items.length) placeRow(row.items, row.items.map((item) => wanted.get(item)), context.H_GAP, lo, hi);
      for (const item of row.items) item.x = Math.round(item.x);
      tick(model.work, "placeWork", row.items.length);
    }
    // Each cable from above: its rail, its own place on its HBA's bottom
    // edge (in the order of the rails), and a level for its sideways run,
    // the outermost rail highest, so that no two runs share a line.
    for (const link of fromAbove) link.rail.x = Math.round(link.rail.side === "left" ? blockX - link.rail.offset : blockX + blockW + link.rail.offset);
    for (const id of new Set(fromAbove.map((link) => link.source))) {
      const source = model.nodeById.get(id);
      const own = fromAbove.filter((link) => link.source === id).sort((a, b) => a.rail.x - b.rail.x);
      own.forEach((link, index) => {
        link.rail.exit = Math.round(source.x + (source.w * (index + 1)) / (own.length + 1));
      });
    }
    const levels = [];
    for (const link of fromAbove.slice().sort((a, b) => b.rail.offset - a.rail.offset)) {
      const span = [Math.min(link.rail.exit, link.rail.x) - 8, Math.max(link.rail.exit, link.rail.x) + 8];
      let level = levels.findIndex((taken) => taken.every(([low, high]) => span[1] < low || span[0] > high));
      if (level < 0) level = levels.push([]) - 1;
      levels[level].push(span);
      link.rail.level = level;
    }
    const first = rows.find((row) => row.tier.stack);
    const labelH = Math.max(0, ...fromAbove.map(linkHeight));
    model.cableLevel = labelH + 10;
    if (first) first.tier.gapAbove = fromAbove.length ? 14 + levels.length * model.cableLevel : 0;
    // Room above a row of stubs for the runs of the cables down to it.
    for (const row of rows) {
      const down = cables.filter((link) => context.chipOfNode(model, link.source).chipOf && rowIndex.get(link.target) === rows.indexOf(row) && !context.chipOfNode(model, link.target).chipOf).length;
      if (down) row.tier.gapAbove = 28 + (down - 1) * context.DOWN_STEP;
    }
    return width;
  }

function routeCables(model, cables, rows) {
    const first = rows.find((row) => row.tier.stack);
    const chip = (id) => context.chipOfNode(model, id);
    // Where each cable meets the side of a chip: cables from above first, the
    // nearest rail highest, so no cable crosses another at a chip.
    const meets = new Map();
    for (const link of cables) {
      for (const id of [link.source, link.target]) {
        const end = chip(id);
        if (!end?.chipOf) continue;
        const key = `${end.id}|${link.rail.side}`;
        if (!meets.has(key)) meets.set(key, []);
        meets.get(key).push(link);
      }
    }
    const meetY = (end, link) => {
      // An expander below the boards is met at the middle of its side.
      if (!end.chipOf) return Math.round(end.y + end.h / 2);
      const other = (item) => (chip(item.source) === end ? chip(item.target) : chip(item.source));
      const order = meets.get(`${end.id}|${link.rail.side}`).slice().sort((a, b) => {
        const [upA, upB] = [other(a).y < end.y, other(b).y < end.y];
        if (upA !== upB) return upA ? -1 : 1;
        return upA ? a.rail.offset - b.rail.offset : b.rail.offset - a.rail.offset;
      });
      return Math.round(end.y + (end.h * (order.indexOf(link) + 1)) / (order.length + 1));
    };
    const trackOf = (link) => Math.round(first.y - first.tier.gapAbove + 12 + (model.cableLevel - 10) / 2 + link.rail.level * model.cableLevel);
    // The cables down to one row of stubs, innermost rail first.
    const downOf = (target) => {
      const all = cables.filter((link) => chip(link.source).chipOf && !chip(link.target).chipOf && chip(link.target).y === target.y).sort((a, b) => a.rail.offset - b.rail.offset);
      return { all, index: all.findIndex((link) => chip(link.target) === target) };
    };
    const fromAbove = cables.filter((link) => !link.rail.onRail);
    for (const link of cables) {
      const source = chip(link.source);
      const target = chip(link.target);
      const block = model.groupById.get(target.chipOf || source.chipOf);
      const left = link.rail.side === "left";
      const railX = link.rail.x ?? Math.round(left ? block.x - link.rail.offset : block.x + block.w + link.rail.offset);
      const end = [left ? target.x : target.x + target.w, meetY(target, link)];
      const w = linkWidth(link);
      const h = linkHeight(link);
      if (source.chipOf && !target.chipOf) {
        // Down to a stub below the boards: along the rail to a level of its
        // own above the stub's row, across to the stub and into its top. The
        // innermost rail has the highest level, so no run crosses a rail.
        const start = [left ? source.x : source.x + source.w, meetY(source, link)];
        const own = downOf(target);
        const leadY = Math.round(target.y - 12 - (own.all.length - 1 - own.index) * context.DOWN_STEP);
        const dropX = Math.round(target.x + target.w / 2);
        link.points = [start, [railX, start[1]], [railX, leadY], [dropX, leadY], [dropX, target.y]];
        link.labelBox = { x: railX - w / 2, y: Math.round((start[1] + leadY) / 2 - h / 2), w, h };
      } else if (source.chipOf) {
        const start = [left ? source.x : source.x + source.w, meetY(source, link)];
        link.points = [start, [railX, start[1]], [railX, end[1]], end];
        link.labelBox = { x: railX - w / 2, y: Math.round((start[1] + end[1]) / 2 - h / 2), w, h };
      } else {
        const trackY = trackOf(link);
        link.points = [[link.rail.exit, source.y + source.h], [link.rail.exit, trackY], [railX, trackY], [railX, end[1]], end];
        // The label stands on the run, beside its rail, clear of every other
        // cable's drop and rail that passes this level.
        const uprights = fromAbove.filter((other) => other !== link).flatMap((other) => [
          { x: other.rail.exit, top: source.y, bottom: trackOf(other) },
          { x: other.rail.x, top: trackOf(other), bottom: Infinity },
        ]).filter((line) => line.top < trackY + h / 2 + 2 && line.bottom > trackY - h / 2 - 2);
        const inward = left ? 1 : -1;
        const reach = Math.abs(link.rail.exit - railX);
        let best = null;
        for (let step = w / 2 + 10; step <= Math.max(w / 2 + 10, reach - w / 2 - 6); step += 6) {
          const centre = railX + inward * step;
          const hits = uprights.filter((line) => Math.abs(line.x - centre) < w / 2 + 4).length;
          if (!best || hits < best.hits) best = { centre, hits };
          if (!hits) break;
        }
        link.labelBox = { x: Math.round(best.centre - w / 2), y: Math.round(trackY - h / 2), w, h };
      }
      tick(model.work, "routeWork");
    }
  }

function cableSide(model, link) {
    const target = context.chipOfNode(model, link.target);
    return target?.chipOf ? target.side : context.chipOfNode(model, link.source)?.side;
  }

function headChain(model, chainOf, id) {
    const below = new Set(context.list(model.outgoing.get(id)).map((link) => chainOf.get(link.target)).filter(Boolean));
    return below.size === 1 ? [...below][0] : undefined;
  }

function assignChains(model) {
    const chainOf = chainsOf(model);
    const controllers = model.nodes.filter((entry) => entry.kind === "controller");
    for (const link of model.links) {
      link.chain = model.nodeById.get(link.target)?.kind === "controller" ? link.target : chainOf.get(link.source) || null;
      // A line into a head end is its one HBA's, where it has one.
      if (!link.chain && model.nodeById.get(link.target)?.head) link.chain = headChain(model, chainOf, link.target) || null;
    }
    for (const link of model.links) if (context.list(link.rowIds).length > 1 || link.reported) link.chain = null;
    const drawn = new Set(model.links.map((link) => link.chain));
    model.chains = controllers.map((entry) => entry.id).filter((id) => drawn.has(id));
    // More chains than looks: every chain is still drawn and in the legend,
    // and the page names the chains that share a colour and line pattern.
    if (model.chains.length > CHAIN_LOOKS) {
      const byLook = new Map();
      model.chains.forEach((id, index) => {
        const look = index % CHAIN_LOOKS;
        if (!byLook.has(look)) byLook.set(look, []);
        byLook.get(look).push(model.nodeById.get(id)?.label || id);
      });
      const shared = [...byLook.values()].filter((names) => names.length > 1)
        .map((names) => (names.length > 2 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names.join(" and ")));
      // Every group is named: at most one per look, so the list stays short.
      model.notices.push(`${model.chains.length} chains are drawn and there are ${CHAIN_LOOKS} line looks, so these chains share a colour and line pattern: `
        + `${shared.join("; ")}. Every chain is drawn and in the legend; hover or focus a line to trace its chain.`);
    }
  }

function layoutModel(model, options = {}) {
    const available = Math.max(0, Number(options.width) || 0);
    // Work counters, so a test can bound the cost by counting, not timing.
    model.work = { total: 0, trackCosts: 0, routePasses: 0, candidates: 0 };
    for (const entry of model.nodes) sizeNode(entry);
    distinctTitles(model);
    const rows = model.tiers.map((tier) => ({
      tier,
      items: tier.id === "bays" && !model.physical?.stacked ? model.groups : tier.nodeIds.map((id) => model.nodeById.get(id) || model.groupById.get(id)).filter(Boolean),
    }));
    // Boards: each block's chips, left to right in HBA order, one size per board.
    const hbaOrder = model.nodes.filter((entry) => entry.kind === "controller").map((entry) => entry.id);
    for (const group of model.groups) {
      group.chips = model.physical?.boards && group.enclosure ? context.list(group.chipIds).map((id) => model.nodeById.get(id)).filter(Boolean) : [];
      const [w, h] = [Math.max(0, ...group.chips.map((chip) => chip.w)), Math.max(0, ...group.chips.map((chip) => chip.h))];
      group.chips.forEach((chip, index) => Object.assign(chip, { w, h, side: index < group.chips.length / 2 ? "left" : "right" }));
    }
    // Stacked enclosure blocks: one cell size for all of them, as large as the
    // box allows beside the tier labels and the rails, up to PHYS_CELL_MAX.
    const stackedRows = model.groups.filter((group) => group.stack && group.rows);
    if (stackedRows.length && stackedRows.every((group) => group.cellWidth === context.PHYS_CELL_W)) {
      const columns = Math.max(...stackedRows.flatMap((group) => group.rows.map((row) => Math.max(...row.map((cell) => cell.column)) + 1)));
      const breaks = Math.max(...stackedRows.map((group) => context.list(group.breakColumns).length));
      const boards = Boolean(model.physical?.boards);
      // Room beside the blocks only for what runs there: a board's cables, or
      // the rails of blocks that are not boards.
      const beside = boards ? (model.links.some((link) => link.cable) ? context.BOARD_ROOM : 0) : context.STACK_ROOM;
      const fit = Math.floor((available - (boards ? context.LEFT_BOARD : context.LEFT) - context.MARGIN - 2 * beside - 20 - breaks * context.GROUP_GAP) / columns) - context.PHYS_GAP;
      // A wide single row (24 bays) may take a slightly narrower cell to fit its box.
      const size = Math.max(context.PHYS_CELL_MIN, Math.min(context.PHYS_CELL_MAX, fit));
      for (const group of stackedRows) {
        group.cellWidth = size;
        for (const bay of group.bays) bay.w = size;
      }
    }
    for (const group of model.groups) [group.w, group.h] = groupSize(group);
    // A physical enclosure's node is drawn as its block's head: its lines
    // are the head's lines (the block's summary lines when collapsed).
    for (const entry of model.nodes) {
      const group = entry.physical ? model.groupById.get(entry.groupId) : null;
      if (!group) continue;
      entry.lines = group.lines.map((line) => line.text);
      entry.titleLines = group.lines.filter((line) => line.title).length;
    }
    for (const row of rows) {
      row.labelH = tierLines(row.tier, Boolean(model.physical?.boards));
      const missing = !row.items.length && (row.tier.state === "not_read" || row.tier.state === "not_collected" || row.tier.state === "none");
      row.bandLines = missing ? wrap(row.tier.note, context.BAND_CHARS) : null;
      row.bandW = missing ? Math.max(context.NODE_W, ...row.bandLines.map((line) => line.length * context.TEXT_W + 24)) : 0;
      row.bandH = missing ? 14 + row.bandLines.length * context.LINE_H : 0;
      row.bandWidth = row.bandW;
    }
    // A child with several labelled arrivals is wide enough for them side by
    // side, and a child with several parents wide enough that every parent's
    // centre can sit over it (a bay group: as wide as its enclosures).
    for (const item of [...model.nodes, ...model.groups]) {
      const into = context.list(model.incoming.get(item.id)).filter((link) => !link.bus);
      if (into.length > 1) item.w = Math.max(item.w, into.reduce((sum, link) => sum + linkWidth(link) + 12, 12));
      const parents = into.map((link) => model.nodeById.get(link.source)).filter(Boolean);
      if (parents.length > 1) {
        const row = parents.reduce((sum, parent) => sum + parent.w, 0) + (parents.length - 1) * context.H_GAP;
        const needed = item.tier === "bays" ? row : row - (parents[0].w + parents[parents.length - 1].w) / 2 + 40;
        item.w = Math.max(item.w, needed);
      }
    }
    const rowIndex = new Map();
    rows.forEach((row, index) => row.items.forEach((item) => rowIndex.set(item.id, index)));
    // A chip is placed with its board, so a link to or from one counts too.
    const placed = (id) => rowIndex.has(id) || Boolean(model.nodeById.get(id)?.chipOf);
    const links = model.links.filter((link) => placed(link.source) && placed(link.target) && link.source !== link.target);
    // The order that keeps every parent over its children wins; then the one
    // with the fewest crossings; then the shortest sideways runs.
    const byId = new Map(rows.flatMap((row) => row.items.map((item) => [item.id, item])));
    // A drawing with more lines than SIMPLIFY_ABOVE skips the polish: one
    // ordering pass instead of the candidate-order search, and no crossing
    // gaps. Every device and every line is still drawn, and the page says so.
    const lines = links.filter((link) => !link.bundle).length;
    model.simplified = lines > context.SIMPLIFY_ABOVE;
    // The order drawn last time for this scope is tried first and kept on a
    // tie, so expanding a shelf does not reorder the drawing under the operator.
    // Its ranks are read once per tier.
    const previous = context.isRecord(options.previousOrder) ? options.previousOrder : null;
    const previousCandidate = () => rows.map((row) => {
      const known = context.list(context.own(previous, row.tier.id));
      const ranks = new Map(known.map((id, index) => [id, index]));
      tick(model.work, "orderWork", known.length + row.items.length * Math.max(1, Math.ceil(Math.log2(row.items.length + 1))));
      const rank = (item) => ranks.get(item.id) ?? known.length;
      return row.items.slice().sort((left, right) => rank(left) - rank(right));
    });
    const stacked = Boolean(model.physical?.stacked);
    let candidates;
    if (stacked) {
      // Stacked enclosure blocks have one placement: see placeStacked.
      candidates = [rows.map((row) => row.items.slice())];
    } else if (model.simplified) {
      candidates = [previous ? previousCandidate() : orderCandidates(rows, links, rowIndex, model.work, 1).pop()];
    } else {
      candidates = orderCandidates(rows, links, rowIndex, model.work);
      if (previous) candidates.unshift(previousCandidate());
    }
    model.work.candidates = candidates.length;
    let best = null;
    for (const [number, candidate] of candidates.entries()) {
      if (candidates.length === 1) {
        best = { candidate };
        break;
      }
      rows.forEach((row, index) => {
        row.items = candidate[index];
      });
      const width = placeRows(rows, links, rowIndex, available, model.work);
      routeLinks(model, rows, links, rowIndex, width);
      const violations = spanViolations(links, byId, rowIndex);
      const score = countCrossings(links, model.work);
      const length = links.reduce((sum, link) => sum + link.points.reduce((total, point, index, all) => total + (index ? Math.abs(point[0] - all[index - 1][0]) : 0), 0), 0);
      tick(model.work, "scoreWork", links.length);
      const kept = previous && number === 0 ? 0 : 1;
      const better = !best || violations < best.violations
        || (violations === best.violations && (score < best.score
          || (score === best.score && (kept < best.kept || (kept === best.kept && length < best.length - 0.5)))));
      if (better) best = { violations, score, kept, length, candidate };
    }
    rows.forEach((row, index) => {
      row.items = best ? best.candidate[index] : row.items;
    });
    if (stacked && model.physical.boards) {
      model.width = placeBoards(model, rows, links, rowIndex, available);
      model.height = routeLinks(model, rows, links.filter((link) => !link.cable && !link.bus), rowIndex, model.width);
      placeCells(model);
      routeCables(model, links.filter((link) => link.cable), rows);
    } else if (stacked) {
      model.width = placeStacked(model, rows, links, rowIndex, available);
      model.height = routeLinks(model, rows, links.filter((link) => !link.rail), rowIndex, model.width);
      routeRails(model, links.filter((link) => link.rail), byId);
    } else {
      model.width = placeRows(rows, links, rowIndex, available, model.work);
      model.height = routeLinks(model, rows, links, rowIndex, model.width);
    }
    // No coordinate is ever NaN: a link that could not be routed is named.
    for (const link of links) {
      if (link.points && !link.points.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y))) {
        link.points = null;
        link.labelBox = null;
        model.notices.push(`Not drawn: ${linkName(model, link)} could not be routed.`);
      }
    }
    if (model.simplified) {
      model.crossings = null;
      model.crossingMarks = [];
      model.notices.push(`The drawing was simplified because of its size (${lines} lines, more than ${context.SIMPLIFY_ABOVE}): every device and every line is drawn, `
        + "but two steps were skipped: the search for the row order with the fewest crossings (the rows keep one ordering pass) and the crossing gaps.");
    } else {
      model.crossings = countCrossings(links, model.work);
      model.crossingMarks = crossingMarks(links, model.work);
    }
    model.bands = [];
    model.tiers.forEach((tier, index) => {
      tier.order = rows[index].items.map((item) => item.id);
      tier.band = rows[index].band ? { ...rows[index].band, lines: rows[index].bandLines } : null;
      if (tier.band) model.bands.push(tier.band);
    });
    placeCells(model);
    assignChains(model);
  }

function emptyPath() {
    return { nodes: new Set(), links: new Set(), groups: new Set(), bays: new Set(), ports: new Set() };
  }

function walkUp(model, id, path) {
    const pending = [id];
    while (pending.length) {
      const current = pending.pop();
      for (const link of context.list(model.incoming.get(current))) {
        if (path.links.has(link.id)) continue;
        path.links.add(link.id);
        if (!path.nodes.has(link.source)) {
          path.nodes.add(link.source);
          pending.push(link.source);
        }
      }
    }
  }

function walkDown(model, id, path) {
    const pending = [id];
    while (pending.length) {
      const current = pending.pop();
      for (const link of context.list(model.outgoing.get(current))) {
        if (path.links.has(link.id)) continue;
        path.links.add(link.id);
        if (link.bundle) {
          const group = model.groupById.get(link.target);
          path.groups.add(link.target);
          if (group?.enclosure) path.nodes.add(group.enclosure.id);
          for (const bay of busBays(link, group)) addBayVia(group, bay, link, path);
          continue;
        }
        if (!path.nodes.has(link.target)) {
          path.nodes.add(link.target);
          pending.push(link.target);
        }
      }
    }
  }

function bayPath(model, bayId, path) {
    const bay = model.bayById.get(bayId);
    const group = model.groupByBay.get(bayId);
    if (!bay || !group) return path;
    path.bays.add(bayId);
    path.groups.add(group.id);
    for (const port of bay.ports) path.ports.add(port.key);
    reportedChips(model, bay.ports, path);
    if (group.enclosure) {
      // The paths that report a disk in this bay; for a bay none reports one
      // in (an empty bay), every path of the enclosure it is in.
      const seen = group.paths.filter((item) => item.bayIds.has(bayId));
      enclosurePaths(model, group, seen.length ? seen : group.paths, path);
      return path;
    }
    for (const sourceId of bay.sources) {
      path.links.add(`bundle:${sourceId}->${group.id}`);
      path.nodes.add(sourceId);
      walkUp(model, sourceId, path);
    }
    return path;
  }

function busBays(link, group) {
    if (link.reported) return context.list(group?.bays).filter((bay) => bay.ports.some((port) => port.expanderId === link.source));
    const via = context.list(group?.paths).find((item) => item.id === link.via);
    return context.list(group?.bays).filter((bay) => (via ? via.bayIds.has(bay.bayId) : bay.sources.includes(link.via || link.source)));
  }

function addBayVia(group, bay, link, path) {
    path.bays.add(bay.bayId);
    const via = context.list(group.paths).find((item) => item.id === link.via);
    for (const port of bay.ports) {
      if (link.reported ? port.expanderId === link.source : via?.controllerId ? port.controller === via.label : true) path.ports.add(port.key);
    }
  }

function reportedChips(model, ports, path) {
    for (const port of ports) {
      const chip = model.nodeById.get(port.expanderId);
      if (!chip?.reported) continue;
      path.nodes.add(chip.id);
      if (chip.busId) path.links.add(chip.busId);
    }
  }

function drawnPath(model, path) {
    for (const id of [...path.nodes]) {
      const merged = model.nodeById.get(id)?.mergedInto;
      if (merged) path.nodes.add(merged);
    }
    for (const id of [...path.links]) {
      const bus = model.linkById.get(id)?.busOf;
      if (bus) path.links.add(bus);
    }
    return path;
  }

function portPath(model, key, path) {
    const port = model.portByKey.get(key);
    const group = port ? model.groupByBay.get(port.bayId) : null;
    if (!group) return path;
    path.ports.add(key);
    path.bays.add(port.bayId);
    path.groups.add(group.id);
    if (!group.enclosure) return bayPath(model, port.bayId, path);
    reportedChips(model, [port], path);
    const mine = group.paths.filter((item) => !item.controllerId || item.label === port.controller);
    const own = mine.filter((item) => (item.rowId ? item.rowId === port.expanderId || !port.expanderId : true));
    enclosurePaths(model, group, own.length ? own : mine, path);
    return path;
  }

function enclosurePaths(model, group, paths, path) {
    path.nodes.add(group.enclosure.id);
    for (const item of paths) {
      item.linkIds.forEach((id) => path.links.add(id));
      for (const sourceId of item.sourceIds) {
        path.nodes.add(sourceId);
        walkUp(model, sourceId, path);
      }
    }
  }

function tracePath(model, ref, path = emptyPath()) {
    return model && ref ? drawnPath(model, traceRef(model, ref, path)) : path;
  }

function traceRef(model, ref, path) {
    if (ref.kind === "port") return portPath(model, ref.id, path);
    if (ref.kind === "bay") return bayPath(model, ref.id, path);
    if (ref.kind === "group") {
      const group = model.groupById.get(ref.id);
      for (const bayId of context.list(group?.bayIds)) bayPath(model, bayId, path);
      return path;
    }
    if (ref.kind === "link") {
      const link = model.linkById.get(ref.id);
      if (!link) return path;
      path.links.add(link.id);
      path.nodes.add(link.source);
      walkUp(model, link.source, path);
      if (link.bundle) {
        const group = model.groupById.get(link.target);
        path.groups.add(link.target);
        if (group?.enclosure) path.nodes.add(group.enclosure.id);
        for (const bay of busBays(link, group)) addBayVia(group, bay, link, path);
        // The bus of a chip two HBAs reach: every row's own bundle as well.
        for (const rowId of context.list(link.rowIds)) {
          if (rowId === link.source) continue;
          path.nodes.add(rowId);
          walkUp(model, rowId, path);
          walkDown(model, rowId, path);
        }
      } else {
        path.nodes.add(link.target);
        walkDown(model, link.target, path);
      }
      return path;
    }
    if (!model.nodeById.has(ref.id)) {
      return model.bayById.has(ref.id) ? bayPath(model, ref.id, path) : path;
    }
    const entry = model.nodeById.get(ref.id);
    if (entry.physical) {
      // A physical enclosure: every bay in it and every path into it.
      const group = model.groupById.get(entry.groupId);
      if (!group) return path;
      path.groups.add(group.id);
      for (const bay of group.bays) {
        path.bays.add(bay.bayId);
        for (const port of bay.ports) path.ports.add(port.key);
      }
      enclosurePaths(model, group, group.paths, path);
      return path;
    }
    // A chip: every HBA's row for it. Any other node: itself.
    for (const row of entry.rows?.length ? entry.rows : [entry]) {
      path.nodes.add(row.id);
      walkUp(model, row.id, path);
      walkDown(model, row.id, path);
    }
    path.nodes.add(ref.id);
    return path;
  }

function searchTopology(model, query) {
    const text = String(query ?? "").trim().toLowerCase();
    const result = { query: text, matches: [], path: emptyPath() };
    if (!model || !text) return result;
    const bayOnly = /^bay\s*([0-9]+)$/.exec(text);
    const digits = /^[0-9]+$/.test(text) ? text : "";
    const wanted = bayOnly ? bayOnly[1] : digits;
    const found = new Set();
    const addBay = (bay) => {
      if (found.has(bay.bayId)) return;
      found.add(bay.bayId);
      const group = model.groupByBay.get(bay.bayId);
      result.matches.push({ id: bay.bayId, kind: "bay", label: bay.label, groupId: group?.id || "", collapsed: Boolean(group?.collapsed) });
      bayPath(model, bay.bayId, result.path);
    };
    if (wanted) {
      for (const bay of model.bays) {
        if (bay.locationKnown && bay.number !== "" && Number(bay.number) === Number(wanted)) addBay(bay);
      }
    }
    if (bayOnly) {
      drawnPath(model, result.path);
      return result;
    }
    for (const bay of model.bays) {
      const hit = digits
        ? bay.identities.some((value) => value.includes(text))
        : bay.label.toLowerCase().includes(text) || bay.terms.some((value) => value.includes(text));
      if (hit) addBay(bay);
    }
    for (const entry of model.nodes) {
      if (entry.kind === "host" || entry.mergedInto) continue;
      // Digits match what the payload names a node by, not the view's own ids.
      const names = digits ? [entry.label, entry.rawId] : [entry.label, entry.rawId, entry.id, ...context.list(entry.aliases)];
      if (names.some((value) => value && String(value).toLowerCase().includes(text))) {
        result.matches.push({ id: entry.id, kind: "node", label: entry.label, groupId: "", collapsed: false });
        tracePath(model, { kind: "node", id: entry.id }, result.path);
      }
    }
    drawnPath(model, result.path);
    return result;
  }

const MARK_SHAPES = {
    active: (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="4"`,
    standby: (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="4" fill="none"`,
    fault: (cx, cy) => `<path d="M${cx - 4} ${cy - 4}L${cx + 4} ${cy + 4}M${cx + 4} ${cy - 4}L${cx - 4} ${cy + 4}" fill="none"`,
    missing: (cx, cy) => `<rect x="${cx - 4}" y="${cy - 4}" width="8" height="8" fill="none"`,
    mapped: (cx, cy) => `<path d="M${cx} ${cy - 4.5}L${cx + 4.5} ${cy}L${cx} ${cy + 4.5}L${cx - 4.5} ${cy}Z"`,
    unmapped: (cx, cy) => `<path d="M${cx} ${cy - 4.5}L${cx + 4.5} ${cy}L${cx} ${cy + 4.5}L${cx - 4.5} ${cy}Z" fill="none"`,
    unknown: (cx, cy) => `<rect x="${cx - 4}" y="${cy - 4}" width="8" height="8" rx="2"`,
    member: (cx, cy) => `<path d="M${cx} ${cy - 4}L${cx + 4.5} ${cy + 4}L${cx - 4.5} ${cy + 4}Z"`,
    other: (cx, cy) => `<path d="M${cx - 4.5} ${cy - 4}L${cx + 4.5} ${cy - 4}L${cx} ${cy + 4}Z" fill="none"`,
  };

const LEGEND = {
    "is-lanes-reported": ["line", "lanes reported by both ends: count, rate, PHYs at each end"],
    "is-lanes-unreported": ["line", "lanes not reported: the link is in the payload, its lanes are not"],
    "is-plain-link": ["line", "link between a host, source, device and enclosure (no lane data kept)"],
    "is-bundle": ["line", "bay bundle: the count of enclosure-to-bay links it carries"],
    "is-bus": ["line", "expander bus: each bay's port stands on the bus of the expander it is attached to"],
    "flag-critical": ["flag", "critical: a wiring check's finding (listed above the drawing)"],
    "flag-warning": ["flag", "warning: a wiring check's finding (listed above the drawing)"],
    "is-on-path": ["line", "traced path (hover, focus, search or pin)"],
    "mark-parent-unknown": ["mark", "parent link not reported"],
    "mark-not-read": ["mark", "dashed band: not read (not collected, or the read failed), or read, but its completeness not reported"],
    "bay-yes": ["bay", "bay with a disk"],
    "bay-no": ["bay", "empty bay"],
    "bay-unknown": ["bay", "occupancy not read"],
    "path-active": ["shape", "path active (filled circle)"],
    "path-standby": ["shape", "path passive or standby (ring)"],
    "path-fault": ["shape", "path failed (cross)"],
    "path-missing": ["shape", "path missing (open square)"],
    "path-mapped": ["shape", "mapped by SES, no path state (diamond)"],
    "path-unmapped": ["shape", "no SES mapping (open diamond)"],
    "path-unknown": ["shape", "path state unknown (square)"],
    "path-member": ["shape", "member health, not a path state (triangle)"],
    "path-other": ["shape", "path state not recognised (open triangle)"],
    "path-more": ["text", "+N: more paths than the cell can draw; the cell's name lists every path"],
  };

function pathTone(state, basis = "") {
    if (basis === context.MEMBER_HEALTH) return "member";
    return context.own(context.PATH_STATE_TONES, String(state || "").toLowerCase()) || "other";
  }

function markShape(tone, cx, cy, state) {
    return `${MARK_SHAPES[tone](cx, cy)} class="topo-path-mark path-${tone}" data-path-state="${context.escapeHtml(state)}"></${tone === "active" || tone === "standby" ? "circle" : tone === "missing" || tone === "unknown" ? "rect" : "path"}>`;
  }

function onPathClass(path, set, id) {
    if (!path) return "";
    return path[set].has(id) ? " is-on-path" : " is-dim";
  }

function linkStyle(link) {
    if (link.bundle) return "is-bundle";
    if (link.lanesState === "reported") return "is-lanes-reported";
    if (link.lanesState === "not_reported") return "is-lanes-unreported";
    return "is-plain-link";
  }

function linkName(model, link) {
    const source = context.nodeName(model, link.source);
    const group = model.groupById.get(link.target);
    if (link.bundle && group?.enclosure) {
      const item = group.paths.find((entry) => entry.id === link.via);
      return `Path ${item?.label ? `${item.label} through` : "from"} ${source} to ${group.enclosure.board ? "" : "enclosure "}${group.enclosure.node.label}`;
    }
    const target = model.nodeById.get(link.target)?.label || (group ? (group.locationKnown ? "bay group" : "disk group") : link.target);
    return `Link ${source} to ${target}`;
  }

function linkDescription(model, link) {
    const parts = [linkName(model, link)];
    if (link.lanesState === "reported") {
      parts.push(context.plural(link.lanes.count, "lane"), context.laneRate(link.lanes));
      parts.push(`PHYs ${link.lanes.sourcePhys.join(", ") || "not listed"} to PHYs ${link.lanes.targetPhys.join(", ") || "not listed"}${link.lanes.targetBasis === "inferred" ? " (far end inferred)" : ""}`);
    } else if (link.lanesState === "not_reported") {
      parts.push("lanes not reported");
    } else if (link.bus) {
      parts.length = 0;
      parts.push(context.cardText(context.cardFor(model, { kind: "link", id: link.id })));
    } else if (link.bundle && model.groupById.get(link.target)?.enclosure) {
      parts.push(`${context.plural(model.groupById.get(link.target).bays.length, "bay")} in the enclosure`, `this path reports a disk in ${link.count}`);
    } else if (link.bundle) {
      parts.push(`${link.count} enclosure-to-bay ${link.count === 1 ? "link" : "links"}`);
    }
    // A cable with an expander's PHY table at an end: what that table says.
    if (context.LANE_LINKS.has(link.kind) && (model.sas.expanders.has(link.source) || model.sas.expanders.has(link.target))) {
      parts.push(...context.cableRows(model, link).filter((row) => !["Lanes", "PHYs"].includes(row.label) || row.warn).map((row) => `${row.label}: ${row.value}`));
    }
    if (!link.bus) parts.push(...context.flagRows(model, "link", link.id).map((row) => `${row.label}: ${row.value}`));
    return parts.join(", ");
  }

const CHAIN_COLOURS = 4;

const CHAIN_DASHES = 8;

const CHAIN_LOOKS = CHAIN_COLOURS * CHAIN_DASHES;

function chainStyle(index) {
    return { colour: (index + Math.floor(index / CHAIN_DASHES)) % CHAIN_COLOURS, dash: index % CHAIN_DASHES };
  }

function chainClass(index) {
    if (index < 0) return " chain-none";
    const style = chainStyle(index);
    return ` chain-${index} chain-colour-${style.colour} chain-dash-${style.dash}`;
  }

function flagBadge(x, y, small = false, tier = "critical") {
    const [half, up, down] = small ? [5, 5, 4] : [7, 7, 6];
    return `<path class="topo-flag-shape tier-${tier}" aria-hidden="true" d="M${x} ${y - up}L${x + half} ${y + down}L${x - half} ${y + down}Z"></path>`
      + `<path class="topo-flag-mark${small ? " is-small" : ""}" aria-hidden="true" d="M${x} ${y - (small ? 1.5 : 2)}L${x} ${y + (small ? 1 : 2)}M${x} ${y + (small ? 2.6 : 4)}L${x} ${y + (small ? 2.9 : 4.5)}"></path>`;
  }

function renderLink(model, link, path, used) {
    if (!link.points && !link.shape) return { line: "", label: "" };
    const style = link.bus ? "is-bus" : linkStyle(link);
    used.add(style);
    const flagged = context.flagTier(model, "link", link.id);
    if (flagged) used.add(`flag-${flagged}`);
    const highlight = onPathClass(path, "links", link.id) + (pinnedRef(path, "link", link.id) ? " is-pinned" : "");
    const name = linkDescription(model, link);
    const chain = chainClass(link.chain ? model.chains.indexOf(link.chain) : -1);
    const line = link.shape
      ? `<path class="topo-link ${style}${chain}${highlight}" data-topo-link="${context.escapeHtml(link.id)}" d="${link.shape.map((piece) => piece.map(([x, y], index) => `${index ? "L" : "M"}${Math.round(x)} ${Math.round(y)}`).join("")).join("")}"></path>`
      : `<polyline class="topo-link ${style}${chain}${flagged ? ` is-flagged tier-${flagged}` : ""}${highlight}" data-topo-link="${context.escapeHtml(link.id)}" points="${link.points.map(([x, y]) => `${Math.round(x)},${Math.round(y)}`).join(" ")}"></polyline>`;
    const box = link.labelBox;
    let body;
    if (link.labelLines.length) {
      body = `<rect class="topo-label-box" x="${Math.round(box.x)}" y="${Math.round(box.y)}" width="${Math.round(box.w)}" height="${Math.round(box.h)}" rx="7"></rect>`
        + link.labelLines.map((text, index) => `<text class="topo-label-text" x="${Math.round(box.x + box.w / 2)}" y="${Math.round(box.y + 14 + index * 15)}" text-anchor="middle">${context.escapeHtml(text)}</text>`).join(" ");
    } else {
      body = `<circle class="topo-junction" cx="${Math.round(box.x + box.w / 2)}" cy="${Math.round(box.y + box.h / 2)}" r="6"></circle>`;
    }
    if (flagged) body += flagBadge(Math.round(box.x + box.w - 2), Math.round(box.y + 1), false, flagged);
    const labelName = [link.labelLines.join(" "), name].filter(Boolean).join(", ");
    const label = `<g class="topo-link-label ${style}${flagged ? ` is-flagged tier-${flagged}` : ""}${highlight}" role="button" tabindex="0" data-fabric-topology-link="${context.escapeHtml(link.id)}" data-topo-link="${context.escapeHtml(link.id)}" aria-label="${context.escapeHtml(labelName)}">${body}</g>`;
    return { line, label };
  }

function visibleName(lines, title) {
    const visible = lines.join(" ");
    return title === visible || title.startsWith(`${visible}, `) ? title : [visible, title].filter(Boolean).join(", ");
  }

function nodeTitle(entry) {
    const phys = entry.phyGroups.length ? `PHYs ${entry.phyGroups.map((group) => context.phyRanges(group.phys)).join(" | ")}` : "";
    return [entry.label, ...entry.facts, phys, entry.parentUnknown && entry.kind !== "host" ? "parent link not reported" : "", ...context.list(entry.extra)].filter(Boolean).join(", ");
  }

function renderNode(entry, path, used) {
    const highlight = onPathClass(path, "nodes", entry.id) + (pinnedRef(path, "node", entry.id) ? " is-pinned" : "");
    const title = nodeTitle(entry);
    if (entry.parentUnknown && entry.kind !== "host") used.add("mark-parent-unknown");
    const text = entry.lines.map((line, index) => `<text class="topo-node-${index < entry.titleLines ? "title" : "fact"}" x="${entry.x + 10}" y="${entry.y + 20 + index * context.LINE_H}">${context.escapeHtml(line)}</text>`).join(" ");
    const unknown = entry.parentUnknown && entry.kind !== "host" ? " mark-parent-unknown" : "";
    const flagged = entry.flagged || "";
    if (flagged) used.add(`flag-${flagged}`);
const link = entry.neighbour?.href || "";
        const body = `class="topo-node kind-${context.token(entry.kind)}${entry.chipOf ? " topo-chip" : ""}${entry.neighbour || entry.stubGroup ? " topo-stub" : ""}${entry.head?.selected ? " is-own-view" : ""}${flagged ? ` is-flagged tier-${flagged}` : ""}${unknown}${highlight}" ${link ? `href="${context.escapeHtml(link)}" data-fabric-enclosure="${context.escapeHtml(entry.neighbour.enclosureId)}"` : 'role="button" tabindex="0"'} ${entry.reported || entry.peer || entry.stubGroup || link ? "data-fabric-topology-link" : "data-fabric-node"}="${context.escapeHtml(entry.id)}" data-topo-node="${context.escapeHtml(entry.id)}" aria-label="${context.escapeHtml(visibleName(entry.lines, title))}"><rect x="${entry.x}" y="${entry.y}" width="${entry.w}" height="${entry.h}" rx="8"></rect>${text}${flagged ? flagBadge(entry.x + entry.w - 12, entry.y + 14, false, flagged) : ""}`;
    // A stub with a known enclosure is a link to that enclosure's own view;
    // every other box is a button that pins.
    return link ? `<a ${body}</a>` : `<g ${body}</g>`;
  }

function portTone(model, port) {
    const tone = pathTone(port.state, port.basis);
    const role = model.roleOf?.get(port.controller)?.role;
    if (!role || tone !== "member" || !/^(online|ok|healthy|optimal)$/i.test(port.state)) return tone;
    return role === "active" ? "active" : "standby";
  }

function renderPort(model, bay, port, path, used) {
    const tone = portTone(model, port);
    used.add(`path-${tone}`);
    const flagged = context.flagTier(model, "port", port.key);
    const highlight = onPathClass(path, "ports", port.key) + (pinnedRef(path, "port", port.key) ? " is-pinned" : "");
    const controller = model.nodes.find((entry) => entry.kind === "controller" && entry.label === port.controller);
    const chain = chainClass(controller ? model.chains.indexOf(controller.id) : -1);
    const name = context.cardText(context.cardFor(model, { kind: "port", id: port.key }));
    // The port's target is its mark's band only: never over its bay's box,
    // the bus or a label on the bus. Its stub to the bus is drawn beside it,
    // not inside it, and follows its highlight (the stylesheet's "+").
    const stub = port.busY === null || port.busY === undefined ? "" : `<line class="topo-link topo-port-stub${chain}" x1="${port.x}" y1="${port.flip ? port.y - 5 : port.y + 5}" x2="${port.x}" y2="${port.busY}" aria-hidden="true"></line>`;
    return `<g class="topo-port${flagged ? ` is-flagged tier-${flagged}` : ""}${highlight}" role="button" tabindex="0" data-fabric-topology-link="${context.escapeHtml(port.key)}" data-topo-port="${context.escapeHtml(port.key)}" aria-label="${context.escapeHtml(name)}">`
      + `<rect class="topo-port-hit" x="${Math.round(port.x - port.hitW / 2)}" y="${port.y - PORT_HALF}" width="${port.hitW}" height="${2 * PORT_HALF}" rx="3"></rect>${markShape(tone, port.x, port.y, port.state)}</g>${stub}`;
  }

// Half the height of a port's band at the open edge of its cell; the cell's
// own box stops short of it (PORT_BAND), so the two targets never overlap.
const PORT_HALF = 7;

const PORT_BAND = 19;

function phyRowsText(state) {
    if (state.transportPhyCount === undefined) return "";
    return state.transportPhyCount === null ? "; PHY rows not read" : `; ${state.transportPhyCount} reported PHY rows`;
  }

function bayTitle(bay) {
    const occupancy = { yes: "disk present", no: "empty", unknown: "occupancy not read" }[bay.occupancy];
    const paths = bay.pathStates.length
      ? `paths: ${bay.pathStates.map((state) => `${state.controller || "path"} ${state.basis === context.MEMBER_HEALTH ? "member health " : ""}${state.state}${phyRowsText(state)}`).join(", ")}`
      : "no path state in payload";
    return [bay.label, bay.locationKnown ? "" : context.LOCATION_UNKNOWN, occupancy, bay.deviceName, bay.protocol === "SATA" ? "SATA: one port" : bay.protocol, paths, ...context.list(bay.extra)].filter(Boolean).join(", ");
  }

function marksFor(bay) {
    const fit = Math.max(1, Math.floor((bay.w - 4) / context.MARK_PITCH));
    if (bay.pathStates.length <= fit) return { shown: bay.pathStates, more: 0 };
    const shown = bay.pathStates.slice(0, Math.max(1, fit - 1));
    return { shown, more: bay.pathStates.length - shown.length };
  }

function renderBayCell(bay, path, used) {
    const highlight = onPathClass(path, "bays", bay.bayId) + (pinnedRef(path, "bay", bay.bayId) ? " is-pinned" : "");
    used.add(`bay-${bay.occupancy}`);
    const hook = bay.traceId ? `data-fabric-trace="${context.escapeHtml(bay.traceId)}"` : `data-fabric-node="${context.escapeHtml(bay.bayId)}"`;
    const separate = Boolean(bay.spread);
    const { shown, more } = separate ? { shown: [], more: 0 } : marksFor(bay);
    const height = bay.h || context.CELL_BOX;
    // In a board the edge band of the cell holds its port marks, not the bay.
    const band = bay.spread ? PORT_BAND : 0;
    const markY = bay.y + height - (bay.spread ? 11 : 8);
    // In an enclosure block the marks are spread across the cell, in HBA order.
    const markX = (index) => (bay.spread && !more
      ? Math.round(bay.x + (bay.w * (index + 1)) / (shown.length + 1))
      : bay.x + 2 + context.MARK / 2 + index * context.MARK_PITCH);
    const marks = shown.map((state, index) => {
      const tone = pathTone(state.state, state.basis);
      used.add(`path-${tone}`);
      return markShape(tone, markX(index), markY, state.state);
    }).join("");
    let count = "";
    if (more) {
      used.add("path-more");
      count = `<text class="topo-path-more" x="${bay.x + bay.w - 2}" y="${markY + 4}" text-anchor="end">+${more}</text>`;
    }
    const title = bayTitle(bay);
    // A bay label with no supported number is shown as the producer wrote it.
    const shownLabel = bay.locationKnown ? bay.number || bay.label : bay.label;
    // Under the strip a cell reads upward: ports, then number, then name.
    const down = bay.flip ? height - (bay.short ? 39 : 30) : 0;
    const flagged = bay.flagged || "";
    if (flagged) used.add(`flag-${flagged}`);
    // A tall cell says SAS or SATA beside the bay number.
    const numberText = shownLabel && bay.tag ? `${shownLabel} · ${bay.tag}` : shownLabel;
    const number = (shownLabel ? `<text class="topo-bay-number" x="${bay.x + bay.w / 2}" y="${bay.y + down + (bay.spread ? 16 : 14)}" text-anchor="middle">${context.escapeHtml(numberText)}</text>` : "")
      + (bay.short ? ` <text class="topo-bay-device" x="${bay.x + bay.w / 2}" y="${bay.y + down + 31}" text-anchor="middle">${context.escapeHtml(bay.short)}</text>` : "");
    const badge = flagged ? flagBadge(bay.x + bay.w - 6, bay.y + (bay.flip ? height - 6 : 7), true, flagged) : "";
    return `<g class="topo-bay bay-${bay.occupancy} state-${context.token(bay.status)}${bay.locationKnown ? "" : " is-logical"}${flagged ? ` is-flagged tier-${flagged}` : ""}${highlight}" role="button" tabindex="0" ${hook} data-topo-bay="${context.escapeHtml(bay.bayId)}" aria-label="${context.escapeHtml(visibleName([numberText, bay.short || "", more ? `+${more}` : ""].filter(Boolean), title))}"><rect x="${bay.x}" y="${bay.flip ? bay.y + band : bay.y}" width="${bay.w}" height="${height - band}" rx="3"></rect>${number} ${marks} ${count}${badge}</g>`;
  }

function pathSummary(summary) {
    return [...summary.pathStates.entries()]
      .sort(([left], [right]) => context.naturalCompare(left, right))
      .map(([name, states]) => `${name}: ${[...states.entries()].map(([state, count]) => `${count} ${state}`).join(", ")}`)
      .join(" · ");
  }

function groupHeading(group) {
    if (!group.locationKnown) {
      return `Disks, ${context.LOCATION_UNKNOWN} · ${context.plural(group.bayIds.length, "disk")}`;
    }
    const count = context.plural(group.bayIds.length, "bay");
    if (group.enclosure) return `${group.enclosure.node.label} · ${context.bayRange(group)} · ${count}`;
    if (!group.enclosureIds.length) return `No enclosure link · ${count}`;
    const numbers = group.bays.map((bay) => bay.number).filter(Boolean).sort((left, right) => Number(left) - Number(right));
    const span = numbers.length > 1 && numbers[0] !== numbers[numbers.length - 1] ? `${numbers[0]}–${numbers[numbers.length - 1]}` : numbers[0];
    // Only some labels carry a number: say how many, never a range for all.
    if (numbers.length && numbers.length < group.bayIds.length) return `${count}, ${numbers.length} numbered: ${span}`;
    const range = numbers.length ? `Bays ${numbers[0]}–${numbers[numbers.length - 1]}` : "Bays";
    return `${range} · ${count}`;
  }

function toggleBox(group) {
    return { x: group.x + 10, y: group.y + 4, w: 160, h: 22 };
  }

function renderEnclosureHead(group, path, used) {
    const entry = group.enclosure.node;
    const highlight = onPathClass(path, "nodes", entry.id) + (pinnedRef(path, "node", entry.id) ? " is-pinned" : "");
    const title = nodeTitle(entry);
    if (entry.parentUnknown) used.add("mark-parent-unknown");
    const top = group.y + (group.textTop || 0);
    const text = group.lines.map((line, index) => `<text class="topo-node-${line.title ? "title" : "fact"}" x="${group.x + 10}" y="${top + 18 + index * context.LINE_H}">${context.escapeHtml(line.text)}</text>`).join(" ");
    const flagged = entry.flagged || "";
    if (flagged) used.add(`flag-${flagged}`);
    return `<g class="topo-node topo-enclosure-head kind-enclosure${flagged ? ` is-flagged tier-${flagged}` : ""}${entry.parentUnknown ? " mark-parent-unknown" : ""}${highlight}" role="button" tabindex="0" data-fabric-topology-link="${context.escapeHtml(entry.id)}" data-topo-node="${context.escapeHtml(entry.id)}" aria-label="${context.escapeHtml(visibleName(group.lines.map((line) => line.text), title))}">`
      + `<rect x="${group.x + 4}" y="${top + 3}" width="${group.w - 8}" height="${group.lines.length * context.LINE_H + 8}" rx="6"></rect>${text}${flagged ? flagBadge(group.x + group.w - 16, top + 16, false, flagged) : ""}</g>`;
  }

function renderGroup(model, group, path, used) {
    const highlight = path ? (path.groups.has(group.id) ? " is-on-path" : " is-dim") : "";
    if (!group.enclosureIds.length) used.add("mark-not-read");
    const head = group.enclosure && !group.collapsed;
    const parts = [`<rect class="topo-group-box" x="${group.x}" y="${group.y}" width="${group.w}" height="${group.h}" rx="8"></rect>`,
      ...(head ? [] : group.lines.map((line, index) => `<text class="topo-node-${line.title ? "title" : "fact"}" x="${group.x + 10}" y="${group.y + (group.textTop || 0) + 18 + index * context.LINE_H}">${context.escapeHtml(line.text)}</text>`))];
    if (head) parts.push(renderEnclosureHead(group, path, used));
    if (group.caption && group.rows) {
      parts.push(`<text class="topo-edge-caption" x="${Math.round(group.x + group.w / 2)}" y="${group.y + group.h - 8}" text-anchor="middle">${context.escapeHtml(group.caption)}</text>`);
    }
    const summaryText = pathSummary(group.summary);
    if (context.list(group.chips).length) {
      parts.push(`<rect class="topo-board-strip" x="${group.gridLeft - 4}" y="${group.stripTop}" width="${group.gridW + 8}" height="${group.stripH}" rx="4"></rect>`);
    }
    // The buses are drawn on the board, over its strip and under its ports.
    const buses = model.links.filter((link) => link.bus && link.target === group.id).map((link) => renderLink(model, link, path, used).line).join("");
    const cells = group.cells.map((bay) => renderBayCell(bay, path, used)).join("") + buses
      + group.cells.filter((bay) => bay.spread).flatMap((bay) => bay.ports.map((port) => renderPort(model, bay, port, path, used))).join("")
      + context.list(group.chips).map((chip) => renderNode(chip, path, used)).join("");
    let toggle = "";
    if (group.expandable) {
      const label = group.collapsed ? `Draw all ${group.bayIds.length} ${group.locationKnown ? "bays" : "disks"}` : "Collapse to a summary";
      const { x, y } = toggleBox(group);
      toggle = `<g class="topo-expand" role="button" tabindex="0" data-fabric-expand-slots="${context.escapeHtml(group.expandKey)}" aria-expanded="${group.collapsed ? "false" : "true"}" aria-label="${context.escapeHtml(label)}"><rect x="${x}" y="${y}" width="160" height="22" rx="6"></rect><text class="topo-label-text" x="${x + 80}" y="${y + 15}" text-anchor="middle">${context.escapeHtml(label)}</text></g>`;
    }
    const name = group.lines.map((line) => line.text).join(" ");
    return `<g class="topo-group${group.collapsed ? " is-collapsed" : ""}${group.enclosure ? " is-enclosure" : ""}${highlight}" data-topo-group="${context.escapeHtml(group.id)}" aria-label="${context.escapeHtml(name)}" role="group">${parts.join(" ")}${cells}${toggle}</g>`;
  }

function renderTier(tier, model) {
    const y = tier.y ?? 0;
    const missing = tier.state === "not_read" || tier.state === "not_collected" || tier.state === "none";
    const tone = tier.state === "drawn" ? "ok" : context.token(tier.state);
    const labelLines = tier.labelLines || [tier.label];
    const noteLines = tier.noteLines || [tier.note];
    const left = model?.labelX ?? context.MARGIN;
    const label = `<g class="topo-tier" data-topo-tier="${context.escapeHtml(tier.id)}" aria-label="${context.escapeHtml([...labelLines, ...noteLines].join(" "))}">`
      + labelLines.map((line, index) => `<text class="topo-tier-label" x="${left}" y="${y + 18 + index * 17}">${context.escapeHtml(line)}</text>`).join(" ") + " "
      + noteLines.map((line, index) => `<text class="topo-tier-note tone-${tone}" x="${left}" y="${y + 18 + (labelLines.length + index) * 17}">${context.escapeHtml(line)}</text>`).join(" ")
      + "</g>";
    let band = "";
    if (missing && tier.band) {
      const { x, w, h, lines } = tier.band;
      band = `<g class="topo-missing-tier mark-not-read tone-${tone}" data-topo-band="${context.escapeHtml(tier.id)}"><rect x="${Math.round(x)}" y="${y}" width="${Math.round(w)}" height="${h}" rx="8"></rect>`
        + lines.map((line, index) => `<text class="topo-node-fact" x="${Math.round(x) + 12}" y="${y + 22 + index * context.LINE_H}">${context.escapeHtml(line)}</text>`).join("") + "</g>";
    }
    return label + band;
  }

function activePath(model, view = {}) {
    let path = null;
    if (view.hover && view.hover.kind !== "tier") path = tracePath(model, view.hover);
    else if (view.pin) path = tracePath(model, view.pin);
    else if (view.query) {
      const found = searchTopology(model, view.query);
      path = found.matches.length ? found.path : null;
    }
    if (path) path.pin = view.pin || null;
    return path;
  }

function pinnedRef(path, kind, id) {
    return Boolean(path?.pin && path.pin.kind === kind && path.pin.id === id);
  }

function refLabel(model, ref) {
    if (!ref) return "";
    if (ref.kind === "link") {
      const link = model.linkById.get(ref.id);
      if (!link) return "";
      if (link.bus) return `${context.nodeName(model, link.source)} → its bays`;
      return context.LANE_LINKS.has(link.kind) ? `Cable ${context.cableName(model, link)}` : linkName(model, link);
    }
    if (ref.kind === "bay") {
      const bay = model.bayById.get(ref.id);
      return bay ? [bay.label, bay.locationKnown ? "" : context.LOCATION_UNKNOWN, bay.deviceName].filter(Boolean).join(", ") : "";
    }
    if (ref.kind === "port") {
      const port = model.portByKey.get(ref.id);
      return port ? `${model.bayById.get(port.bayId)?.label || port.bayId} path via ${port.controller || `a ${model.sideWord} not reported`}${port.device ? ` (${context.deviceShort(port.device)})` : ""}` : "";
    }
    const entry = model.nodeById.get(ref.id);
    if (entry) return entry.label;
    const group = model.groupById.get(ref.id);
    return group ? groupHeading(group) : "";
  }

function traceSentence(model, view = {}) {
    if (view.hover?.kind === "tier") return traceSentence(model, { pin: view.pin });
    if (view.hover) return `Tracing: ${refLabel(model, view.hover)}.${view.pin ? " Pinned item kept; Esc clears." : ""}`;
    if (view.pin) return `Pinned: ${refLabel(model, view.pin)}. Click it again, click the background or press Esc to clear.`;
    return context.DEFAULT_HINT;
  }

function traceSentences(model, view = {}) {
    // What a pointer or keyboard can reach in this drawing: bays inside a
    // collapsed shelf are not drawn, so their sentences belong to the drawing
    // that shows the shelf in full.
    const reachable = [];
    for (const link of model.links) if (link.points || link.shape) reachable.push({ kind: "link", id: link.id });
    for (const entry of model.nodes) if (!entry.mergedInto) reachable.push({ kind: "node", id: entry.id });
    for (const group of model.groups) {
      reachable.push({ kind: "group", id: group.id });
      for (const bay of group.cells) {
        reachable.push({ kind: "bay", id: bay.bayId });
        if (bay.spread) for (const port of bay.ports) reachable.push({ kind: "port", id: port.key });
      }
    }
    // Any item that can be pinned gives every trace its held-pin form.
    const pin = reachable.find((ref) => ref.kind !== "group") || view.pin || null;
    const kept = new Set([traceSentence(model)]);
    // A pin this drawing does not show (a bay inside a collapsed shelf).
    if (view.pin && !reachable.some((ref) => context.sameRef(ref, view.pin))) kept.add(traceSentence(model, { pin: view.pin }));
    for (const ref of reachable) {
      kept.add(traceSentence(model, { hover: ref, pin }));
      // A group is not pinned: a click on it pins nothing.
      if (ref.kind !== "group") kept.add(traceSentence(model, { pin: ref }));
    }
    return [...kept];
  }

function legendText(model, style, text) {
    const located = model.bays.some((bay) => bay.locationKnown);
    const logical = model.bays.some((bay) => !bay.locationKnown);
    if (model.shared && style === "path-active") return "path via the active head end, the pool's owner (filled circle)";
    if (model.shared && style === "path-standby") return "path via a standby head end (ring)";
    if (model.physical && style === "is-bundle") return "path into an enclosure: the bays in the enclosure, and how many of them this path reports a disk in";
    if (!logical) return text;
    const words = located
      ? { "is-bundle": "bundle: the count of links to bays or disks it carries", "bay-yes": "bay with a disk, or a disk", "bay-no": "empty bay" }
      : { "is-bundle": "disk bundle: the count of links to disks it carries", "bay-yes": "disk present", "bay-no": "no disk" };
    return context.own(words, style) || text;
  }

function searchHelp(model) {
    const located = Boolean(model?.bays.some((bay) => bay.locationKnown));
    const expanders = Boolean(model?.nodes.some((entry) => entry.kind === "expander"));
    const things = [located ? "bay" : "", "disk", expanders ? "expander" : ""].filter(Boolean);
    const label = `Find a ${things.length > 1 ? `${things.slice(0, -1).join(", ")} or ${things[things.length - 1]}` : things[0]}`;
    const placeholder = located ? "Bay number, disk name, serial or SAS address" : "Disk name, serial or SAS address";
    return { label, placeholder };
  }

function legendSwatch(style, shape) {
    if (shape === "shape") {
      const tone = style.replace(/^path-/, "");
      return `<svg class="fabric-topology-swatch swatch-shape" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">${markShape(tone, 6, 6, tone)}</svg>`;
    }
    if (shape === "text") {
      return '<span class="fabric-topology-swatch swatch-text" aria-hidden="true">+N</span>';
    }
    if (shape === "flag") {
      return `<svg class="fabric-topology-swatch swatch-shape" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">${flagBadge(7, 7, false, style.replace(/^flag-/, ""))}</svg>`;
    }
    return `<span class="fabric-topology-swatch swatch-${shape} ${style}" aria-hidden="true"></span>`;
  }

function renderTransportNote(model) {
    const note = model.transportNote;
    if (!note) return "";
    const item = (text, kind) => `<li class="fabric-topology-adds-item is-${kind}">${context.escapeHtml(text)}</li>`;
    return `<section class="fabric-topology-adds" aria-label="What the SAS transport adds"><h3 class="fabric-topology-checks-title">What the SAS transport adds <span class="subtle">measured now${note.open.some((text) => /^Inferred/.test(text)) ? " · inferred" : ""} · still not read</span></h3>`
      + `<ul class="fabric-topology-adds-list">${note.measured.map((text) => item(text, "measured")).join("")}${note.open.map((text) => item(text, "open")).join("")}</ul></section>`;
  }

function renderChecks(model) {
    if (!model.checks.length) return "";
    const words = { pass: "PASS", critical: "CRITICAL", warning: "WARNING", not_read: "NOT READ" };
    const count = (status) => model.checks.filter((check) => check.status === status).length;
    const rows = model.checks.map((check) => `<li class="fabric-topology-check is-${context.token(check.status)}" data-topology-check="${context.escapeHtml(check.id)}"><span class="fabric-topology-check-status">${words[check.status]}</span>`
      + `<span class="fabric-topology-check-body"><span class="fabric-topology-check-title">${context.escapeHtml(check.title)}</span>`
      + check.lines.map((line) => `<span class="fabric-topology-check-line">${context.escapeHtml(line)}</span>`).join("")
      + check.notes.map((line) => `<span class="fabric-topology-check-note">${context.escapeHtml(line)}</span>`).join("")
      + "</span></li>").join("");
    return `<section class="fabric-topology-checks" aria-label="Wiring checks"><h3 class="fabric-topology-checks-title">Wiring checks <span class="subtle">${count("critical")} critical · ${count("warning")} warning · ${count("not_read")} not read · ${count("pass")} pass</span></h3><ul>${rows}</ul></section>`;
  }

function renderTopology(model, view = {}) {
    const path = activePath(model, view);
    // The traced-path entry is listed whatever is traced, so hover, focus,
    // search and a pin never change the legend's height above the drawing.
    const used = new Set(["is-on-path"]);
    const lines = [];
    const cables = [];
    const labels = [];
    for (const link of model.links) {
      const rendered = renderLink(model, link, path, used);
      // A bus is drawn with its board; a cable ends on a chip, over its board.
      if (link.cable) cables.push(rendered.line);
      else if (!link.bus) lines.push(rendered.line);
      labels.push(rendered.label);
    }
    const tiers = model.tiers.map((tier) => renderTier(tier, model)).join("");
    // At a crossing the horizontal line has a small gap and the vertical one
    // runs through it: a short background stroke, then the vertical again as
    // two pieces that both start at the crossing centre, so every line pattern
    // (each starts with a dash) is painted through the centre.
    const chainIndex = new Map(model.chains.map((id, index) => [id, index]));
    const crossings = context.list(model.crossingMarks).map((mark) => {
      const over = model.linkById.get(mark.over);
      if (!over) return "";
      const cross = onPathClass(path, "links", over.id);
      const chain = chainClass(over.chain ? chainIndex.get(over.chain) ?? -1 : -1);
      const x = Math.round(mark.x);
      const y = Math.round(mark.y);
      const piece = (end) => `<line class="topo-link topo-cross-over ${linkStyle(over)}${chain}${cross}" data-topo-cross="${context.escapeHtml(over.id)}" x1="${x}" y1="${y}" x2="${x}" y2="${end}"></line>`;
      return `<line class="topo-cross-gap" x1="${x - 7}" y1="${y}" x2="${x + 7}" y2="${y}"></line>${piece(y - 9)}${piece(y + 9)}`;
    }).join("");
    if (model.tiers.some((tier) => tier.state === "not_read" || tier.state === "not_collected")) used.add("mark-not-read");
    const nodes = model.nodes.filter((entry) => !entry.physical && !entry.chipOf).map((entry) => renderNode(entry, path, used)).join("");
    const groups = model.groups.map((group) => renderGroup(model, group, path, used)).join("");
    const svgLabel = `Storage Fabric topology: ${model.tiers.map((tier) => `${tier.label} ${tier.note}`).join("; ")}`;
    // The drawing at its own size; mount adds room around it when the view
    // kept on the screen needs it.
    const fullW = Math.ceil(model.width);
    const fullH = Math.ceil(model.height);
    // Fit: the whole drawing scaled into the box it was laid out for, never enlarged.
    const box = model.box || {};
    const scale = view.fit ? Math.min(1, box.width ? box.width / fullW : 1, box.height ? box.height / fullH : 1) : 1;
    const shownW = Math.max(1, Math.floor(fullW * scale));
    const shownH = Math.max(1, Math.floor(fullH * scale));
    const svg = `<svg class="fabric-topology-svg${view.fit ? " is-fitted" : ""}" role="group" aria-label="${context.escapeHtml(svgLabel)}" width="${shownW}" height="${shownH}" viewBox="0 0 ${fullW} ${fullH}">`
      + `<g class="topo-tiers">${tiers}</g><g class="topo-lines">${lines.join("")}</g><g class="topo-crossings">${crossings}</g><g class="topo-nodes">${nodes}${groups}</g><g class="topo-lines topo-cables">${cables.join("")}</g><g class="topo-labels">${labels.join("")}</g></svg>`;
    const partName = (part) => (part.title ? ` aria-label="${context.escapeHtml(part.title)}"` : "");
    const parts = model.parts.map((part) => `<span class="fabric-topology-part tone-${context.token(part.state)}"${partName(part)}>${context.escapeHtml(part.label)}: ${context.escapeHtml(part.state)}</span>`).join("");
    const source = model.core ? "HBA" : model.contract === "storage" ? "source" : "controller or source";
    const chains = model.chains.map((id, index) => `<span class="fabric-topology-legend-item" data-topology-legend="chain-${index}"><svg class="fabric-topology-swatch swatch-chain" width="28" height="8" viewBox="0 0 28 8" aria-hidden="true"><line class="topo-link is-lanes-reported${chainClass(index)}" x1="0" y1="4" x2="28" y2="4"></line></svg>${context.escapeHtml(`${model.nodeById.get(id)?.label || id}: every line from this ${source} down`)}</span>`).join("");
    const legend = chains + Object.entries(LEGEND)
      .filter(([style]) => used.has(style))
      .map(([style, [shape, text]]) => `<span class="fabric-topology-legend-item" data-topology-legend="${style}">${legendSwatch(style, shape)}${context.escapeHtml(legendText(model, style, text))}</span>`)
      .join("");
    const order = model.physical && model.chains.length > 1 && model.groups.some((group) => group.rows)
      ? `<span class="fabric-topology-legend-item" data-topology-legend="mark-order">one mark per path in each bay, grouped by ${context.escapeHtml(model.sideWord)}: ${context.escapeHtml(model.chains.map((id) => model.nodeById.get(id)?.label || id).join(", then "))}</span>`
      : "";
    const fitControl = `<button class="button secondary small fabric-topology-fit" type="button" data-topology-fit="${view.fit ? "actual" : "fit"}" aria-pressed="${view.fit ? "true" : "false"}">${view.fit ? "Actual size" : "Fit"}</button>`;
    const wide = !view.fit && box.width && fullW > box.width + 1
      ? `<p class="fabric-topology-wide-notice subtle" data-topology-wide-notice>The drawing is ${fullW} px wide, wider than its box (${Math.round(box.width)} px): scroll sideways for the rest, or choose Fit.</p>`
      : "";
    const smallest = 12 * scale;
    const fitNotice = scale < 1
      ? `<p class="fabric-topology-fit-notice warning-item muted compact" data-topology-fit-notice>Fitted to ${Math.round(scale * 100)}% of its size: its smallest text is ${smallest.toFixed(1)} px, under 12 px. Choose Actual size to read it.</p>`
      : "";
    const notices = model.notices.map((text) => `<div class="warning-item muted compact">${context.escapeHtml(text)}</div>`).join("");
    return `<div class="fabric-topology" data-topology-kind="${context.escapeHtml(model.kind)}">`
      + `<div class="fabric-topology-parts" aria-label="Part status">${parts}</div>`
      + `<div class="fabric-topology-legend" aria-label="Legend">${legend}${order}</div>`
      + renderChecks(model)
      + renderTransportNote(model)
      + notices
      // Shown while something is pinned or searched for: the same as Esc.
      + `<div class="fabric-topology-bar">${fitControl}<button class="button secondary small fabric-topology-clear" type="button" data-topology-clear${view.pin || view.query ? "" : " hidden"}>Clear selection</button>${wide}</div>`
      + fitNotice
      // The drawing, its trace band and the footer share a block: the band
      // sticks inside it, so it never rises over Fit or the notices above
      // the drawing (#1378).
      + `<div class="fabric-topology-stage"><div class="fabric-topology-scroll">${svg}</div>`
      // Below the drawing: hover text that wraps to more lines never moves
      // the drawing, and a label, from under the pointer. The band is held as
      // tall as the tallest sentence the drawing can show, measured in the
      // empty box after the sentence, so its height stays the same.
      + `<div class="fabric-topology-trace" data-topology-trace><p class="fabric-topology-pin subtle" data-topology-pin>${context.escapeHtml(traceSentence(model, { pin: view.pin }))}</p>`
      + `<div class="fabric-topology-trace-measure" data-topology-measure aria-hidden="true"></div></div>`
      + `<p class="fabric-topology-not-shown subtle">${context.escapeHtml(model.shared ? context.NOT_SHOWN_SHARED : !model.core ? context.NOT_SHOWN_STORAGE : [...model.sas.expanders.values()].some((item) => item.phys.length) ? context.NOT_SHOWN_COUNTERS : context.NOT_SHOWN)}</p></div>`
      // The hover card: the short SAS facts of what is hovered or focused,
      // beside it; out of the page's flow. The same facts are in the item's
      // name and in the trace line.
      + '<div class="fabric-topology-tip" data-topology-tip aria-hidden="true" hidden></div>'
      + "</div>";
  }

function linkDetail(model, linkId) {
    const link = model?.linkById.get(linkId);
    if (!link) return null;
    const source = model.nodeById.get(link.source);
    const row = (label, value) => `<div class="kv-row"><span class="kv-label">${context.escapeHtml(label)}</span><span class="kv-value">${context.escapeHtml(value)}</span></div>`;
    if (link.bundle && model.groupById.get(link.target)?.enclosure) {
      // A path into a physical enclosure.
      const group = model.groupById.get(link.target);
      const item = group.paths.find((entry) => entry.id === link.via);
      const seen = group.bays.filter((bay) => item?.bayIds.has(bay.bayId));
      const numbers = seen.filter((bay) => bay.number !== "").map((bay) => Number(bay.number)).sort((a, b) => a - b);
      return {
        title: "Selected enclosure path",
        html: `<div class="kv-grid">${row("Enclosure", group.enclosure.node.label)}`
          + `${row("HBA", item?.label || "not reported")}`
          + `${row("Through", source ? context.kindLabel(model, source) : link.source)}`
          + `${item?.handle ? row("Enclosure handle on this HBA", item.handle) : ""}`
          + `${row("Bays in the enclosure", `${group.bays.length} (${context.bayRange(group)})`)}`
          + `${row("Disks this path reports", `${seen.length}${numbers.length ? `: bays ${context.phyRanges(numbers)}` : ""}`)}</div>`,
      };
    }
    if (link.bundle) {
      const group = model.groupById.get(link.target);
      const carried = context.list(group?.bays).filter((bay) => bay.sources.includes(link.source));
      const unit = group?.locationKnown ? "bay" : "disk";
      // Only the numbers the payload's labels carry: a bay without one is
      // counted, never read as 0.
      const numbered = carried.filter((bay) => bay.number !== "").map((bay) => Number(bay.number)).sort((a, b) => a - b);
      const unnumbered = carried.length - numbered.length;
      const described = [`${carried.length} ${unit}s`];
      if (group?.locationKnown && numbered.length) described.push(`numbers ${context.phyRanges(numbered)}`);
      const counted = group?.locationKnown && unnumbered
        ? `; ${context.plural(unnumbered, "bay")} whose ${unnumbered === 1 ? "label carries" : "labels carry"} no number`
        : "";
      const names = carried.length <= 48 ? carried.map((bay) => bay.label).join(", ") : `${described.join(", ")}${counted}`;
      return {
        title: `Selected ${unit} bundle`,
        html: `<div class="kv-grid">${row("Source enclosure", source ? `${source.label} (${source.id})` : link.source)}`
          + `${row(group?.locationKnown ? "Bays" : "Disks", context.plural(link.count, unit))}`
          + `${row(`${group?.locationKnown ? "Bays" : "Disks"} it carries`, names || "none")}</div>`,
      };
    }
    const target = model.nodeById.get(link.target);
    const rows = [row("From", source?.label || link.source), row("To", target?.label || link.target), row("Kind", link.kind.replace(/-/g, " "))];
    if (link.lanesState === "reported") {
      rows.push(row("Lanes", link.labelLines[0]), row("PHYs", link.labelLines[1].replace(/^PHY /, "")));
    } else if (link.lanesState === "not_reported") {
      rows.push(row("Lanes", "not reported"));
    }
    return { title: "Selected link", html: `<div class="kv-grid">${rows.join("")}</div>` };
  }

  Object.defineProperties(context, {
    wrap: { configurable: true, get: () => wrap },
    wrapWords: { configurable: true, get: () => wrapWords },
    sizeNode: { configurable: true, get: () => sizeNode },
    distinctTitles: { configurable: true, get: () => distinctTitles },
    groupText: { configurable: true, get: () => groupText },
    groupSize: { configurable: true, get: () => groupSize },
    centreOf: { configurable: true, get: () => centreOf },
    placeRow: { configurable: true, get: () => placeRow },
    linkWidth: { configurable: true, get: () => linkWidth },
    linkHeight: { configurable: true, get: () => linkHeight },
    properCross: { configurable: true, get: () => properCross },
    tick: { configurable: true, get: () => tick },
    lowerBound: { configurable: true, get: () => lowerBound },
    segmentsOf: { configurable: true, get: () => segmentsOf },
    meetingPairs: { configurable: true, get: () => meetingPairs },
    countCrossings: { configurable: true, get: () => countCrossings },
    crossingMarks: { configurable: true, get: () => crossingMarks },
    orderCandidates: { configurable: true, get: () => orderCandidates },
    placeRows: { configurable: true, get: () => placeRows },
    spanViolations: { configurable: true, get: () => spanViolations },
    routeLinks: { configurable: true, get: () => routeLinks },
    tierLines: { configurable: true, get: () => tierLines },
    chainsOf: { configurable: true, get: () => chainsOf },
    placeStacked: { configurable: true, get: () => placeStacked },
    routeRails: { configurable: true, get: () => routeRails },
    placeCells: { configurable: true, get: () => placeCells },
    placeBoards: { configurable: true, get: () => placeBoards },
    routeCables: { configurable: true, get: () => routeCables },
    cableSide: { configurable: true, get: () => cableSide },
    headChain: { configurable: true, get: () => headChain },
    assignChains: { configurable: true, get: () => assignChains },
    layoutModel: { configurable: true, get: () => layoutModel },
    emptyPath: { configurable: true, get: () => emptyPath },
    walkUp: { configurable: true, get: () => walkUp },
    walkDown: { configurable: true, get: () => walkDown },
    bayPath: { configurable: true, get: () => bayPath },
    busBays: { configurable: true, get: () => busBays },
    addBayVia: { configurable: true, get: () => addBayVia },
    reportedChips: { configurable: true, get: () => reportedChips },
    drawnPath: { configurable: true, get: () => drawnPath },
    portPath: { configurable: true, get: () => portPath },
    enclosurePaths: { configurable: true, get: () => enclosurePaths },
    tracePath: { configurable: true, get: () => tracePath },
    traceRef: { configurable: true, get: () => traceRef },
    searchTopology: { configurable: true, get: () => searchTopology },
    MARK_SHAPES: { configurable: true, get: () => MARK_SHAPES },
    LEGEND: { configurable: true, get: () => LEGEND },
    pathTone: { configurable: true, get: () => pathTone },
    markShape: { configurable: true, get: () => markShape },
    onPathClass: { configurable: true, get: () => onPathClass },
    linkStyle: { configurable: true, get: () => linkStyle },
    linkName: { configurable: true, get: () => linkName },
    linkDescription: { configurable: true, get: () => linkDescription },
    CHAIN_COLOURS: { configurable: true, get: () => CHAIN_COLOURS },
    CHAIN_DASHES: { configurable: true, get: () => CHAIN_DASHES },
    CHAIN_LOOKS: { configurable: true, get: () => CHAIN_LOOKS },
    chainStyle: { configurable: true, get: () => chainStyle },
    chainClass: { configurable: true, get: () => chainClass },
    flagBadge: { configurable: true, get: () => flagBadge },
    renderLink: { configurable: true, get: () => renderLink },
    visibleName: { configurable: true, get: () => visibleName },
    nodeTitle: { configurable: true, get: () => nodeTitle },
    renderNode: { configurable: true, get: () => renderNode },
    portTone: { configurable: true, get: () => portTone },
    renderPort: { configurable: true, get: () => renderPort },
    bayTitle: { configurable: true, get: () => bayTitle },
    marksFor: { configurable: true, get: () => marksFor },
    renderBayCell: { configurable: true, get: () => renderBayCell },
    pathSummary: { configurable: true, get: () => pathSummary },
    groupHeading: { configurable: true, get: () => groupHeading },
    toggleBox: { configurable: true, get: () => toggleBox },
    renderEnclosureHead: { configurable: true, get: () => renderEnclosureHead },
    renderGroup: { configurable: true, get: () => renderGroup },
    renderTier: { configurable: true, get: () => renderTier },
    activePath: { configurable: true, get: () => activePath },
    pinnedRef: { configurable: true, get: () => pinnedRef },
    refLabel: { configurable: true, get: () => refLabel },
    traceSentence: { configurable: true, get: () => traceSentence },
    traceSentences: { configurable: true, get: () => traceSentences },
    legendText: { configurable: true, get: () => legendText },
    searchHelp: { configurable: true, get: () => searchHelp },
    legendSwatch: { configurable: true, get: () => legendSwatch },
    renderTransportNote: { configurable: true, get: () => renderTransportNote },
    renderChecks: { configurable: true, get: () => renderChecks },
    renderTopology: { configurable: true, get: () => renderTopology },
    linkDetail: { configurable: true, get: () => linkDetail },
  });
  };
})();
