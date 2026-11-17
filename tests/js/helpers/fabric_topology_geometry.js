"use strict";

// Geometry checks on the Topology layout model's own coordinates (#937).
// The same rules a screenshot pass measures in the browser: crossings
// between different links, lines behind boxes that are not their own,
// parallel segments of different links closer than 10 px, corners touching
// another link, and labels touching a box.

function linkSegments(model) {
  const result = [];
  for (const link of model.links) {
    const points = link.points || [];
    for (let index = 0; index + 1 < points.length; index += 1) {
      const [a, b] = [points[index], points[index + 1]];
      if (a[0] === b[0] && a[1] === b[1]) continue;
      result.push({ link, a, b, horizontal: a[1] === b[1], vertical: a[0] === b[0] });
    }
  }
  return result;
}

function properCross(p, q, r, s) {
  const d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0]);
  if (Math.abs(d) < 1e-9) return false;
  const t = ((r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])) / d;
  const u = ((r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])) / d;
  return t > 0.01 && t < 0.99 && u > 0.01 && u < 0.99;
}

// Proper interior crossings between segments of different links.
function crossings(model) {
  const segments = linkSegments(model);
  let count = 0;
  for (let i = 0; i < segments.length; i += 1) {
    for (let j = i + 1; j < segments.length; j += 1) {
      if (segments[i].link === segments[j].link) continue;
      if (properCross(segments[i].a, segments[i].b, segments[j].a, segments[j].b)) count += 1;
    }
  }
  return count;
}

function boxes(model) {
  const result = [];
  for (const item of model.nodes) result.push({ id: item.id, kind: "node", x: item.x, y: item.y, w: item.w, h: item.h });
  for (const group of model.groups) result.push({ id: group.id, kind: "group", x: group.x, y: group.y, w: group.w, h: group.h });
  for (const link of model.links) {
    if (link.labelBox) result.push({ id: `label:${link.id}`, kind: "label", link: link.id, ...link.labelBox });
  }
  return result;
}

function segmentHitsBox(a, b, box, inset = 1) {
  const xmin = box.x + inset;
  const xmax = box.x + box.w - inset;
  const ymin = box.y + inset;
  const ymax = box.y + box.h - inset;
  let t0 = 0;
  let t1 = 1;
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const p = [-dx, dx, -dy, dy];
  const q = [a[0] - xmin, xmax - a[0], a[1] - ymin, ymax - a[1]];
  for (let i = 0; i < 4; i += 1) {
    if (p[i] === 0) {
      if (q[i] < 0) return false;
    } else {
      const t = q[i] / p[i];
      if (p[i] < 0) {
        if (t > t1) return false;
        if (t > t0) t0 = t;
      } else {
        if (t < t0) return false;
        if (t < t1) t1 = t;
      }
    }
  }
  return t1 - t0 > 1e-6;
}

// A line passing through a node, group or label box that is not one of its
// own ends or its own label.
function linesBehindBoxes(model) {
  const hits = [];
  const all = boxes(model);
  for (const segment of linkSegments(model)) {
    const own = new Set([segment.link.source, segment.link.target, `label:${segment.link.id}`]);
    for (const box of all) {
      if (own.has(box.id)) continue;
      if (segmentHitsBox(segment.a, segment.b, box)) hits.push(`${segment.link.id} behind ${box.id}`);
    }
  }
  return [...new Set(hits)];
}

// Parallel segments of different links closer than `gap` px where they overlap.
function sharedTracks(model, gap = 10) {
  const segments = linkSegments(model);
  const problems = [];
  for (let i = 0; i < segments.length; i += 1) {
    for (let j = i + 1; j < segments.length; j += 1) {
      const [s, t] = [segments[i], segments[j]];
      if (s.link === t.link) continue;
      if (s.horizontal && t.horizontal) {
        const overlap = Math.min(Math.max(s.a[0], s.b[0]), Math.max(t.a[0], t.b[0])) - Math.max(Math.min(s.a[0], s.b[0]), Math.min(t.a[0], t.b[0]));
        if (overlap > 0 && Math.abs(s.a[1] - t.a[1]) < gap) problems.push(`${s.link.id} | ${t.link.id} horizontal ${Math.abs(s.a[1] - t.a[1])}`);
      } else if (s.vertical && t.vertical) {
        const overlap = Math.min(Math.max(s.a[1], s.b[1]), Math.max(t.a[1], t.b[1])) - Math.max(Math.min(s.a[1], s.b[1]), Math.min(t.a[1], t.b[1]));
        if (overlap > 0 && Math.abs(s.a[0] - t.a[0]) < gap) problems.push(`${s.link.id} | ${t.link.id} vertical ${Math.abs(s.a[0] - t.a[0])}`);
      }
    }
  }
  return problems;
}

function pointSegmentDistance(point, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const length = dx * dx + dy * dy;
  const t = length ? Math.max(0, Math.min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length)) : 0;
  return Math.hypot(point[0] - (a[0] + t * dx), point[1] - (a[1] + t * dy));
}

// A bend of one link closer than `gap` px to a segment of another.
function cornerTouches(model, gap = 6) {
  const segments = linkSegments(model);
  const problems = [];
  for (const link of model.links) {
    const points = link.points || [];
    for (let index = 1; index + 1 < points.length; index += 1) {
      for (const segment of segments) {
        if (segment.link === link) continue;
        const distance = pointSegmentDistance(points[index], segment.a, segment.b);
        if (distance < gap) problems.push(`${link.id} bend ${points[index]} near ${segment.link.id} (${distance.toFixed(1)})`);
      }
    }
  }
  return problems;
}

// Label boxes that touch a node or group box (less than `gap` px apart), and
// labels that do not sit on their own line.
function labelProblems(model, gap = 6) {
  const problems = [];
  const solids = [...model.nodes, ...model.groups];
  for (const link of model.links) {
    const box = link.labelBox;
    if (!box) continue;
    for (const item of solids) {
      const dx = Math.max(item.x - (box.x + box.w), box.x - (item.x + item.w));
      const dy = Math.max(item.y - (box.y + box.h), box.y - (item.y + item.h));
      if (Math.max(dx, dy) < gap) problems.push(`label ${link.id} touches ${item.id}`);
    }
    const centre = [box.x + box.w / 2, box.y + box.h / 2];
    const onLine = linkSegments({ links: [link] }).some((segment) => pointSegmentDistance(centre, segment.a, segment.b) < 0.5);
    if (!onLine) problems.push(`label ${link.id} is not on its line`);
  }
  return problems;
}

module.exports = { linkSegments, crossings, boxes, linesBehindBoxes, sharedTracks, cornerTouches, labelProblems, segmentHitsBox };
