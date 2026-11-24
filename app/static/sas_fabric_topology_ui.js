/* Storage Fabric Topology: checks, detail cards and interaction. */
(function () {
  window.SasFabricTopologyUI = function (context) {
function cableName(model, link) {
    return `${context.nodeName(model, link.source)} → ${context.nodeName(model, link.target)}`;
  }

function laneCap(model, link, lane) {
    const { sas } = model;
    const source = sas.controllers.get(link.source)?.phys.find((row) => row.phy === lane.source) || sas.expanders.get(link.source)?.byPhy.get(lane.source);
    const target = sas.expanders.get(link.target)?.byPhy.get(lane.target);
    // Only where both ends say what they can do: one end's maximum alone
    // does not make a lower rate a downshift.
    const caps = [source?.max, target?.max];
    return caps.every((value) => value !== null && value !== undefined) ? Math.min(...caps) : null;
  }

function cableEnds(model, link) {
    if (!link.lanes) return [];
    const ends = [];
    for (const [id, phys] of [[link.source, link.lanes.sourcePhys], [link.target, link.lanes.targetPhys]]) {
      const entry = model.nodeById.get(id);
      const expander = model.sas.expanders.get(id);
      ends.push({
        id,
        chipId: context.chipOfNode(model, id)?.id || id,
        label: `${context.nodeName(model, id)}${id === link.target && link.lanes.targetBasis === "inferred" ? " (far end, inferred)" : ""}`,
        hba: entry?.kind === "controller",
        rows: expander ? phys.map((phy) => expander.byPhy.get(phy)).filter(Boolean)
          : context.list(model.sas.controllers.get(id)?.phys).filter((row) => phys.includes(row.phy) && row.counters),
        wanted: phys.length,
      });
    }
    return ends;
  }

function unpairedPhys(model, link) {
    if (!link.lanes || link.lanes.rows.length) return [];
    const source = model.sas.controllers.get(link.source)?.phys || model.sas.expanders.get(link.source)?.phys || [];
    const target = model.sas.expanders.get(link.target)?.phys || [];
    // Each side by the cable's own end id; the name is display only.
    return [...link.lanes.sourcePhys.map((phy) => ({ side: "near", id: link.source, end: context.nodeName(model, link.source), row: source.find((row) => row.phy === phy) })),
      ...link.lanes.targetPhys.map((phy) => ({ side: "far", id: link.target, end: context.nodeName(model, link.target), row: target.find((row) => row.phy === phy) }))];
  }

// The mutual capability laneCap uses, for a cable whose PHY pairing is not
// read: a PHY is below its lane's maximum only where it is below its own
// maximum and every maximum the other end reports, so any pairing agrees.
function unpairedBelow(model, link) {
    const ends = unpairedPhys(model, link);
    const floor = (side) => {
      const maxima = ends.filter((item) => item.side === side).map((item) => item.row?.max ?? null);
      return maxima.length && maxima.every((value) => value !== null) ? Math.min(...maxima) : null;
    };
    const other = { near: floor("far"), far: floor("near") };
    return (item) => Boolean(item.row) && item.row.rate !== null && item.row.max !== null && other[item.side] !== null && item.row.rate < Math.min(item.row.max, other[item.side]);
  }

function slowLanes(model, link) {
    if (!link.lanes) return [];
    return link.lanes.rows.map((lane) => ({ ...lane, cap: laneCap(model, link, lane) })).filter((lane) => lane.rate !== null && lane.cap !== null && lane.rate < lane.cap);
  }

function stateCounts(states) {
    const counts = new Map();
    for (const state of states) counts.set(state, (counts.get(state) || 0) + 1);
    return [...counts.entries()].map(([state, count]) => `${count} ${state}`).join(", ");
  }

function transportNote(model) {
    if (!model.transport) return null;
    const hbas = model.nodes.filter((entry) => entry.kind === "controller" && model.sas.controllers.has(entry.id));
    const chips = model.nodes.filter((entry) => entry.kind === "expander" && !entry.mergedInto && model.sas.expanders.has(entry.id));
    const cables = model.links.filter((link) => context.LANE_LINKS.has(link.kind) && link.lanes);
    const phys = [...hbas.map((entry) => model.sas.controllers.get(entry.id).phys), ...chips.map((entry) => model.sas.expanders.get(entry.id).phys)].flat();
    const counted = phys.filter((row) => row.measured).length;
    const rated = phys.filter((row) => row.rate !== null && row.max !== null).length;
    const disks = model.bays.filter((bay) => bay.occupancy === "yes");
    const paths = disks.flatMap((bay) => bay.ports);
    const joined = paths.filter((port) => port.phy).length;
    const inferred = cables.filter((link) => link.lanes.targetBasis === "inferred").length;
    const farUnread = cables.filter((link) => !link.lanes.targetPhys.length).length;
    const before = model.shared ? "each head end as a storage system and its disks" : "its bays and what reports them";
    const measured = [
      `${context.plural(hbas.length, "HBA")} (${hbas.map((entry) => [entry.label, entry.facts[0]].filter(Boolean).join(": ")).join("; ")}) and ${context.plural(chips.length, "expander")} (${chips.map((entry) => context.tail(entry.address || entry.rawId)).join(", ")}): before, this view had ${before} and no hardware above them.`,
      `${rated === phys.length ? context.plural(phys.length, "PHY") : `${rated} of ${context.plural(phys.length, "PHY")}`} with link rate and maximum rate; the four error counters are read on ${counted} of ${phys.length}${counted < phys.length ? ` (${phys.length - counted} return none)` : ""}.`,
      cables.length ? `${context.plural(cables.length, "cable")} as its parent sees it: ${cables.map((link) => `${context.nodeName(model, link.source)} → ${context.nodeName(model, link.target)} x${link.lanes.count} on PHY ${context.phyRanges(link.lanes.sourcePhys)}`).join("; ")}.` : "",
      paths.length ? `${joined} of ${context.plural(paths.length, "disk path")} on its expander PHY, joined by the disk's block device name${model.shared ? " on each head" : ""}, not by bay number.` : "",
    ].filter(Boolean);
    const open = [
      inferred ? `Inferred, and marked so: the far end of ${context.plural(inferred, "cable")} (the child expander's only linked PHYs that are in no port).` : "",
      farUnread ? `Not read: the far-end PHYs of ${context.plural(farUnread, "cable")}.` : "",
      "Not read: which PHY pairs with which across a cable; whether a path is active or standby; each drive's own maximum rate; HBA temperature and slot; when the counters were last reset (one reading, so no trend).",
    ].filter(Boolean);
    return { measured, open };
  }

function unmatchedTargets(model) {
    const matched = new Set();
    for (const port of model.portByKey.values()) {
      if (port.phy) matched.add(`${context.chipOfNode(model, port.expanderId)?.id}|${port.phy.phy}`);
    }
    for (const entry of model.nodes) {
      const record = entry.chipOf && !entry.mergedInto ? model.sas.expanders.get(entry.id) : null;
      if (!record) continue;
      entry.unmatched = record.phys.filter((row) => /^(SAS|SATA) Target$/.test(row.device) && row.handle && !matched.has(`${entry.id}|${row.phy}`));
      if (!entry.unmatched.length) continue;
      entry.unmatchedText = `${targetCount(entry.unmatched)} on PHY ${context.phyRanges(entry.unmatched.map((row) => row.phy))}`;
      entry.facts.push(entry.unmatchedText, "not matched to a bay");
    }
  }

function targetCount(rows) {
    const kinds = [...new Set(rows.map((row) => row.device.replace(/ Target$/, "")))];
    return context.plural(rows.length, `${kinds.length === 1 ? kinds[0] : "disk"} target`);
  }

function wiringChecks(model, fabric) {
    const { physical, sas } = model;
    if (!physical) return;
    // Each flag keeps the check's short name, for the hover card and the
    // item's own name, and its full text, for the list and the inspector.
    // A flag is a warning or critical: `severity` is the tier of the check
    // in hand unless a line says its own.
    let checking = "";
    let severity = "warning";
    const tierOf = new Map();
    const tiered = (text, tier = severity) => {
      tierOf.set(text, tier);
      return text;
    };
    const flag = (kind, id, text, tier = tierOf.get(text) || severity) => {
      const key = `${kind}:${id}`;
      if (!model.flags.has(key)) model.flags.set(key, []);
      model.flags.get(key).push({ text, short: checking, tier });
    };
    // Coverage first. Each check names every subject it is expected to judge
    // before judging any, then marks each read and fine, read and faulty, or
    // not read. A subject left unmarked is not read: never skipped. A check
    // passes only when it has subjects and every one is read and fine; any
    // fault is the check's tier; otherwise it is not read, and partial where
    // some subjects were read and fine.
    const coverage = (noun) => {
      const subjects = new Map();
      const set = (key, state) => {
        const subject = subjects.get(key);
        if (!subject) return;
        if (subject.state === "bad" || (subject.state === "unread" && state === "ok")) return;
        subject.state = state;
      };
      return {
        noun,
        expect: (key, label) => {
          if (!subjects.has(key)) subjects.set(key, { label, state: null });
        },
        has: (key) => subjects.has(key),
        ok: (key) => set(key, "ok"),
        bad: (key) => set(key, "bad"),
        unread: (key) => set(key, "unread"),
        count: (state) => [...subjects.values()].filter((subject) => subject.state === state).length,
        keys: (state) => [...subjects].filter(([, subject]) => subject.state === state).map(([key]) => key),
        total: () => subjects.size,
        unmarked: () => [...subjects.values()].filter((subject) => subject.state === null).map((subject) => subject.label),
      };
    };
    // What is flagged and what is not read are two entries of one check: a
    // thing not read is never a line under a flag.
    const check = (id, title, flags, unread, pass, notes = [], cover = null, options = {}) => {
      const lines = [...unread];
      if (cover) {
        const unmarked = cover.unmarked();
        // A subject not read is never a pass, said or not.
        const short = cover.count("unread");
        if (short && !lines.length) lines.push(`${context.plural(short, cover.noun)} not read`);
        if (unmarked.length) lines.push(`${context.plural(unmarked.length, cover.noun)} not judged from read evidence: ${unmarked.slice(0, 6).join("; ")}${unmarked.length > 6 ? `; and ${unmarked.length - 6} more` : ""}`);
        const fine = cover.count("ok");
        const total = cover.total();
        if (lines.length && fine && fine < total && !flags.length) lines.unshift(`Partial: ${fine} of ${context.plural(total, cover.noun)} read and as expected; the rest not read`);
        if (!total && !lines.length && !flags.length && !options.empty) lines.push(`no ${cover.noun} is in the payload to check`);
      }
      const parts = [["critical", flags.filter((text) => (tierOf.get(text) || severity) === "critical")], ["warning", flags.filter((text) => (tierOf.get(text) || severity) !== "critical")], ["not_read", lines]].filter(([, entries]) => entries.length);
      // The notes go with the last entry of the check.
      parts.forEach(([status, entries], index) => model.checks.push({ id: index ? `${id}:${status}` : id, title, status, lines: entries, notes: index === parts.length - 1 ? notes : [] }));
      if (!parts.length) model.checks.push({ id, title, status: "pass", lines: [pass], notes });
    };
    const controllers = model.nodes.filter((entry) => entry.kind === "controller");
    const labelOf = (id) => model.nodeById.get(id)?.label || "an HBA not reported";
    const chipOf = (id) => context.chipOfNode(model, id);
    // A chip and every observation of it (one per HBA that reaches it).
    const rowsOf = (chip) => [...new Map([chip, ...context.list(chip?.rows)].filter(Boolean).map((row) => [row.id, row])).values()];
    const cables = model.links.filter((link) => context.LANE_LINKS.has(link.kind));
    const cableInto = new Map(cables.map((link) => [link.target, link]));
    // The HBA an expander observation is reached from, through cables the
    // payload resolved; null where a cable on the way is not resolved.
    const reachOf = (id) => {
      const seen = new Set();
      let current = id;
      while (current && !seen.has(current)) {
        seen.add(current);
        const node = model.nodeById.get(current);
        if (node?.kind === "controller") return current;
        const link = cableInto.get(current);
        if (!link) return null;
        current = link.source;
      }
      return null;
    };
    // Every expander observation the payload names with no cable into it.
    const unparented = model.nodes.filter((entry) => entry.kind === "expander" && sas.expanders.has(entry.id) && !cableInto.has(entry.id));
    const boards = physical.enclosures.map((enclosure) => ({
      enclosure,
      name: context.enclosureName(enclosure),
      chips: context.list(enclosure.group.chipIds).map((id) => model.nodeById.get(id)).filter(Boolean),
    }));
    const boardOf = new Map();
    for (const board of boards) {
      for (const chip of board.chips) for (const row of [chip, ...context.list(chip.rows)]) boardOf.set(row.id, board);
    }
    const pathStates = () => {
      checking = "failed disk paths";
      severity = "critical";
      const flags = [];
      const lines = [];
      const unread = [];
      const cover = coverage("path state");
      // Every path of every bay is a subject, its HBA read or not; so is a
      // bay whose paths the producer did not all read, and a bay that holds
      // a disk with no path state at all.
      const owners = [...controllers.map((entry) => entry.id), ""];
      const byOwner = new Map(owners.map((id) => [id, []]));
      for (const bay of model.bays) {
        bay.pathStates.forEach((state, index) => {
          const key = `${bay.bayId}#${index}`;
          cover.expect(key, `${bay.label}: path via ${state.controller || "an HBA not read"}`);
          const owner = byOwner.has(state.controllerId) ? state.controllerId : "";
          byOwner.get(owner).push({ bay, state, key });
        });
        if (bay.notRead.includes("path_states")) {
          cover.expect(`${bay.bayId}#more`, `${bay.label}: paths past the limit`);
          cover.unread(`${bay.bayId}#more`);
          unread.push(`${bay.label}: some of its paths were not read (the producer's path limit)`);
        }
        if (!bay.pathStates.length && bay.occupancy === "yes") {
          cover.expect(`${bay.bayId}#none`, `${bay.label}: its paths`);
          cover.unread(`${bay.bayId}#none`);
          unread.push(`${bay.label}: holds a disk, but no path state is read for it`);
        }
      }
      if (context.readState(fabric, "paths") === "partial") unread.push("The path read was not reported complete, so a path may be unread rather than absent.");
      let read = 0;
      for (const [owner, items] of byOwner) {
        if (!items.length) continue;
        const label = owner ? labelOf(owner) : "A path whose HBA is not read";
        // Only states the payload read are counted or judged; the rest are
        // not read, whatever the platform calls them.
        const known = items.filter((item) => !context.isUnreadState(item.state));
        for (const item of items) if (context.isUnreadState(item.state)) cover.unread(item.key);
        read += known.length;
        const blind = items.length - known.length;
        if (blind) unread.push(known.length ? `${label}: ${blind} of ${context.plural(items.length, "path")}: active or standby is not read` : `${label}: ${context.plural(items.length, "path")} present; active or standby is not read`);
        if (!known.length) continue;
        const bad = known.filter((item) => ["fault", "missing"].includes(context.pathTone(item.state.state, item.state.basis)));
        for (const item of known) (bad.includes(item) ? cover.bad : cover.ok)(item.key);
        const text = `${label}: ${stateCounts(known.map((item) => item.state.state))}`;
        lines.push(text);
        if (!bad.length) continue;
        flags.push(tiered(text, "critical"));
        if (owner) flag("node", owner, `${context.plural(bad.length, "disk path")} failed or missing via ${labelOf(owner)}`, "critical");
        else for (const item of bad) flag("bay", item.bay.bayId, `${item.bay.label}: a path whose HBA is not read is ${item.state.state}`, "critical");
      }
      const title = model.core ? "Disk path states per HBA side" : model.shared ? "Disk states per head end" : "Disk path states per source";
      const notes = cover.total() && !read && model.transport ? ["The Linux SAS transport classes say a path exists, not whether it is active."] : [];
      if (!cover.total() && !unread.length) unread.push("no path state is in the payload");
      if (unread.length && !flags.length && lines.length) unread.push(`Read: ${lines.join("; ")}`);
      check("path-states", title, flags, unread, lines.join("; "), notes, cover, { empty: true });
    };

    // Two head ends on one backplane: what the two say of each bay is checked
    // against each other, and that they agree on which of them is active.
    const headChecks = () => {
      const heads = model.nodes.filter((entry) => entry.head);
      const labels = heads.map((entry) => entry.label).join(" and ");
      {
        checking = "the heads disagree";
        severity = "warning";
        const flags = [];
        const blind = [];
        let agreed = 0;
        // A head end that reports no disk in any bay says nothing to compare
        // (a standby node that does not see the disks): that is not read, not
        // a disagreement in every bay.
        const everyBay = boards.flatMap((board) => board.enclosure.group.bays);
        // Occupancy through the bay reader: a head that did not read the bay is
        // not read, never "no disk".
        const occupancy = (head) => context.bayOccupancy(head);
        const silent = heads.map((entry) => entry.label).filter((label) => !everyBay.some((bay) => bay.heads.some((head) => head.label === label && occupancy(head) === "yes")));
        if (silent.length) blind.push(`${silent.join(", ")} ${silent.length === 1 ? "reports" : "report"} no disk in any bay: nothing to compare with`);
        for (const bay of everyBay) {
          const asked = bay.heads.filter((head) => !silent.includes(head.label));
          const told = asked.filter((head) => occupancy(head) !== "unknown");
          const present = told.filter((head) => occupancy(head) === "yes");
          if (!present.length) continue;
          const unread = asked.filter((head) => occupancy(head) === "unknown");
          if (unread.length) blind.push(`${bay.label}: ${unread.map((head) => head.label).join(", ")} did not read the bay`);
          const absent = told.filter((head) => occupancy(head) === "no");
          const serials = [...new Set(present.map((head) => head.serial).filter(Boolean))];
          let text = "";
          if (absent.length) text = `${bay.label}: ${present.map((head) => head.label).join(", ")} ${present.length === 1 ? "reports" : "report"} a disk, ${absent.map((head) => head.label).join(", ")} ${absent.length === 1 ? "does" : "do"} not`;
          else if (serials.length > 1) text = tiered(`${bay.label}: the heads report different disks (serials ${serials.join(", ")})`, "critical");
          else if (present.some((head) => !head.serial)) blind.push(`${bay.label}: a head reports no serial, so the disk is not compared`);
          else agreed += 1;
          if (!text) continue;
          flags.push(text);
          flag("bay", bay.bayId, text);
        }
        check("heads", "Each disk is reported by each head end", flags, blind, `${context.plural(agreed, "disk")}, each reported by ${labels} with the same serial in the same bay`);
      }
      {
        checking = "pool ownership";
        severity = "critical";
        const owners = [...new Set(heads.map((entry) => entry.head.owner))];
        const flags = owners.length > 1 && !owners.includes("") ? [`The heads name different pool owners: ${heads.map((entry) => `${entry.label} says ${entry.head.owner}`).join("; ")}`] : [];
        for (const entry of flags.length ? heads : []) flag("node", entry.id, flags[0]);
        const fencing = heads.map((entry) => entry.head.fencing);
        check("ownership", "The head ends agree on which is active", flags, owners.includes("") ? ["A head does not report the pool's owner."] : [],
          `${heads[0]?.head.pool || "pool"} active on ${owners[0]}; ${heads.filter((entry) => entry.head.role === "standby").map((entry) => entry.label).join(", ") || "no head"} standby`,
          [fencing.every((value) => value === true) ? `I/O fencing enabled on ${labels}.` : fencing.includes(null) ? "I/O fencing: not read." : `I/O fencing is not enabled on ${heads.filter((entry) => entry.head.fencing !== true).map((entry) => entry.label).join(", ")}.`]);
      }
    };

    if (!model.core) {
      // A platform that does not collect HBA and expander hops: what its
      // disks report is checked, and the rest is not read, never a flag.
      const why = context.notCollectedReason(fabric);
      const unread = (id, title) => model.checks.push({ id, title, status: "not_read", lines: [why], notes: [] });
      if (model.shared) {
        headChecks();
        unread("redundancy", "Each head end's controller and expander hops to the backplane");
        {
          const bays = boards.flatMap((board) => board.enclosure.group.bays).filter((bay) => bay.occupancy === "yes");
          const named = bays.filter((bay) => bay.ports.some((port) => port.fromPath));
          const lines = named.map((bay) => `${bay.label}: ${bay.ports.filter((port) => port.fromPath).map((port) => `${port.controller} via expander ${port.attached} PHY ${port.pathPhy}`).join("; ")} (from the device path)`);
          const rest = bays.filter((bay) => !named.includes(bay));
          model.checks.push({
            id: "ports",
            title: "The expander and PHY of each disk, per head end",
            status: rest.length || !named.length ? "not_read" : "pass",
            lines: rest.length ? [`${context.plural(rest.length, "disk")} (${rest.map((bay) => bay.label).join(", ")}): the device name each head reports names no expander. ${why}`] : lines.length ? lines : [why],
            notes: rest.length ? lines.map((line) => `Read: ${line}`) : [],
          });
        }
        unread("lanes", "Cable lanes and rates between the head ends and the backplane");
        unread("cable-errors", "No PHY errors on the cables' lanes");
        unread("port-errors", "No PHY errors on the disk ports");
        pathStates();
        return;
      }
      unread("redundancy", "Each backplane has an expander via each HBA");
      unread("cascades", "Each cascade stays on one HBA's side and joins two backplanes in level order");
      unread("lanes", "Every cable has the same lane count and every lane runs at the maximum of both its ends");
      const lines = [];
      const missing = [];
      for (const board of boards) {
        const disks = board.enclosure.group.bays.filter((bay) => bay.occupancy === "yes");
        const attached = disks.filter((bay) => bay.ports.some((port) => boardOf.get(port.expanderId) === board));
        const without = disks.filter((bay) => !attached.includes(bay));
        if (attached.length) lines.push(`${board.name}: ${context.plural(attached.length, "disk")}, each with a port attached to ${board.chips.map((chip) => `${chip.label} (${chip.address})`).join(" or ")}`);
        if (without.length) missing.push(`${board.name}: ${without.map((bay) => bay.label).join(", ")}: the address the disk's port is attached to is not read`);
      }
      model.checks.push({
        id: "ports",
        title: "Each disk reports the expander its port is attached to",
        status: missing.length || !lines.length ? "not_read" : "pass",
        lines: missing.length ? missing : lines.length ? lines : [why],
        notes: [...(missing.length ? lines : []), `A second path per disk and the expander PHY of each port are not read: ${why}`],
      });
      unread("cable-errors", "No PHY errors on the cables' lanes");
      unread("port-errors", "No PHY errors on the disk ports");
      pathStates();
      return;
    }

    if (model.shared) headChecks();
    const complete = ["controllers", "expanders", "enclosures"].every((domain) => context.readState(fabric, domain) === "complete");
    const incomplete = complete ? [] : ["The HBA, expander or enclosure read was not reported complete, so a missing item may be unread rather than absent."];
    const names = (ids) => ids.map(labelOf).join(" + ");
    // One HBA in a complete Linux transport survey: single-path as built.
    const singlePath = Boolean(model.transport) && !model.shared && complete && controllers.length === 1;

    // (a) Redundancy: each board, against the HBAs its own evidence names,
    // each reached only through a cable the payload resolved.
    {
      checking = "HBA redundancy";
      severity = "warning";
      const flags = [];
      const passes = [];
      const notes = [];
      const unread = [...incomplete];
      const cover = coverage("backplane");
      for (const board of boards) cover.expect(board.enclosure.id, board.name);
      for (const board of boards) {
        const key = board.enclosure.id;
        const reach = board.chips.map((chip) => [...new Set(rowsOf(chip).map((row) => reachOf(row.id)))]);
        const blind = board.chips.filter((chip, index) => reach[index].includes(null));
        const here = new Set(reach.flat().filter(Boolean));
        // The HBAs this enclosure's own evidence names: those that reach its
        // expanders, see it as an enclosure, or report a path to its bays.
        // Another HBA of the host may serve other shelves only.
        const expected = new Set([...here,
          ...context.list(board.enclosure.observations).map((entry) => entry.controllerId),
          ...board.enclosure.group.bays.flatMap((bay) => bay.pathStates.map((state) => state.controllerId))].filter(Boolean));
        const group = controllers.filter((entry) => expected.has(entry.id));
        const missing = group.filter((entry) => !here.has(entry.id));
        const described = board.chips.map((chip, index) => `${chip.label} via ${names(reach[index].filter(Boolean)) || "a cable not read"}`).join("; ");
        let text = "";
        if (!board.chips.length) text = `${board.name}: no expander is reported for it`;
        else if (blind.length) {
          cover.unread(key);
          unread.push(`${board.name}: ${blind.map((chip) => chip.label).join(", ")} ${blind.length === 1 ? "is" : "are"} not reached through a cable the payload resolved`);
          continue;
        } else if (!singlePath && controllers.length > 1 && group.length < 2) {
          cover.unread(key);
          unread.push(`${board.name}: only ${names([...here])} reports it; whether another HBA of this host should reach it is not read`);
          continue;
        } else if (missing.length) text = `${board.name}: no expander via ${missing.map((entry) => entry.label).join(", ")} (${described})`;
        else if (board.chips.length === 1) {
          // One expander that every HBA reaches: nothing is miswired. That
          // the expander itself is not doubled is said, not flagged.
          passes.push(`${board.name}: ${described}`);
          if (!singlePath) notes.push(`${board.name} has one expander, reached via ${names([...here])}: the HBA paths are doubled, the expander is not.`);
        } else if (reach.every((ids) => ids.length === group.length)) {
          // Every HBA of its group has a cable to every expander.
          passes.push(`${board.name}: dual-domain, each of its ${board.chips.length} expanders is reached via ${names(group.map((entry) => entry.id))}`);
          notes.push(`${board.name}: an expander reached by more than one HBA is one chip with an uplink from each, drawn once.`);
        } else if (reach.every((ids) => ids.length === 1) && here.size === board.chips.length) {
          passes.push(`${board.name}: one expander via each of ${names([...here])}`);
        } else text = `${board.name}: its expanders are not reached alike (${described})`;
        if (!text) {
          cover.ok(key);
          continue;
        }
        cover.bad(key);
        flags.push(text);
        flag("node", board.enclosure.id, text);
        for (const chip of board.chips) flag("node", chip.id, text);
      }
      if (singlePath) notes.push(`A complete survey found one SAS HBA (${controllers[0].label}) and one expander per backplane: this fabric is single-path as built, so there is no second path to check. That is not a fault of the read.`);
      else if (controllers.length < 2) flags.push(`Only ${controllers.length === 1 ? `one HBA (${controllers[0].label})` : "no HBA"} is in the payload: no second path`);
      const firmware = controllers.map((entry) => [entry.label, sas.controllers.get(entry.id)?.firmware || ""]).filter(([, value]) => value);
      if (new Set(firmware.map(([, value]) => value)).size > 1) notes.push(`HBA firmware differs: ${firmware.map(([label, value]) => `${label} ${value}`).join(", ")} (information, not a finding).`);
      check("redundancy", "Each backplane has an expander via each HBA", flags, unread, passes.join("; "), notes, cover);
    }

    // (b) Cascades: on one HBA's side, between boards, in level order. A
    // cable into an expander that the payload did not resolve is a cascade
    // not read, never "no cascade".
    {
      checking = "cascade order";
      severity = "warning";
      const flags = [];
      const unread = [...incomplete];
      const cover = coverage("cascade");
      const cascades = model.links.filter((link) => link.kind === "expander-expander");
      const orphans = unparented.filter((entry) => entry.level !== 1);
      for (const link of cascades) cover.expect(link.id, cableName(model, link));
      for (const entry of orphans) cover.expect(`into:${entry.id}`, `the cable into ${context.nodeName(model, entry.id)}`);
      for (const entry of orphans) {
        cover.unread(`into:${entry.id}`);
        unread.push(`${context.nodeName(model, entry.id)}: the cable into it is not resolved in the payload, so whether it is a cascade, and from where, is not read`);
      }
      for (const link of cascades) {
        const [source, target] = [model.nodeById.get(link.source), model.nodeById.get(link.target)];
        const name = cableName(model, link);
        const found = [];
        let blind = false;
        // Sides by the payload's HBA id at both ends; a missing id is not read.
        if (!source?.controllerId || !target?.controllerId) {
          blind = true;
          unread.push(`${name}: the HBA side of ${!source?.controllerId && !target?.controllerId ? "both ends" : "an end"} is not read`);
        } else if (source.controllerId !== target.controllerId) found.push(`joins an expander via ${labelOf(source.controllerId)} to one via ${labelOf(target.controllerId)}`);
        // A neighbour is another backplane only where its enclosure was read.
        const boardFor = (entry) => boardOf.get(entry?.id) || (entry?.neighbour?.known ? { name: entry.neighbour.name } : null);
        const [from, to] = [boardFor(source), boardFor(target)];
        if (from && to && from === to) found.push(`joins two expanders on the same backplane (${from.name})`);
        else if (!from || !to) {
          blind = true;
          unread.push(`${name}: the backplane of an end is not read`);
        }
        if (source?.level === null || target?.level === null || source?.level === undefined || target?.level === undefined) {
          blind = true;
          unread.push(`${name}: an expander's level is not reported`);
        } else if (target.level !== source.level + 1) found.push(`runs from level ${source.level} to level ${target.level}`);
        if (found.length) {
          cover.bad(link.id);
          const text = `${name}: ${found.join("; ")}`;
          flags.push(text);
          flag("link", link.id, text);
        } else if (blind) cover.unread(link.id);
        else cover.ok(link.id);
      }
      check("cascades", "Each cascade stays on one HBA's side and joins two backplanes in level order", flags, unread,
        cascades.length ? `${context.plural(cascades.length, "cascade")}, each on one HBA's side, from an expander on one backplane to the next level on another` : "no cascade cable is in the payload: every expander here is cabled to an HBA",
        cascades.length ? ["The payload does not report which chip on a board is primary or secondary, so a cascade crossed between them cannot be told from a straight one."] : [], cover, { empty: true });
    }

    // (c) Lanes: the same width on every cable, every lane at its PHY
    // maximum. Every cable is a subject, and so is a cable the payload
    // could not resolve.
    {
      checking = "lane count or rate";
      severity = "warning";
      const flags = [];
      const unread = [...incomplete];
      const cover = coverage("cable");
      let farInferred = 0;
      for (const link of cables) cover.expect(link.id, cableName(model, link));
      for (const entry of unparented) {
        cover.expect(`into:${entry.id}`, `the cable into ${context.nodeName(model, entry.id)}`);
        cover.unread(`into:${entry.id}`);
        unread.push(`${context.nodeName(model, entry.id)}: the cable into it is not resolved in the payload, so its lanes are not read`);
      }
      // Cables are compared with the other cables into the same backplane
      // (the two sides of a dual-path chain), never with every cable of the
      // fabric: an x8 uplink beside x4 cascades is not a finding.
      const into = (link) => {
        const target = chipOf(link.target);
        return boardOf.get(link.target)?.enclosure.id || (target?.neighbour?.enclosureId ? `enclosure:${target.neighbour.enclosureId}` : target?.id) || link.target;
      };
      const widths = [...new Set(cables.filter((link) => link.lanes).map((link) => link.lanes.count))].sort((left, right) => right - left);
      for (const link of cables) {
        const name = cableName(model, link);
        if (!link.lanes) {
          cover.unread(link.id);
          unread.push(`${name}: lanes not reported`);
          continue;
        }
        const found = [];
        let blind = false;
        const widest = Math.max(...cables.filter((other) => other.lanes && into(other) === into(link)).map((other) => other.lanes.count));
        if (link.lanes.count < widest) found.push(`x${link.lanes.count} where another cable into the same backplane is x${widest}`);
        const slow = slowLanes(model, link);
        if (slow.length) found.push(`${context.plural(slow.length, "lane")} below the maximum of both ends: ${slow.map((lane) => `PHY ${lane.source} ↔ ${lane.target} at ${lane.rate} Gb/s (both ends ${lane.cap})`).join(", ")}`);
        // A lane is judged against both of its ends only where the payload
        // pairs its PHYs; one end's maximum alone says nothing about the lane.
        const ends = unpairedPhys(model, link);
        if (!link.lanes.targetPhys.length && link.lanes.targetBasis !== "inferred") {
          blind = true;
          unread.push(`${name}: far end not read, so no lane is judged against both of its ends`);
        } else if (ends.length) {
          blind = true;
          unread.push(`${name}: which PHY pairs with which is not read, so no lane is judged against the maximum of both of its ends`);
        }
        const lost = ends.length ? 0 : link.lanes.count - link.lanes.rows.filter((lane) => lane.rate !== null && laneCap(model, link, lane) !== null).length;
        if (lost > 0) {
          blind = true;
          unread.push(`${name}: rate or an end's maximum not read for ${context.plural(lost, "lane")}`);
        }
        if (link.lanes.targetBasis === "inferred") farInferred += 1;
        if (found.length) {
          cover.bad(link.id);
          const text = tiered(`${name}: ${found.join("; ")}`, slow.length ? "critical" : "warning");
          flags.push(text);
          flag("link", link.id, text);
        } else if (blind) cover.unread(link.id);
        else cover.ok(link.id);
      }
      check("lanes", "Every cable has the same lane count and every lane runs at the maximum of both its ends", flags, unread, `${context.plural(cables.length, "cable")}${widths.length === 1 ? `, each x${widths[0]}` : ` (${widths.map((width) => `x${width}`).join(", ")})`}, every lane at the maximum of both its ends`,
        ["A lane is judged only where both of its ends report their maximum: below it is critical; a cable narrower than another into the same backplane is a warning. Disk ports are not judged for rate: the drive's maximum is not in the payload.",
          ...(farInferred ? [`The far end of ${context.plural(farInferred, "cable")} is inferred (the child expander's only linked PHYs in no port): sysfs has a port on the parent only.`] : [])], cover);
    }

    // (d) Bay ports: a path via each HBA, on the bay's own board. Every bay
    // that holds a disk or may hold one is a subject, on a board or not. A
    // SATA disk has one port: its paths share one expander, which is expected.
    const portAt = new Map();
    const phyKey = (id, phy) => `${chipOf(id)?.id || id}|${phy}`;
    // Bays with paths the collector did not join, by bay: their PHYs are not read.
    const unjoinedPaths = new Map();
    {
      checking = "port placement";
      severity = "warning";
      const flags = [];
      const unread = [...incomplete];
      const notes = [];
      const cover = coverage("bay");
      let sas2 = 0;
      let sata = 0;
      const subjects = model.bays.filter((bay) => bay.occupancy !== "no");
      for (const bay of subjects) cover.expect(bay.bayId, bay.label);
      const onBoards = new Set(boards.flatMap((board) => board.enclosure.group.bays.map((bay) => bay.bayId)));
      const outside = subjects.filter((bay) => !onBoards.has(bay.bayId));
      for (const bay of outside) cover.unread(bay.bayId);
      if (outside.length) unread.push(`${outside.map((bay) => bay.label).join(", ")}: not in any backplane read here, so ${outside.length === 1 ? "its" : "their"} placement is not read`);
      for (const board of boards) {
        for (const bay of board.enclosure.group.bays) for (const port of bay.ports) if (port.phy) portAt.set(phyKey(port.expanderId, port.phy.phy), { port, bay });
      }
      for (const board of boards) {
        // By id; each is named by its label where the check says so.
        const hbas = [...new Set(board.chips.flatMap((chip) => rowsOf(chip).map((row) => row.controllerId)))].filter(Boolean);
        const unmapped = [];
        const single = new Map();
        const unsure = new Map();
        const unjoined = [];
        const unknownBays = [];
        let blind = 0;
        for (const bay of board.enclosure.group.bays) {
          if (bay.occupancy === "no") continue;
          if (bay.occupancy !== "yes") {
            unknownBays.push(bay);
            cover.unread(bay.bayId);
            continue;
          }
          if (!bay.ports.length) {
            unmapped.push(bay);
            continue;
          }
          const found = [];
          let open = false;
          const used = [...new Set(bay.ports.map((port) => chipOf(port.expanderId)).filter(Boolean))];
          for (const port of bay.ports) {
            const chip = chipOf(port.expanderId);
            const landed = chip ? boardOf.get(port.expanderId) || boardOf.get(chip.id) || null : null;
            if (chip && landed !== board) {
              const text = `its path via ${port.controller || "an HBA not read"} lands on ${landed ? `an expander of ${landed.name}` : `${chip.label}, an expander outside this backplane`}`;
              found.push(text);
              flag("port", port.key, `${bay.label}: ${text}`);
            } else if (chip && !port.phy) {
              blind += 1;
              open = true;
            }
          }
          const unknown = bay.ports.filter((port) => !chipOf(port.expanderId));
          const ownerless = bay.ports.some((port) => !port.controllerId);
          if (bay.ports.every((port) => port.protocol === "SATA")) {
            // One port, behind one expander: every HBA that reaches that
            // expander has a path, and that is all a SATA disk can have.
            // Where that expander is not read, its placement is not read.
            if (!used.length) {
              open = true;
              unread.push(`${bay.label}: SATA, one port, through an expander not read`);
            } else if (!found.length) {
              sata += 1;
              const key = used.map((chip) => chip.label).join(" and ");
              if (!single.has(key)) single.set(key, []);
              single.get(key).push(bay.label);
            }
          } else {
            // No path at all via an HBA is evidence only where every path of
            // the bay names its HBA. Which expander a path uses is evidence
            // only where the payload has the path's port row.
            const missing = hbas.filter((id) => !bay.ports.some((port) => port.controllerId === id));
            // The transport collector joins one block device per slot, so a
            // second path through another HBA is not read, not missing. A
            // head end's own survey does read that head's path.
            if (missing.length && ((model.transport && !model.shared) || ownerless)) {
              open = true;
              unjoined.push(`${bay.label}: via ${missing.map(labelOf).join(", ")}`);
              unjoinedPaths.set(bay.bayId, missing.length);
            } else if (missing.length) found.push(`no path via ${missing.map(labelOf).join(", ")}`);
            if (unknown.length) {
              open = true;
              const shape = `${context.plural(bay.ports.length, "path")}: ${hbas.map((id) => `${bay.ports.filter((port) => port.controllerId === id).length} via ${labelOf(id)}`).join(", ")}`;
              if (!unsure.has(shape)) unsure.set(shape, []);
              unsure.get(shape).push(bay.label);
            } else if (!found.length) {
              const pairs = bay.ports.map((port) => `${port.controllerId || port.controller}|${chipOf(port.expanderId).id}`);
              for (const pair of new Set(pairs.filter((pair, index) => pairs.indexOf(pair) !== index))) found.push(`two paths via ${model.nodeById.get(pair.split("|")[0])?.label || pair.split("|")[0]} through ${chipOf(pair.split("|")[1]).label}`);
              if (board.chips.length > 1 && used.length === 1 && bay.ports.length > 1) found.push(`every path runs through ${used[0].label}: one of its two ports is in use`);
              if (!found.length && !open) sas2 += 1;
            }
          }
          if (found.length) {
            cover.bad(bay.bayId);
            const text = `${bay.label}: ${found.join("; ")}`;
            flags.push(text);
            flag("bay", bay.bayId, text);
          } else if (open) cover.unread(bay.bayId);
          else cover.ok(bay.bayId);
        }
        const spareText = board.chips.filter((chip) => context.list(chip.unmatched).length).map((chip) => `${chip.unmatchedText} of ${chip.label}`).join("; ");
        for (const [shape, labels] of unsure) {
          unread.push(`${board.name}: ${labels.join(", ")}: ${shape.replace(/^(\d+ paths?): (.*)$/, labels.length > 1 ? "$1 each ($2)" : "$1 ($2)")}. Which expander each path uses is not read: the payload has no port rows for ${labels.length > 1 ? "these disks" : "this disk"}.${spareText ? ` Reported, but not matched to a bay: ${spareText}.` : ""}`);
        }
        if (unjoined.length) unread.push(`${board.name}: a path via another HBA is not read for ${context.plural(unjoined.length, "disk")} (${model.transport && !model.shared ? "the collector joins one block device per bay" : "a path's HBA is not read"}): ${unjoined.slice(0, 6).join("; ")}${unjoined.length > 6 ? `; and ${unjoined.length - 6} more` : ""}`);
        if (unknownBays.length) unread.push(`${board.name}: ${unknownBays.map((bay) => bay.label).join(", ")}: whether ${unknownBays.length === 1 ? "the bay holds" : "these bays hold"} a disk is not read`);
        for (const [chip, labels] of single) notes.push(`${board.name}: ${labels.join(", ")}: SATA: one port, through ${chip}. Expected for SATA; not counted against redundancy.`);
        if (blind) unread.push(`${board.name}: the expander PHY of ${context.plural(blind, "disk port")} is not read`);
        if (!unmapped.length) continue;
        // A disk the payload maps to no port. The transport collector joins
        // a bay to its disk by block device: without the join, its port is
        // not read. Otherwise it is unread when every expander on the board
        // still reports that many targets no bay is mapped to.
        const spare = board.chips.map((chip) => (sas.expanders.get(chip.id)?.phys || []).filter((row) => /^(SAS|SATA) Target$/.test(row.device) && row.handle && !portAt.has(phyKey(chip.id, row.phy))).length);
        const labels = unmapped.map((bay) => bay.label).join(", ");
        if (model.transport) {
          for (const bay of unmapped) cover.unread(bay.bayId);
          unread.push(`${board.name}: ${labels}: the disk's block device is not joined to a port, so ${unmapped.length === 1 ? "its" : "their"} path is not read`);
        } else if (spare.length && spare.every((count) => count >= unmapped.length)) {
          for (const bay of unmapped) cover.unread(bay.bayId);
          unread.push(`${board.name}: ${labels} ${unmapped.length === 1 ? "holds a disk" : "hold disks"} the payload maps to no port; each expander on the board reports ${context.plural(Math.min(...spare), "disk target")} that no bay is mapped to`);
        } else {
          for (const bay of unmapped) cover.bad(bay.bayId);
          const text = `${labels}: no path is reported for ${unmapped.length === 1 ? "this disk" : "these disks"}`;
          flags.push(`${board.name}: ${text}`);
          for (const bay of unmapped) flag("bay", bay.bayId, `${bay.label}: no path is reported for this disk`);
        }
      }
      const counted = [sas2 ? `${context.plural(sas2, "disk")} with a path via each HBA on its own backplane` : "", sata ? `${context.plural(sata, "SATA disk")}, one port each` : ""].filter(Boolean).join("; ");
      check("ports", "Each disk has a path via each HBA, on its own backplane's expanders", flags, unread, counted || "no disk is in these bays", notes, cover, { empty: true });
    }

    // The PHY of one expander as every observation of it reports it: a chip
    // seen via several HBAs is one PHY with several readings. Never summed;
    // the worst measured reading stands, and readings that differ are said.
    const readings = (id, phy) => {
      const chip = chipOf(id);
      const ids = chip ? [...new Set([chip.id, ...rowsOf(chip).map((row) => row.id)])] : [id];
      const rows = ids.map((owner) => sas.expanders.get(owner)?.byPhy.get(phy)).filter(Boolean);
      const measured = rows.filter((row) => row.measured);
      const worst = measured.reduce((best, row) => (!best || row.errors > best.errors ? row : best), null);
      const disagree = new Set(measured.map((row) => JSON.stringify(row.counters))).size > 1;
      return { rows, measured, worst, disagree, read: measured.length > 0 && measured.length === rows.length };
    };
    // Every PHY any observation of a chip reports, once per PHY number.
    const chipPhys = (view) => {
      const chip = chipOf(view.id);
      const byPhy = new Map();
      for (const owner of chip ? [view.id, ...rowsOf(chip).map((row) => row.id)] : [view.id]) {
        for (const row of sas.expanders.get(owner)?.phys || []) if (!byPhy.has(row.phy)) byPhy.set(row.phy, row);
      }
      return [...byPhy.values()];
    };
    const disagreeText = (reading) => (reading.disagree ? `; the ${context.plural(reading.rows.length, "observation")} of this PHY disagree, the worst is shown` : "");

    // (e) Cables: error counters on each end of each cable the platform
    // reads (an HBA's own PHYs only where its counters are collected), on a
    // cable the payload could not resolve, and on any other PHY an HBA or an
    // expander is attached to.
    const cablePhys = new Set();
    const views = [...sas.expanders.values()].filter((item) => model.nodeById.has(item.id) && !model.nodeById.get(item.id).mergedInto);
    {
      checking = "PHY errors on the cable";
      severity = "warning";
      const flags = [];
      const unread = [...incomplete];
      const cover = coverage("cable end");
      const hbaCounters = Boolean(model.transport);
      for (const entry of unparented) {
        cover.expect(`into:${entry.id}`, `the cable into ${context.nodeName(model, entry.id)}`);
        cover.unread(`into:${entry.id}`);
        unread.push(`${context.nodeName(model, entry.id)}: the cable into it is not resolved in the payload, so its counters are not read`);
      }
      for (const link of cables) {
        const name = cableName(model, link);
        const hbaEnd = model.nodeById.get(link.source)?.kind === "controller";
        const sides = [["near", link.source], ["far", link.target]].filter(([side]) => side === "far" || !hbaEnd || hbaCounters);
        for (const [side] of sides) cover.expect(`${link.id}|${side}`, `${name} at its ${side} end`);
        if (!link.lanes) {
          for (const [side] of sides) cover.unread(`${link.id}|${side}`);
          unread.push(`${name}: lanes not reported, so the PHYs the cable uses and their counters are not read`);
          continue;
        }
        for (const end of cableEnds(model, link)) {
          const side = end.id === link.source ? "near" : "far";
          const key = `${link.id}|${side}`;
          if (end.hba && !hbaCounters) continue;
          if (!end.wanted) {
            cover.unread(key);
            unread.push(`${name}: which PHYs it uses at ${end.label} is not read`);
            continue;
          }
          const rows = end.hba ? end.rows : end.rows.map((row) => readings(end.id, row.phy));
          const measured = end.hba ? end.rows.filter((row) => row.measured) : rows.filter((reading) => reading.read).map((reading) => reading.worst);
          if (!end.hba) for (const row of end.rows) cablePhys.add(`${end.chipId}|${row.phy}`);
          const shown = end.hba ? end.rows : rows.map((reading) => reading.worst || reading.rows[0]).filter(Boolean);
          const summary = context.errorSummary(shown, end.wanted);
          if (summary.warn) {
            cover.bad(key);
            const text = tiered(`${name}: at ${end.label} ${context.errorDigest(shown)}${end.hba ? "" : rows.map(disagreeText).find(Boolean) || ""}`, summary.warn);
            flags.push(text);
            flag("link", link.id, text);
          }
          const lost = end.wanted - measured.length;
          if (lost > 0) {
            cover.unread(key);
            unread.push(`${name}: counters not read for ${context.plural(lost, "PHY")} at ${end.label}`);
          } else cover.ok(key);
        }
      }
      for (const view of views) {
        for (const row of chipPhys(view)) {
          const uplink = row.device === "SAS Initiator" ? "an HBA" : row.device === "SMP Target" ? "another expander" : "";
          if (!uplink || cablePhys.has(`${view.id}|${row.phy}`)) continue;
          const key = `phy:${view.id}|${row.phy}`;
          const reading = readings(view.id, row.phy);
          const where = `${context.nodeName(model, view.id)}: PHY ${row.phy}, attached to ${uplink} (${row.device}) and in no cable the payload reports`;
          cover.expect(key, where);
          cablePhys.add(`${view.id}|${row.phy}`);
          if (reading.worst?.errors) {
            cover.bad(key);
            const text = tiered(`${where}: ${context.counterText(reading.worst)}${disagreeText(reading)}`, context.phyTier(reading.worst));
            flags.push(text);
            flag("node", view.id, text);
          } else if (!reading.read) {
            cover.unread(key);
            unread.push(`${where}: counters not read`);
          } else cover.ok(key);
        }
      }
      const read = cover.count("ok") + cover.count("bad");
      check("cable-errors", "No PHY errors on the cables' lanes", flags, unread, `no errors at the ${context.plural(read, "cable end")} the cables use`,
        [!hbaCounters ? "Error counters for the HBA's own PHYs are not collected on this platform: each cable is checked at its expander ends."
          : read === cover.total() && !cover.unmarked().length ? "Each cable is checked at both of its ends." : "Each cable is checked at each end whose PHYs are read.",
          "The counters are cumulative since the expander last reset; this view cannot tell when that was.", context.THRESHOLD_NOTE, context.TREND_NOTE], cover);
    }

    // (f) Disk ports: every path of every bay that holds or may hold a
    // disk, by its PHY on every observation of its expander; a path the
    // collector did not join; and every disk PHY no bay is matched to.
    {
      checking = "PHY errors on a disk port";
      severity = "warning";
      const flags = [];
      const unread = [...incomplete];
      const cover = coverage("disk port");
      const byBay = new Map();
      const notes = [];
      let both = false;
      let disagree = false;
      const blindPaths = [];
      for (const bay of model.bays.filter((item) => item.occupancy !== "no")) {
        const unjoined = unjoinedPaths.get(bay.bayId) || 0;
        for (let index = 0; index < unjoined; index += 1) {
          cover.expect(`${bay.bayId}#unjoined${index}`, `${bay.label}: a path not joined`);
          cover.unread(`${bay.bayId}#unjoined${index}`);
        }
        if (unjoined) blindPaths.push(`${bay.label}: ${context.plural(unjoined, "path")} not joined`);
        if (!bay.ports.length) {
          if (bay.occupancy === "yes") {
            cover.expect(`${bay.bayId}#none`, `${bay.label}: its port`);
            cover.unread(`${bay.bayId}#none`);
            blindPaths.push(`${bay.label}: no port read`);
          }
          continue;
        }
        for (const port of bay.ports) {
          if (!port.phy) {
            cover.expect(port.key, `${bay.label}: path via ${port.controller || "an HBA not read"}`);
            cover.unread(port.key);
            blindPaths.push(`${bay.label}: a path matched to no expander PHY`);
            continue;
          }
          const key = phyKey(port.expanderId, port.phy.phy);
          cover.expect(key, `${bay.label}: PHY ${port.phy.phy} of ${chipOf(port.expanderId)?.label || port.expanderId}`);
        }
      }
      for (const view of views) {
        const entry = model.nodeById.get(view.id);
        for (const row of chipPhys(view)) {
          const key = `${view.id}|${row.phy}`;
          if (cablePhys.has(key) || row.device === "SAS Initiator" || row.device === "SMP Target") continue;
          const at = portAt.get(key);
          const disk = at || (/Target$|End device/.test(row.device) && !/^SEP/.test(row.device));
          const reading = readings(view.id, row.phy);
          if (!at && disk) cover.expect(key, `${entry.label}: PHY ${row.phy}, a disk port no bay is matched to`);
          if (!cover.has(key)) {
            // Anything else: a fault is still a fault; quiet is not judged.
            if (!reading.worst?.errors) continue;
          }
          if (reading.disagree) disagree = true;
          if (reading.worst?.errors) {
            if (cover.has(key)) cover.bad(key);
            if (at) {
              if (!byBay.has(at.bay)) byBay.set(at.bay, []);
              // A chip two HBAs reach has one PHY per disk port: named once.
              if (!byBay.get(at.bay).some((item) => item.chip === entry && item.row.phy === row.phy)) byBay.get(at.bay).push({ port: at.port, row: reading.worst, chip: entry, reading });
              for (const port of at.bay.ports) if (chipOf(port.expanderId) === entry && port.phy?.phy === row.phy) flag("port", port.key, `PHY ${row.phy} of ${entry.label}: ${context.counterText(reading.worst)}`, context.phyTier(reading.worst));
            } else {
              const what = /Target$/.test(row.device) && !/^SEP/.test(row.device) ? `a disk port (${row.device}${row.handle ? `, handle ${row.handle}` : ""}) mapped to no bay`
                : /^SEP/.test(row.device) ? "the enclosure processor (SEP Target)" : `${row.device || "nothing reported"}`;
              const text = tiered(`${entry.label}: PHY ${row.phy}, ${what}: ${context.counterText(reading.worst)}${disagreeText(reading)}`, context.phyTier(reading.worst));
              flags.push(text);
              flag("node", entry.id, text);
            }
          } else if (cover.has(key)) {
            if (reading.read) cover.ok(key);
            else cover.unread(key);
          }
        }
      }
      for (const [bay, found] of byBay) {
        const text = tiered(`${bay.label}${bay.deviceName ? ` (${bay.deviceName})` : ""}, ${found.length > 1 ? "both ports" : "one port"}: ${found.map((item) => `${item.chip.label} PHY ${item.row.phy}: ${context.counterText(item.row)}${disagreeText(item.reading)}`).join("; ")}`, context.worse(...found.map((item) => context.phyTier(item.row))));
        flags.push(text);
        flag("bay", bay.bayId, text);
        if (found.length > 1) both = true;
      }
      const unreadPhys = cover.keys("unread").filter((key) => !key.includes("#") && !key.startsWith("bay:")).length;
      if (unreadPhys) unread.push(`counters not read for ${context.plural(unreadPhys, "disk port PHY")}`);
      if (blindPaths.length) unread.push(`${context.plural(blindPaths.length, "disk path")} not attributed to a read PHY: ${blindPaths.slice(0, 6).join("; ")}${blindPaths.length > 6 ? `; and ${blindPaths.length - 6} more` : ""}`);
      if (both) notes.push("Errors on both ports of one drive point at the drive or its bay, not at a cable.");
      if (disagree) notes.push("Where the observations of one PHY via different HBAs disagree, the worst measured reading is shown, never a sum.");
      if (flags.length) notes.push("The counters are cumulative since the expander last reset; this view cannot tell when that was.", context.THRESHOLD_NOTE, context.TREND_NOTE);
      flags.sort(context.naturalCompare);
      check("port-errors", "No PHY errors on the disk ports", flags, unread, `no errors on ${context.plural(cover.count("ok"), "disk port PHY")}`, notes, cover);
    }

    // (g) Path states, per HBA side.
    pathStates();
  }

function flagTier(model, kind, id) {
    return context.worse(...flagsOf(model, kind, id).map((item) => item.tier));
  }

function flagRows(model, kind, id) {
    const flags = flagsOf(model, kind, id);
    return ["critical", "warning"].map((tier) => ({ tier, shorts: [...new Set(flags.filter((item) => item.tier === tier).map((item) => item.short))] }))
      .filter((row) => row.shorts.length).map((row) => ({ label: context.TIER_WORDS[row.tier], value: row.shorts.join("; "), warn: row.tier }));
  }

function flagsOf(model, kind, id) {
    return context.list(model?.flags.get(`${kind}:${id}`));
  }

function headRows(model, entry) {
    const head = entry.head;
    const info = model.shared.heads.find((item) => item.controller_id === entry.id);
    const paths = model.bays.flatMap((bay) => bay.ports.filter((port) => port.controller === (info?.hba_label || entry.label)));
    const named = paths.filter((port) => port.fromPath);
    const chips = [...new Set(paths.map((port) => context.chipOfNode(model, port.expanderId)).filter(Boolean))];
    return [
      { label: "System", value: head.system },
      { label: "Role", value: head.role === "active" ? `active (pool owner): ${head.pool}` : head.role ? `standby: ${head.pool} active on ${head.owner}` : context.NOT_READ },
      { label: "I/O fencing", value: head.fencing === null ? context.NOT_READ : `${head.fencing ? "enabled" : "disabled"}${head.fence ? `, fence owner ${head.fence}` : ""}` },
      { label: "Disks reported", value: paths.length ? `${paths.length}: ${stateCounts(paths.map((port) => `${port.basis === context.MEMBER_HEALTH ? "member health " : ""}${port.state}`))}` : context.NOT_READ },
      model.shared.transport ? { label: "Transport", value: `${info.hba_label} → ${chips.map((chip) => `${chip.label} (${chip.address})`).join(", ") || `expander ${context.NOT_READ}`}` }
        : { label: "Transport", value: `${context.NOT_READ}: ${context.notCollectedReason({ warnings: model.warnings })}${named.length ? ` ${context.plural(named.length, "device path")} ${named.length === 1 ? "names" : "name"} an expander: ${named.map((port) => `${model.bayById.get(port.bayId)?.label} via ${port.attached} PHY ${port.pathPhy}`).join("; ")}.` : ""}` },
    ];
  }

function hbaRows(model, entry) {
    const record = model.sas.controllers.get(entry.id);
    if (!record) return [{ label: "HBA detail", value: context.NOT_READ }];
    const { linked, drawn, inside, outside } = hbaLanes(model, entry);
    const rates = (rows) => [...new Set(rows.map((row) => row.rate))].sort((a, b) => b - a).map((rate) => `${rows.filter((row) => row.rate === rate).length} at ${rate} Gb/s`).join(", ");
    // A lane of a cable drawn here is judged against both of its ends.
    const slow = inside.filter((row) => slowLanes(model, drawn.get(row.phy)).some((lane) => lane.source === row.phy));
    const bad = record.pathCounts.some(([state, count]) => count > 0 && ["fault", "missing"].includes(context.pathTone(state)));
    const rows = [
      { label: "Board", value: [record.board, record.chip].filter(Boolean).join(" · ") || context.NOT_READ },
      { label: "Firmware", value: record.firmware || context.NOT_READ },
      { label: "Temperature", value: record.temperature || context.NOT_READ },
      { label: "Slot", value: [record.slot, record.pci].filter(Boolean).join(" · ") || context.NOT_READ },
      ...(record.sasAddress ? [{ label: "SAS address", value: record.sasAddress }] : []),
      { label: "PHYs", value: record.phys.length ? `${linked.length} of ${record.phys.length} linked` : context.NOT_READ },
    ];
    // Only a lane of a cable drawn here is this view's. A lane to something
    // else is said, with where it goes: its far end is not in this payload,
    // so its rate is not judged here.
    if (linked.length) {
      rows.push({
        label: "In this view",
        value: inside.length ? `${context.plural(inside.length, "lane")} on PHY ${context.phyRanges(inside.map((row) => row.phy))}: ${rates(inside)}${slow.length ? `; PHY ${context.phyRanges(slow.map((row) => row.phy))} below the maximum of both ends` : ""}` : "no lane of a cable drawn here",
        warn: slow.length > 0,
      });
    }
    if (outside.length) {
      const handles = [...new Set(outside.map((row) => row.handle).filter(Boolean))];
      const others = context.list(model.otherEnclosures);
      const lowest = Math.min(...outside.map((row) => row.rate));
      const lower = outside.filter((row) => row.rate === lowest && outside.some((other) => other.rate > lowest));
      rows.push({
        label: "Outside this view",
        value: `${context.plural(outside.length, "lane")} on PHY ${context.phyRanges(outside.map((row) => row.phy))}: ${rates(outside)}`
          + `${lower.length ? ` (PHY ${context.phyRanges(lower.map((row) => row.phy))})` : ""}`
          + `. ${outside.length === 1 ? "It goes" : "They go"} to ${handles.length ? `device handle ${handles.join(", ")}` : "a device"} that is not in this enclosure's payload`
          + `${others.length ? `; this system's other ${others.length === 1 ? "enclosure is" : "enclosures are"} ${others.join(", ")}` : ""}`,
      });
    }
    const paths = model.bays.flatMap((bay) => bay.ports.filter((port) => port.controllerId === entry.id));
    rows.push({ label: "Disk paths", value: record.pathCounts.length ? record.pathCounts.map(([state, count]) => `${count} ${state}`).join(", ") : paths.length ? stateCounts(paths.map((port) => `${port.basis === context.MEMBER_HEALTH ? "member health " : ""}${port.state}`)) : context.NOT_READ, warn: bad });
    return rows;
  }

function hbaLanes(model, entry) {
    const record = model.sas.controllers.get(entry.id);
    const drawn = new Map();
    for (const link of context.list(model.outgoing.get(entry.id))) {
      for (const phy of context.list(link.lanes?.sourcePhys)) drawn.set(phy, link);
    }
    const linked = context.list(record?.phys).filter((row) => row.rate !== null);
    return { linked, drawn, inside: linked.filter((row) => drawn.has(row.phy)), outside: linked.filter((row) => !drawn.has(row.phy)) };
  }

function phyPeer(model, expanderId, row) {
    const rows = context.list(model.nodeById.get(expanderId)?.rows).map((item) => item.id);
    const ids = rows.length ? rows : [expanderId];
    for (const link of model.links) {
      if (!link.lanes) continue;
      if (ids.includes(link.target) && link.lanes.targetPhys.includes(row.phy)) return `cable from ${context.nodeName(model, link.source)}${link.lanes.targetBasis === "inferred" ? ", inferred" : ""}`;
      if (ids.includes(link.source) && link.lanes.sourcePhys.includes(row.phy)) return `cable to ${context.nodeName(model, link.target)}`;
    }
    for (const port of model.portByKey.values()) {
      if (ids.includes(port.expanderId) && port.phy?.phy === row.phy) return model.bayById.get(port.bayId)?.label || port.bayId;
    }
    if (!row.device && row.rate !== null) return "linked, device not identified";
    const role = { "SAS Initiator": "HBA uplink in no reported cable", "SMP Target": "another expander, in no reported cable", "SEP Target": "enclosure processor" }[row.device];
    return role ? `${role} (${row.device}${row.handle ? `, handle ${row.handle}` : ""})` : row.device ? `${row.device}${row.handle ? `, handle ${row.handle}` : ""}` : "nothing reported";
  }

function expanderRows(model, entry) {
    const record = model.sas.expanders.get(entry.id);
    const reached = entry.viaLine || context.viaText(model.nodeById.get(entry.controllerId)?.label, entry.level);
    if (entry.reported) {
      const named = entry.fromPath ? [...model.portByKey.values()].filter((port) => port.expanderId === entry.id) : [];
      return [
        { label: "SAS address", value: entry.address },
        { label: "Known from", value: entry.fromPath
          ? `the device path of ${named.map((port) => `${model.bayById.get(port.bayId)?.label || port.bayId} as ${port.controller} names it (PHY ${port.pathPhy})`).join("; ")}`
          : `${context.plural(entry.reported, "disk port")} that report this address as the one they are attached to` },
        { label: "PHYs", value: `${context.NOT_READ}: the expander's own PHY table is not in the payload` },
        { label: "Uplink", value: `${context.NOT_READ}: ${model.shared ? "its link to a head end" : "which HBA reaches it"} is not in the payload` },
        { label: "PHY errors", value: context.NOT_READ },
      ];
    }
    if (!record) return [{ label: "Reached", value: reached }, ...context.neighbourRows(model, entry), { label: "Expander detail", value: context.NOT_READ }];
    const bad = record.phys.filter((row) => row.errors > 0);
    const measured = record.phys.filter((row) => row.measured).length;
    const log = record.log;
    // A chip two HBAs reach has a PHY table from each: the same chip, so the
    // same counters; a difference between the two reads is said.
    const others = context.list(entry.rows).filter((row) => row.id !== entry.id).map((row) => {
      const other = model.sas.expanders.get(row.id);
      const name = model.nodeById.get(row.controllerId)?.label || row.id;
      if (!other) return `${name}: ${context.NOT_READ}`;
      const differ = record.phys.filter((mine) => {
        const theirs = other.byPhy.get(mine.phy);
        return !theirs || context.PHY_COUNTERS.some(([key]) => theirs.counters[key] !== mine.counters[key]);
      });
      return `${name}: ${differ.length ? `counters differ on PHY ${context.phyRanges(differ.map((row) => row.phy))}` : "same counters"}`;
    });
    // What is attached, as the source counts it; else counted from its PHY
    // rows, a linked PHY with no device named being said as that.
    const kinds = new Map();
    for (const row of record.phys) {
      const name = row.device || (row.rate !== null ? "linked, device not identified" : "");
      if (name) kinds.set(name, (kinds.get(name) || 0) + 1);
    }
    const attached = record.attached.length ? record.attached : [...kinds];
    return [
      { label: "SAS address", value: record.address || entry.address || entry.rawId || context.NOT_READ },
      ...(record.product ? [{ label: "Expander", value: record.product }] : []),
      ...(record.enclosureAddress ? [{ label: "Enclosure", value: `SAS address ${record.enclosureAddress}` }] : []),
      { label: "Reached", value: reached },
      ...(record.reportedLevel && entry.level !== null && String(entry.level) !== record.reportedLevel ? [{ label: "Level", value: `${entry.level} by its place in the chain; the driver's own level field says ${record.reportedLevel}` }] : []),
      ...context.neighbourRows(model, entry),
      { label: "PHYs", value: record.linked !== null && record.total !== null ? `${record.linked} of ${record.total} linked` : context.NOT_READ },
      { label: "Attached", value: attached.length ? attached.map(([name, count]) => `${count} ${name}`).join(" · ") : context.NOT_READ },
      ...(context.list(entry.unmatched).length ? [{ label: "Not matched", value: `${entry.unmatchedText} ${entry.unmatched.length === 1 ? "is" : "are"} attached (${[...new Set(entry.unmatched.map((row) => context.rateText(row.rate)))].join(", ")}); the payload has no port row that says which bay` }] : []),
      {
        label: "PHY errors",
        value: !measured ? context.NOT_READ : bad.length ? `${context.plural(bad.length, "PHY")} with errors: ${[...bad.reduce((groups, row) => {
          const peer = phyPeer(model, entry.id, row);
          return groups.set(peer, [...(groups.get(peer) || []), row.phy]);
        }, new Map())].map(([peer, phys]) => `PHY ${context.phyRanges(phys)} (${peer})`).join(", ")}` : `none on ${context.plural(measured, "PHY")}`,
        warn: context.worse(...bad.map(context.phyTier)),
      },
      { label: "Error log", value: log && log.state === "available" ? `read ${log.readAt || "at a time not reported"}${log.source ? ` · ${log.source}` : ""}` : `${context.NOT_READ}${log?.reason ? ` (${log.reason})` : ""}` },
      ...(others.length ? [{ label: "Also read via", value: others.join("; "), warn: others.some((line) => /differ/.test(line)) }] : []),
    ];
  }

function cableRows(model, link) {
    const rows = [];
    if (!link.lanes) return [{ label: "Lanes", value: "not reported" }];
    const rates = [...new Set(link.lanes.rows.map((lane) => lane.rate))].sort((a, b) => (b ?? 0) - (a ?? 0));
    const perRate = rates.map((rate) => `${link.lanes.rows.filter((lane) => lane.rate === rate).length} at ${rate === null ? `a rate ${context.NOT_READ}` : `${rate} Gb/s`}`);
    rows.push({ label: "Lanes", value: `${link.lanes.count}${perRate.length ? `: ${perRate.join(", ")}` : ` · ${context.formatRate(link.lanes.rate)}`}`, warn: rates.length > 1 });
    const parentOnly = link.lanes.basis === "sysfs_parent_port_membership";
    rows.push({ label: "PHYs", value: `${context.phyRanges(link.lanes.sourcePhys) || "?"} at ${context.nodeName(model, link.source)}${link.lanes.targetPhys.length ? ` ↔ ${context.phyRanges(link.lanes.targetPhys)} at ${context.nodeName(model, link.target)}` : ""}` });
    if (link.lanes.targetBasis === "inferred") rows.push({ label: "Far end", value: `inferred: the only linked PHYs of ${context.nodeName(model, link.target)} that are in no port. sysfs has a port on the parent only` });
    else if (!link.lanes.targetPhys.length && parentOnly) rows.push({ label: "Far end", value: `${context.NOT_READ}: sysfs has a port on the parent only` });
    // Each lane's own rate, at the end that is read.
    const near = unpairedPhys(model, link).filter((item) => item.side === "near" && item.row);
    const below = unpairedBelow(model, link);
    if (near.length) {
      const groups = new Map();
      for (const { row } of near) {
        const key = `${context.rateText(row.rate)}${row.max !== null ? ` (PHY max ${row.max})` : ""}`;
        groups.set(key, [...(groups.get(key) || []), row.phy]);
      }
      rows.push({ label: "Per lane", value: [...groups].map(([key, phys]) => `PHY ${context.phyRanges(phys)}: ${phys.length > 1 ? "each " : ""}${key}`).join("; "), warn: near.some(below) });
    }
    if (!link.lanes.rows.length) rows.push({ label: "Pairing", value: `${context.NOT_READ}: which PHY at one end is cabled to which at the other` });
    const slow = slowLanes(model, link);
    if (slow.length) rows.push({ label: "Below both ends' max", value: slow.map((lane) => `PHY ${lane.source} ↔ ${lane.target} at ${lane.rate} Gb/s (both ends ${lane.cap})`).join(", "), warn: true });
    for (const end of cableEnds(model, link)) {
      const summary = end.hba && !end.rows.length ? { text: `${context.NOT_READ} (HBA PHY counters are not in the payload)`, warn: false } : context.errorSummary(end.rows, end.wanted);
      rows.push({ label: `Errors at ${end.label}`, value: summary.text, warn: summary.warn });
    }
    return rows;
  }

function busPorts(model, link) {
    const group = model.groupById.get(link.target);
    const chip = context.chipOfNode(model, link.source);
    return context.list(group?.bays).flatMap((bay) => bay.ports.filter((port) => context.chipOfNode(model, port.expanderId) === chip).map((port) => ({ bay, port })));
  }

function busRows(model, link) {
    const group = model.groupById.get(link.target);
    const ports = busPorts(model, link);
    const bad = ports.filter((item) => item.port.phy?.errors > 0);
    const joined = ports.filter((item) => item.port.phy).length;
    const measured = ports.filter((item) => item.port.phy?.measured).length;
    const disks = new Set(ports.map((item) => item.bay.bayId)).size;
    return [
      { label: "Disks on this expander", value: `${disks} of ${context.plural(group.bays.length, "bay")}; ${context.plural(ports.length, "path")}, ${joined} joined to an expander PHY` },
      { label: "Path states", value: ports.length ? stateCounts(ports.map((item) => `${item.port.controller} ${item.port.state}`)) : context.NOT_READ, warn: ports.some((item) => ["fault", "missing"].includes(context.pathTone(item.port.state, item.port.basis))) },
      {
        label: "PHY errors",
        value: !measured ? context.NOT_READ : bad.length ? [...new Set(bad.map((item) => `${item.bay.label} PHY ${item.port.phy.phy}: ${context.counterText(item.port.phy)}`))].join("; ") : `none on ${context.plural(measured, "disk port PHY")}`,
        warn: context.worse(...bad.map((item) => context.phyTier(item.port.phy))),
      },
    ];
  }

function portRows(model, port) {
    const chip = context.chipOfNode(model, port.expanderId);
    const phy = port.phy;
    const tone = context.pathTone(port.state, port.basis);
    const role = model.roleOf?.get(port.controllerId) || model.roleOf?.get(port.controller);
    const rows = [
      ...(role ? [{ label: "Head end", value: `${role.head}: ${role.role === "active" ? "active (pool owner)" : role.role || `role ${context.NOT_READ}`}` }] : []),
      { label: "State", value: `${port.basis === context.MEMBER_HEALTH ? "member health " : ""}${port.state}`, warn: tone === "fault" || tone === "missing" },
      { label: "Path", value: `${port.controller || `${model.sideWord} not reported`} → ${chip ? `${chip.label}${chip.address ? ` (${chip.address})` : ""}` : `expander ${context.NOT_READ}`}${phy ? ` PHY ${phy.phy}${port.phyBasis ? ` (${port.phyBasis})` : ""}` : port.pathPhy !== null && port.pathPhy !== undefined ? ` PHY ${port.pathPhy} (from the device path)` : ""}` },
    ];
    if (port.protocol) rows.push({ label: "Disk", value: port.protocol === "SATA" ? "SATA: one port" : "SAS" });
    rows.push(
      // A disk port's rate is the drive's own: the expander PHY's maximum is
      // not the drive's, so the rate is shown and never judged.
      { label: "Rate", value: phy ? `${context.rateText(phy.rate)} (drive maximum not read)` : port.rate !== null ? `${port.rate} Gb/s (drive maximum not read)` : context.NOT_READ },
      {
        label: "PHY errors",
        value: !phy ? `${context.NOT_READ}: ${port.why}` : !phy.measured ? `${context.NOT_READ} (${phy.counterReason || phy.counterState})` : context.PHY_COUNTERS.map(([key, label]) => `${phy.counters[key]} ${label}`).join(" · "),
        warn: context.phyTier(phy),
      },
      { label: "Port SAS address", value: port.sasAddress || context.NOT_READ },
    );
    // What the disk itself reports (Linux SES): never an expander's own PHY.
    if (port.attached && !port.fromPath && !phy) rows.push({ label: "Attached to", value: port.attached });
    if (port.diskPhy) rows.push({ label: "Disk PHY", value: `${port.diskPhy} (the disk's own PHY; the expander PHY is ${phy ? `PHY ${phy.phy}` : context.NOT_READ})` });
    if (port.hctl) rows.push({ label: "SCSI address", value: `${port.hctl}${port.sg ? ` · ${port.sg}` : ""}` });
    return rows;
  }

function portBrief(port) {
    const phy = port.phy;
    const seen = phy ? `PHY ${phy.phy} · ${context.rateText(phy.rate)} · ${!phy.measured ? `errors ${context.NOT_READ}` : phy.errors ? context.counterText(phy) : "no PHY errors"}`
      : port.fromPath ? `expander ${port.attached} PHY ${port.pathPhy} (from the device path)` : port.attached ? `expander PHY ${context.NOT_READ}` : `transport ${context.NOT_READ}`;
    return [`${port.basis === context.MEMBER_HEALTH ? "member health " : ""}${port.state}`, port.sasAddress && !phy ? `port ${port.sasAddress}` : "", port.attached && !port.fromPath && !phy ? `attached to ${port.attached}` : "", seen, port.hctl ? `SCSI ${port.hctl}` : ""].filter(Boolean).join(" · ");
  }

function cardFor(model, ref) {
    if (!model || !ref) return null;
    context.tailDigits = model.tailDigits || 4;
    let card = null;
    if (ref.kind === "tier") {
      const tier = model.tiers.find((item) => item.id === ref.id);
      return tier ? { title: `${tier.label}: ${tier.note}`, rows: [{ label: "Evidence", value: tier.state }], flags: [] } : null;
    }
    if (ref.kind === "port") {
      const port = model.portByKey.get(ref.id);
      if (!port) return null;
      const bay = model.bayById.get(port.bayId);
      card = { title: `${bay?.label || port.bayId} path via ${port.controller || `a ${model.sideWord} not reported`}${port.device ? ` (${context.deviceShort(port.device)})` : ""}`, rows: portRows(model, port) };
    } else if (ref.kind === "bay") {
      const bay = model.bayById.get(ref.id);
      if (!bay) return null;
      const rows = bay.ports.map((port) => ({
        // The device is named where it tells two paths of one source apart, or is short.
        label: `via ${port.controller || "?"}${port.device && (context.deviceShort(port.device).length <= 12 || bay.ports.filter((other) => other.controllerId === port.controllerId && other.controller === port.controller).length > 1) ? ` (${context.deviceShort(port.device)})` : ""}`,
        value: `${{ active: "active head · ", standby: "standby head · " }[(model.roleOf?.get(port.controllerId) || model.roleOf?.get(port.controller))?.role] || ""}${portBrief(port)}`,
        warn: ["fault", "missing"].includes(context.pathTone(port.state, port.basis)) ? "critical" : context.phyTier(port.phy),
      }));
      if (!rows.length) rows.push({ label: "Paths", value: bay.occupancy === "yes" ? "none reported for this disk" : bay.occupancy === "no" ? "none: no disk" : context.NOT_READ });
      card = { title: `${bay.label}${bay.deviceName ? ` · ${model.shared ? context.deviceShort(bay.deviceName) : bay.deviceName}` : ` · ${{ yes: "disk present", no: "empty", unknown: "occupancy not read" }[bay.occupancy]}`}${bay.protocol === "SATA" ? " · SATA: one port" : bay.protocol ? ` · ${bay.protocol}` : ""}`, rows };
    } else if (ref.kind === "link") {
      const link = model.linkById.get(ref.id);
      if (!link) return null;
      if (link.bus) card = { title: `${context.nodeName(model, link.source)} → its bays`, rows: busRows(model, link) };
      else if (context.LANE_LINKS.has(link.kind)) card = { title: cableName(model, link), rows: cableRows(model, link) };
      else if (link.stubCables) {
        card = {
          title: `${context.nodeName(model, link.source)} → ${context.plural(link.stubCables.length, "enclosure")}`,
          rows: link.stubCables.map((cable) => ({ label: `to ${cable.targetName}`, value: cable.lanes ? `x${cable.lanes.count} · ${context.laneRate(cable.lanes)} on PHY ${context.phyRanges(cable.lanes.sourcePhys) || "?"}` : "lanes not reported" })),
        };
      } else card = { title: context.linkName(model, link), rows: link.bundle ? [{ label: "Bays", value: link.labelLines.join(" · ") || String(link.count) }] : [] };
    } else if (ref.kind === "node") {
      const entry = model.nodeById.get(ref.id);
      if (!entry) return null;
      if (entry.kind === "controller" && model.core) card = { title: entry.label, rows: hbaRows(model, entry) };
      else if (entry.head) card = { title: entry.label, rows: headRows(model, entry) };
      else if (entry.stubGroup) {
        card = {
          title: entry.label,
          rows: entry.stubGroup.map((member) => ({ label: member.neighbour.name, value: context.stubFacts(member, member.neighbour.health).filter((line) => !/^open /.test(line)).join(" · "), warn: member.neighbour.tier || false })),
        };
      } else if (entry.kind === "expander") card = { title: entry.label, rows: expanderRows(model, entry) };
      else card = { title: entry.label, rows: entry.facts.map((value) => ({ label: "", value })) };
    } else {
      return null;
    }
    const flags = flagsOf(model, ref.kind, ref.id);
    card.flags = flags;
    card.rows.push(...flagRows(model, ref.kind, ref.id).map((row) => ({ ...row, flag: true })));
    return card;
  }

function cardText(card) {
    return card ? [card.title, ...card.rows.map((row) => (row.label ? `${row.label}: ${row.value}` : row.value))].join(", ") : "";
  }

function cardHtml(card) {
    return `<strong class="topo-tip-title">${context.escapeHtml(card.title)}</strong>`
      + card.rows.map((row) => `<span class="topo-tip-label${tone(row.warn)}">${context.escapeHtml(row.label)}</span><span class="topo-tip-value${tone(row.warn)}">${context.escapeHtml(row.value)}</span>`).join("");
  }

function tone(warn) {
    return warn === "warning" ? " is-caution" : warn ? " is-warn" : "";
  }

function kvRow(label, value, warn = false) {
    return `<div class="kv-row${warn === "warning" ? " topo-caution" : warn ? " topo-warn" : ""}"><span class="kv-label">${context.escapeHtml(label)}</span><span class="kv-value">${context.escapeHtml(value)}</span></div>`;
  }

function phyLine(row) {
    const disk = /^(SAS|SATA) Target$|^End device \(disk/.test(row.device);
    const range = disk ? " (drive maximum not read)" : row.min !== null && row.max !== null ? ` (PHY ${row.min}–${row.max})` : "";
    return `${context.rateText(row.rate)}${range} · ${!row.measured ? `counters ${context.NOT_READ}` : row.errors ? context.counterText(row) : "no errors"}`;
  }

function detailFor(model, ref) {
    const card = cardFor(model, ref);
    if (!card) return null;
    const rows = card.rows.filter((row) => !row.flag).map((row) => kvRow(row.label || "Fact", row.value, row.warn));
    for (const item of card.flags) rows.push(kvRow(context.TIER_WORDS[item.tier], item.text, item.tier));
    let title = "";
    if (ref.kind === "port") {
      const port = model.portByKey.get(ref.id);
      const chip = model.nodeById.get(port.expanderId);
      title = "Selected disk path";
      rows.unshift(kvRow("Path", card.title));
      rows.push(kvRow("HBA device", port.device || context.NOT_READ), kvRow("Device handle", port.handle || context.NOT_READ),
        kvRow("Expander", chip ? `${chip.label}${chip.address ? ` (${chip.address})` : ""}` : context.NOT_READ),
        kvRow("Counter state", port.phy ? port.phy.counterState : context.NOT_READ));
    } else if (ref.kind === "bay") {
      // Every port of the bay in full; the card above them is the short form.
      const bay = model.bayById.get(ref.id);
      title = "SAS ports";
      rows.length = 0;
      for (const port of bay.ports) {
        const name = `via ${port.controller || "an HBA not reported"}`;
        for (const row of portRows(model, port)) rows.push(kvRow(`${name} · ${row.label}`, row.value, row.warn));
        rows.push(kvRow(`${name} · HBA device`, [port.device, port.handle ? `handle ${port.handle}` : ""].filter(Boolean).join(" · ") || context.NOT_READ));
      }
      if (!bay.ports.length) {
        if (bay.occupancy !== "yes") return null;
        rows.push(kvRow("Ports", `${context.NOT_READ}: the payload has no path or HBA device row for this disk`));
      }
      for (const item of card.flags) rows.push(kvRow(context.TIER_WORDS[item.tier], item.text, item.tier));
    } else if (ref.kind === "link") {
      const link = model.linkById.get(ref.id);
      if (link.bus) {
        title = "Selected expander bus";
        rows.unshift(kvRow("Bus", card.title));
        for (const { bay, port } of busPorts(model, link)) {
          rows.push(kvRow(`${bay.label}${port.phy ? ` · PHY ${port.phy.phy}` : ""}`, `${port.state} · ${port.phy ? phyLine(port.phy) : `PHY ${context.NOT_READ}`}`, ["fault", "missing"].includes(context.pathTone(port.state, port.basis)) ? "critical" : context.phyTier(port.phy)));
        }
      } else if (context.LANE_LINKS.has(link.kind) && link.lanes) {
        title = "Selected cable";
        rows.unshift(kvRow("Cable", card.title));
        const target = model.sas.expanders.get(link.target);
        const below = unpairedBelow(model, link);
        for (const item of unpairedPhys(model, link)) {
          const { end, row } = item;
          rows.push(kvRow(`${end} PHY ${row ? row.phy : "?"}`, row ? `${context.rateText(row.rate)}${row.max !== null ? ` (max ${row.max})` : ""} · ${!row.measured ? `counters ${context.NOT_READ}` : row.errors ? context.counterText(row) : "no errors"}` : context.NOT_READ,
            below(item) ? "critical" : context.phyTier(row)));
        }
        for (const lane of link.lanes.rows) {
          const cap = laneCap(model, link, lane);
          const far = target?.byPhy.get(lane.target);
          rows.push(kvRow(`Lane PHY ${lane.source} ↔ ${lane.target}`, `${context.rateText(lane.rate)}${cap !== null ? ` (both ends ${cap})` : " (a maximum not read)"} · target PHY: ${far ? (!far.measured ? `counters ${context.NOT_READ}` : far.errors ? context.counterText(far) : "no errors") : context.NOT_READ}`,
            lane.rate !== null && cap !== null && lane.rate < cap ? "critical" : context.phyTier(far)));
        }
      } else {
        return null;
      }
    } else if (ref.kind === "node") {
      const entry = model.nodeById.get(ref.id);
      if (entry.stubGroup) {
        title = "Enclosures next in the chain";
        // Each with the link a single stub would be.
        for (const member of entry.stubGroup) {
          if (member.neighbour.href) rows.push(`<div class="kv-row"><span class="kv-label">${context.escapeHtml(member.neighbour.name)}</span><span class="kv-value"><a href="${context.escapeHtml(member.neighbour.href)}" data-fabric-enclosure="${context.escapeHtml(member.neighbour.enclosureId)}">open its Topology view</a></span></div>`);
        }
      } else if (entry.head) {
        title = "Head end";
        if (entry.peer) rows.unshift(kvRow("Head end", `${card.title} (open its own view to give it a friendly label)`));
      } else if (entry.reported) {
        title = entry.fromPath ? "Expander named in a device path" : "Expander reported by its disks";
        rows.unshift(kvRow("Expander", card.title));
        for (const { bay, port } of busPorts(model, model.linkById.get(entry.busId))) {
          rows.push(kvRow(bay.label, [port.controller, port.fromPath ? `PHY ${port.pathPhy} (from the device path)` : "", port.sasAddress ? `port ${port.sasAddress}` : "", port.protocol, port.diskPhy ? `disk PHY ${port.diskPhy}` : "", port.hctl ? `SCSI ${port.hctl}` : "", port.state].filter(Boolean).join(" · ")));
        }
      } else if (entry.kind === "expander" && model.sas.expanders.has(entry.id)) {
        title = "Expander SAS detail";
        for (const row of model.sas.expanders.get(entry.id).phys) {
          const linked = row.rate !== null || row.handle;
          rows.push(kvRow(`PHY ${row.phy}`, linked ? `${phyPeer(model, entry.id, row)} · ${phyLine(row)}` : `${row.device || "no device"}${row.errors ? ` · ${context.counterText(row)}` : ""}`, context.phyTier(row)));
        }
      } else if (entry.kind === "controller" && model.sas.controllers.has(entry.id)) {
        title = "HBA SAS detail";
        const { drawn } = hbaLanes(model, entry);
        for (const row of model.sas.controllers.get(entry.id).phys) {
          const where = drawn.has(row.phy) ? `to ${context.nodeName(model, drawn.get(row.phy).target)}` : "outside this view";
          rows.push(kvRow(`PHY ${row.phy}`, row.rate === null ? `${row.device || "no device"}${row.disabled ? " · disabled" : ""}`
            : `${where}${row.handle ? ` · handle ${row.handle}` : ""} · ${row.rate} Gb/s${row.min !== null && row.max !== null ? ` (PHY ${row.min}–${row.max})` : ""}`,
          drawn.has(row.phy) && slowLanes(model, drawn.get(row.phy)).some((lane) => lane.source === row.phy)));
        }
      } else {
        return null;
      }
    } else {
      return null;
    }
    return { title, html: `<div class="kv-grid">${rows.join("")}</div>` };
  }

function enclosureDetail(model, nodeId) {
    const entry = model?.nodeById.get(nodeId);
    const group = entry?.physical ? model.groupById.get(entry.groupId) : null;
    if (!group) return null;
    const enclosure = entry.physical;
    const row = (label, value) => `<div class="kv-row"><span class="kv-label">${context.escapeHtml(label)}</span><span class="kv-value">${context.escapeHtml(value)}</span></div>`;
    const rows = [];
    if (enclosure.name) rows.push(row("Name", enclosure.name));
    if (enclosure.sharedBy) rows.push(row("Shared by", `${enclosure.sharedBy.join(" and ")}: ${model.shared.basis}`));
    else rows.push(row("Logical id", enclosure.identity));
    rows.push(row("Bays", `${group.bays.length} (${context.bayRange(group)}): ${group.summary.withDisk} with a disk, ${group.summary.empty} empty${group.summary.unknown ? `, ${group.summary.unknown} not read` : ""}`));
    for (const item of group.paths) {
      const through = item.sourceIds.map((id) => model.nodeById.get(id)).filter(Boolean).map((source) => kindLabel(model, source));
      rows.push(row(`Path ${item.label}`, `${through.length ? `through ${through.join(" and ")}` : "parent link not reported"}${item.handle ? `; enclosure handle ${item.handle}` : ""}; reports a disk in ${context.plural(item.bayIds.size, "bay")}`));
    }
    const basis = { reported: "the inventory names its enclosure", bays: "every bay it reports is in this enclosure", coverage: "inferred from the bays it covers, not reported" };
    for (const ses of enclosure.ses) rows.push(row(`SES ${ses.device}`, `${context.plural(ses.bays, "bay")}; ${basis[ses.basis]}`));
    const states = context.pathSummary(group.summary);
    if (states) rows.push(row("Path states", states));
    for (const item of flagsOf(model, "node", nodeId)) rows.push(kvRow(context.TIER_WORDS[item.tier], item.text, item.tier));
    if (group.summary.noPath) rows.push(row("No path reported", `${context.plural(group.summary.noPath, "disk")}: ${group.bays.filter((bay) => bay.occupancy === "yes" && !bay.pathStates.length).map((bay) => bay.label).join(", ")}`));
    return { title: "Selected enclosure", html: `<div class="kv-grid">${rows.join("")}</div>` };
  }

function kindLabel(model, entry) {
    const chip = context.chipOfNode(model, entry.id) || entry;
    if (chip.chipOf) return `${chip.label}${chip.address ? ` (${chip.address})` : ""}`;
    return entry.label.toLowerCase().includes(entry.kind) ? entry.label : `${entry.kind} ${entry.label}`;
  }

function ownPin(model, ref) {
    if (!ref) return false;
    const entry = ref.kind === "node" ? model?.nodeById.get(ref.id) : null;
    return ref.kind === "link" || ref.kind === "port" || Boolean(entry?.physical || entry?.reported || entry?.peer || entry?.stubGroup);
  }

const live = {
    model: null, pin: null, hover: null, query: "", scope: null, fit: false, scroll: { left: 0, top: 0 },
    container: null, input: null, status: null, hooks: {}, pointer: null, hoverElement: null, tipBox: null, down: null, refocusPin: null,
    // Why the next mount chooses the scroll position: "open",
    // { expand: group id, control: where its expand control was on the page
    // when it was in view, else null (keep) } or null (keep).
    firstView: null, expandedKeys: null, refocusFit: false, quiet: null,
    // scroll, actualScroll and restoreScroll are in drawing coordinates: the
    // offsets held before Fit, and those to restore on Actual size. room: the
    // space mount added before the drawing's left and top edges, so a view
    // kept in place there stays reachable. The inner width last measured
    // after a mount.
    actualScroll: null, restoreScroll: null, measuredWidths: new Set(), room: { left: 0, top: 0 },
    // The drawing's root that the last mount put in place, until release.
    mounted: null,
    // The sentences the last render's band can show: { sentences, key }.
    trace: null,
  };

const DEFAULT_HINT = "Hover or focus anything to trace its path; click or press Enter to pin it; click it again, click the background or press Esc to clear.";

function scrollBox(container) {
    const box = container?.querySelector?.(".fabric-topology-scroll");
    return box && typeof box === "object" ? box : null;
  }

function refExists(model, ref) {
    if (!model || !ref) return false;
    if (ref.kind === "link") return model.linkById.has(ref.id);
    if (ref.kind === "bay") return model.bayById.has(ref.id);
    if (ref.kind === "port") return model.portByKey.has(ref.id);
    if (ref.kind === "group") return model.groupById.has(ref.id);
    return model.nodeById.has(ref.id);
  }

function sameRef(left, right) {
    return Boolean(left && right && left.kind === right.kind && left.id === right.id);
  }

function refFromSelection(model, selection) {
    if (!model || !selection) return null;
    if (model.bayById.has(selection.id)) return { kind: "bay", id: selection.id };
    const bay = selection.kind === "trace" ? model.bays.find((item) => item.traceId === selection.id) : null;
    if (bay) return { kind: "bay", id: bay.bayId };
    // A controller's or SES device's row for an enclosure is that enclosure here.
    const merged = model.nodes.find((entry) => entry.physical && entry.memberIds.includes(selection.id));
    if (merged) return { kind: "node", id: merged.id };
    const chip = model.nodeById.get(selection.id)?.mergedInto;
    if (chip) return { kind: "node", id: chip };
    return model.nodeById.has(selection.id) ? { kind: "node", id: selection.id } : null;
  }

function clearInput() {
    live.query = "";
    if (live.input) live.input.value = "";
    if (live.status) Object.assign(live.status, { textContent: "", title: "" });
  }

function expandedKeys(options) {
    const expanded = context.isRecord(options.expanded) ? options.expanded : {};
    return new Set(Object.keys(expanded).filter((key) => key.startsWith("topology:") && expanded[key]));
  }

function boxRect(box) {
    const rect = typeof box?.getBoundingClientRect === "function" ? box.getBoundingClientRect() : null;
    return rect && Number.isFinite(rect.left) && Number.isFinite(rect.top) ? { left: rect.left, top: rect.top } : { left: 0, top: 0 };
  }

// A port's key is its place in its bay; the path is what the payload says of
// it: its bay, controller id, member device, SAS addresses, handle, expander
// and PHY. A pinned port follows that path when rows move only where exactly
// one port had it before and exactly one has it now; otherwise equivalence
// is not proved and the pin is cleared. Position never breaks a tie.
function portIdentity(port) {
    return JSON.stringify([port.bayId, port.controllerId || "", port.controllerId ? "" : port.controller, port.device, port.sasAddress, port.attached, port.handle, port.expanderId, port.phy?.phy ?? null, port.diskPhy, port.hctl, port.sg]);
  }

function followPort(previous, model, pin) {
    const before = previous.portByKey.get(pin.id);
    if (!before) return pin;
    const identity = portIdentity(before);
    if ([...previous.portByKey.values()].filter((port) => portIdentity(port) === identity).length !== 1) return null;
    const found = [...model.portByKey.values()].filter((port) => portIdentity(port) === identity);
    return found.length === 1 ? { ...pin, id: found[0].key } : null;
  }

function render(fabric, options = {}) {
    listen();
    const previous = live.model;
    const sameScope = live.scope !== null && previous?.scope === context.scopeOf(fabric);
    const previousOrder = sameScope ? Object.fromEntries(previous.tiers.map((tier) => [tier.id, tier.order || []])) : null;
    // The layout takes the scroll box's inner width: the box drawn last time
    // (scroll bar and borders already taken off), or before there is one,
    // the page's width for it less the box's two borders.
    const box = scrollBox(live.container);
    const measured = Number(box?.clientWidth) || 0;
    const width = measured > 0 ? measured : Math.max(0, (Number(options.width) || 0) - 2 * context.SCROLL_BORDER);
    // Fit takes the box's inner height (its limit less both borders, floored): nothing left to scroll.
    const height = Math.max(0, Math.floor((Number(options.height) || 0) - 2 * context.SCROLL_BORDER));
    const model = context.buildTopologyModel(fabric, { ...options, width, height, previousOrder });
    // The page replaces the map on every render: keep the drawing's scroll
    // position for the same scope, and start a new scope at the origin.
    const keys = expandedKeys(options);
    // the other head arrives after the first) is a new drawing: a new view.
    const reshaped = live.scope !== null && (Boolean(previous?.shared) !== Boolean(model.shared) || Boolean(previous?.transport) !== Boolean(model.transport));
    if (live.scope === null || model.scope !== live.scope || reshaped) {
      if (live.scope !== null) {
        live.pin = null;
        clearInput();
      }
      live.scroll = { left: 0, top: 0 };
      live.firstView = "open";
    } else {
      // With the box's settled height, which a redraw that keeps the view
      // keeps too.
      if (box) live.scroll = { left: (Number(box.scrollLeft) || 0) - live.room.left, top: (Number(box.scrollTop) || 0) - live.room.top, height: Number(box.clientHeight) || 0 };
      // Back from Fit: the offsets the drawing had before it was fitted.
      if (live.restoreScroll) live.scroll = live.restoreScroll;
      // A shelf expanded or collapsed: keep its control where the operator
      // saw it on the screen when it was in view, else keep the shelf in view.
      const toggled = [...model.groups].find((group) => keys.has(group.expandKey) !== Boolean(live.expandedKeys?.has(group.expandKey)));
      const before = toggled && previous?.groupById.get(toggled.id);
      const clientHeight = Number(box?.clientHeight) || 0;
      if (toggled && before && live.firstView === null) {
        const control = context.toggleBox(before);
        const visible = Boolean(box) && measured > 0 && clientHeight > 0
          && control.x >= live.scroll.left && control.x + control.w <= live.scroll.left + measured
          && control.y >= live.scroll.top && control.y + control.h <= live.scroll.top + clientHeight;
        const rect = boxRect(box);
        live.firstView = {
          expand: toggled.id,
          control: visible ? { x: rect.left + control.x - live.scroll.left, y: rect.top + control.y - live.scroll.top } : null,
        };
      }
    }
    live.restoreScroll = null;
    live.expandedKeys = keys;
    live.scope = model.scope;
    live.model = model;
    const carried = live.pin;
    if (options.selection !== undefined) {
      const selected = refFromSelection(model, options.selection);
      if (selected) live.pin = selected;
      else if (!ownPin(model, live.pin)) live.pin = null;
    }
    if (previous && live.pin === carried && live.pin?.kind === "port") live.pin = followPort(previous, model, live.pin);
    if (!refExists(model, live.pin)) live.pin = null;
    live.hover = null;
    // The sentences this drawing's band can show, for its one measure.
    const sentences = context.traceSentences(model, { pin: live.pin });
    live.trace = { sentences, key: JSON.stringify(sentences) };
    updateSearchStatus();
    const help = context.searchHelp(model);
    if (live.input) {
      live.input.placeholder = help.placeholder;
      const label = live.input.labels?.[0];
      if (label) label.textContent = help.label;
    }
    return context.renderTopology(model, { pin: live.pin, query: live.query, fit: live.fit });
  }

function firstViewLeft(model, width) {
    const top = model.nodes.filter((entry) => entry.kind === "host" || entry.kind === "controller");
    const span = top.length ? [Math.min(...top.map((entry) => entry.x)), Math.max(...top.map((entry) => entry.x + entry.w))] : null;
    const fits = span && span[1] - span[0] <= width;
    let range = [0, Math.max(0, model.width - width)];
    if (fits) range = [Math.max(range[0], span[1] - width), Math.min(range[1], span[0])];
    const wanted = span ? (span[0] + span[1]) / 2 - width / 2 : 0;
    return Math.round(Math.min(range[1], Math.max(range[0], wanted)));
  }

function sizeRoom(box, left, top, boxHeight = null) {
    const svg = box.querySelector?.(".fabric-topology-svg");
    const model = live.model;
    if (!svg || typeof svg.setAttribute !== "function" || !model || live.fit) return { left: 0, top: 0 };
    const room = { left: Math.max(0, Math.ceil(-left)), top: Math.max(0, Math.ceil(-top)) };
    const right = Math.max(Math.ceil(model.width), Math.ceil(left + (Number(box.clientWidth) || 0)));
    const bottom = Math.max(Math.ceil(model.height), Math.ceil(top + (boxHeight ?? (Number(box.clientHeight) || 0))));
    const width = right + room.left;
    const height = bottom + room.top;
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", `${-room.left} ${-room.top} ${width} ${height}`);
    return room;
  }

function keepView(box, left, top, kept, page) {
    sizeRoom(box, left, top, kept);
    const settled = Number(box.clientHeight) || 0;
    if (!page) return settled;
    if (typeof window.scrollTo === "function" && (window.scrollX !== page.x || window.scrollY !== page.y)) {
      window.scrollTo(page.x, page.y);
    }
    if (top > 0) return settled;
    // At its top nothing holds the page back; else what its scroll range
    // has beyond its scroll position, nothing where the page cannot say.
    const y = Number(window.scrollY) || 0;
    if (y <= 0) return 0;
    const root = typeof document !== "undefined" ? document.documentElement : null;
    const slackOf = () => {
      const range = Number(root?.scrollHeight) - (Number(root?.clientHeight) || Number(window.innerHeight) || 0);
      return Number.isFinite(range) ? Math.max(0, Math.floor(range - y)) : 0;
    };
    const slack = slackOf();
    // No slack: nothing to give back, so the box's own height is not read.
    if (!slack) return Math.max(0, settled);
    // A box at a limit that is not a whole pixel gives back the fraction less than the slack (its
    // clientHeight is rounded up), and below the limit it is whole: give back the rest in this redraw.
    const tall = Number(typeof box.getBoundingClientRect === "function" ? box.getBoundingClientRect()?.height : NaN);
    if (!Number.isFinite(tall) || tall === Math.round(tall)) return Math.max(0, settled - slack);
    sizeRoom(box, left, top, settled - slack);
    return Math.max(0, (Number(box.clientHeight) || 0) - slackOf());
  }

function mount(container, pageScroll = null) {
    const box = scrollBox(container);
    if (box) {
      const reason = live.firstView;
      live.firstView = null;
      const width = Number(box.clientWidth) || live.model?.box?.width || 0;
      const page = pageScroll && Number.isFinite(pageScroll.x) && Number.isFinite(pageScroll.y) ? pageScroll : null;
      // Offsets in drawing coordinates: negative ones lie before its edge.
      let left = live.scroll.left;
      let top = live.scroll.top;
      // The box height the room is sized for at the end; null: as it is then.
      let height = null;
      const group = reason?.expand ? live.model?.groupById.get(reason.expand) : null;
      // The room is sized exactly for the view at the end, also when putting
      // the page back throws, so no room sized on the way outlives an error.
      try {
        if (reason?.control && group && !live.fit) {
          // The shelf's expand control where it was on the screen: both
          // offsets, and the box's own move on the page, taken into account.
          // The room changes the drawing's height, so the box's own height
          // (up to its limit) and the page around it can still change: aim
          // again from where the box settled, and size the room for that.
          const control = context.toggleBox(group);
          const aim = () => {
            const rect = boxRect(box);
            left = rect.left + control.x - reason.control.x;
            top = rect.top + control.y - reason.control.y;
          };
          aim();
          if (page) {
            // The box is never taller than the window, so room of one window
            // height after the aimed view lets it reach its height limit. While
            // that room is there the page is as long as it gets, and its own
            // scroll position, lost while the shorter drawing was in place,
            // can be put back before the control is aimed at for good.
            const roomToHeightLimit = Number(window.innerHeight) || 0;
            sizeRoom(box, left, top + roomToHeightLimit);
            if (typeof window.scrollTo === "function" && (window.scrollX !== page.x || window.scrollY !== page.y)) {
              window.scrollTo(page.x, page.y);
            }
          } else {
            sizeRoom(box, left, top);
          }
          aim();
        } else {
          if (reason === "open" && live.model && !live.fit && width) {
            left = firstViewLeft(live.model, width);
            top = 0;
          }
          // A redraw that keeps the view (a pin, a selection, a search, a
          // refresh, a shelf whose control was out of view) moves neither the
          // page nor the drawing. The new drawing is in place at its own size,
          // so the box is for now only as tall as that drawing: where the kept
          // view, with the box as tall as it was, reaches past the drawing's
          // bottom edge, the room is first sized for that box. Any other
          // redraw sizes the room once and reads nothing of the page.
          const kept = reason === "open" ? 0 : Number(live.scroll.height) || 0;
          if (kept > 0 && live.model && !live.fit && Math.ceil(top + kept) > Math.ceil(live.model.height)) {
            height = keepView(box, left, top, kept, page);
          }
        }
      } finally {
        live.room = sizeRoom(box, left, top, height);
        left += live.room.left;
        top += live.room.top;
        if (box.scrollLeft !== left) box.scrollLeft = left;
        if (box.scrollTop !== top) box.scrollTop = top;
      }
      // The box's inner width differs from the width the drawing was laid
      // out for (a scroll bar came or went): lay it out once more.
      // Once per inner width while Topology is shown, so a scroll bar that comes and goes cannot loop.
      const inner = Number(box.clientWidth) || 0;
      const root = container.querySelector?.(".fabric-topology") || null;
      live.mounted = root;
      if (inner > 0 && root && live.model && Math.abs(inner - (live.model.box?.width || 0)) >= 1 && !live.measuredWidths.has(inner)
        && typeof window.setTimeout === "function") {
        live.measuredWidths.add(inner);
        cancelWidthTimer();
        globals.widthTimer = window.setTimeout(() => {
          globals.widthTimer = null;
          // Only for the root still in place: a later redraw measured its own.
          if (live.mounted === root && live.container?.querySelector?.(".fabric-topology") === root) live.hooks.requestRender?.();
        }, 0);
      }
    }
    if (live.refocusFit) {
      live.refocusFit = false;
      container?.querySelector?.("[data-topology-fit]")?.focus?.({ preventScroll: true });
    }
    if (live.refocusPin) {
      // After Clear selection: focus on what was pinned, else on Fit.
      const key = TIP_KEYS[live.refocusPin.kind];
      const id = live.refocusPin.id;
      live.refocusPin = null;
      const item = key ? [...(container?.querySelectorAll?.(`[${key}]`) || [])].find((element) => element.getAttribute(key) === id) : null;
      (item || container?.querySelector?.("[data-topology-fit]"))?.focus?.({ preventScroll: true });
    }
  }

const bandWatch = { observer: null, band: null, measurer: null };

function onBandResize(entries) {
    for (const entry of entries) {
      // The empty measurer is 1em by 1lh (stylesheet): a new font resizes it, also when the band keeps its size.
      if (entry?.target && entry.target === bandWatch.measurer) { holdBand(bandWatch.band); continue; }
      const height = Number(entry?.borderBoxSize?.[0]?.blockSize);
      const root = entry?.target?.closest?.(".fabric-topology");
      if (root?.style && Number.isFinite(height) && height > 0) root.style.setProperty("--topology-trace-height", `${Math.ceil(height)}px`);
      // A new width, or a font that changed without one, is measured here;
      // the band's own height changing is not.
      if (entry?.target !== bandWatch.band) continue;
      const inline = Number(entry?.borderBoxSize?.[0]?.inlineSize);
      holdBand(entry.target, Number.isFinite(inline) && inline > 0 ? inline : hold.width);
    }
  }

const hold = { key: null, width: null, gen: 0, cache: new Map(), band: null, px: null, resolution: null };

const HOLD_WIDTHS = 8;

function holdKey(band, width) {
    const para = band.querySelector?.("[data-topology-pin]");
    const style = para && typeof window.getComputedStyle === "function" ? window.getComputedStyle(para) : null;
    return `${width}|${style?.font || ""}|${style?.lineHeight || ""}|${Number(window.devicePixelRatio) || 1}|${hold.gen}`;
  }

function holdBand(band, width = 0) {
    const measurer = band?.querySelector?.("[data-topology-measure]");
    if (!measurer || !band.style || !live.trace) return;
    if (hold.key !== live.trace.key) hold.cache.clear();
    hold.key = live.trace.key;
    if (!(width > 0)) {
      // A new band first takes the page's height, so the read lays the page
      // out as long as it was; a wrong one is replaced below, before mount.
      if (hold.band !== band && hold.px !== null) putHold(band, hold.px);
      width = Number(band.getBoundingClientRect?.()?.width) || 0;
    }
    hold.width = width > 0 ? width : null;
    if (!hold.width) return;
    const key = holdKey(band, width);
    let px = hold.cache.get(key);
    if (px === undefined) {
      px = measureBand(measurer, live.trace.sentences);
      if (px === null) return;
      hold.cache.set(key, px);
      if (hold.cache.size > HOLD_WIDTHS) hold.cache.delete(hold.cache.keys().next().value);
    }
    putHold(band, px);
  }

function putHold(band, px) {
    if (hold.band === band && hold.px === px) return;
    Object.assign(hold, { band, px });
    band.style.setProperty("--topology-trace-held", `${px}px`);
  }

function measureBand(measurer, sentences) {
    const doc = measurer.ownerDocument || (typeof document !== "undefined" ? document : null);
    if (typeof doc?.createElement !== "function") return null;
    const items = typeof doc.createDocumentFragment === "function" ? doc.createDocumentFragment() : null;
    for (const text of sentences) {
      const line = doc.createElement("p");
      line.className = "fabric-topology-pin subtle";
      line.textContent = text;
      if (items) items.appendChild(line);
      else measurer.appendChild(line);
    }
    if (items) measurer.appendChild(items);
    const height = Number(measurer.getBoundingClientRect?.()?.height);
    measurer.textContent = "";
    return Number.isFinite(height) && height > 0 ? Math.ceil(height) : null;
  }

function remeasure() {
    hold.gen += 1;
    const band = live.container?.querySelector?.("[data-topology-trace]");
    if (band) holdBand(band);
  }

function onResolution() {
    watchResolution();
    remeasure();
  }

function watchResolution() {
    hold.resolution?.removeEventListener?.("change", onResolution);
    hold.resolution = null;
    if (typeof window.matchMedia !== "function") return;
    hold.resolution = window.matchMedia(`(resolution: ${Number(window.devicePixelRatio) || 1}dppx)`);
    hold.resolution?.addEventListener?.("change", onResolution);
  }

function watchBand(container) {
    if (typeof window.ResizeObserver !== "function") return;
    const band = container?.querySelector?.("[data-topology-trace]") || null;
    if (band === bandWatch.band) return;
    unwatchBand();
    if (!band) return;
    if (!bandWatch.observer) bandWatch.observer = new window.ResizeObserver(onBandResize);
    bandWatch.observer.observe(band, { box: "border-box" });
    bandWatch.band = band;
    bandWatch.measurer = band.querySelector?.("[data-topology-measure]") || null;
    if (bandWatch.measurer) bandWatch.observer.observe(bandWatch.measurer);
  }

function unwatchBand() {
    bandWatch.observer?.disconnect();
    Object.assign(bandWatch, { band: null, measurer: null });
  }

function release() {
    unwatchBand();
    Object.assign(hold, { key: null, width: null, band: null, px: null });
    hold.cache.clear();
    live.pin = null;
    live.hover = null;
    live.quiet = null;
    live.scope = null;
    live.firstView = null;
    live.scroll = { left: 0, top: 0 };
    live.actualScroll = null;
    live.restoreScroll = null;
    live.measuredWidths = new Set();
    live.mounted = null;
    live.room = { left: 0, top: 0 };
    cancelWidthTimer();
    clearInput();
    unlisten();
  }

function view() {
    return { pin: live.pin, hover: live.hover, query: live.query };
  }

function drawnModel() {
    return live.model;
  }

function inspectorDetail() {
    if (!live.model || !live.pin) return null;
    const detail = detailFor(live.model, live.pin);
    if (detail) {
      if ((live.pin.kind === "node" || live.pin.kind === "bay") && !ownPin(live.model, live.pin)) return { ...detail, withPage: true };
      const ids = live.pin.kind === "port" ? [live.model.portByKey.get(live.pin.id)?.bayId]
        : live.pin.kind === "link" ? [live.model.linkById.get(live.pin.id)?.source, live.model.linkById.get(live.pin.id)?.target]
        : live.model.nodeById.get(live.pin.id)?.stubGroup?.map((entry) => entry.id) || [live.pin.id];
      const aliases = [...new Set(ids)].map((id) => live.model.nodeById.get(id) || live.model.bayById.get(id))
        .filter(Boolean).map((entry) => ({ label: entry.label, objectId: entry.id || entry.bayId,
          objectKind: entry.kind || "bay" }));
      return { ...detail, aliases };
    }
    if (live.pin.kind === "link") {
      // A bundle's or a link's own details: its two ends are its label
      // targets, and an enclosure it leads into is one by its own target.
      const found = context.linkDetail(live.model, live.pin.id);
      const link = live.model.linkById.get(live.pin.id);
      if (!found || !link) return found;
      const ends = [link.source, link.target].map((id) => live.model.nodeById.get(id)).filter(Boolean)
        .map((entry) => ({ label: entry.label, objectId: entry.id, objectKind: entry.kind }));
      const enclosure = live.model.groupById.get(link.target)?.enclosure?.aliasTarget;
      return { ...found, aliases: [...ends, ...(enclosure ? [{ label: "Enclosure", ...enclosure }] : [])] };
    }
    if (!ownPin(live.model, live.pin)) return null;
    const enclosure = live.model.nodeById.get(live.pin.id)?.physical;
    const found = enclosureDetail(live.model, live.pin.id);
    return found && enclosure ? { ...found, aliases: [{ label: "Enclosure", ...enclosure.aliasTarget }] } : found;
  }

function composeInspector(detail, { nodes, renderAliasRow, renderPage, title, body }) {
    if (!detail.withPage) {
      const aliases = context.list(detail.aliases).filter((target) => nodes.has(target.objectId)).map((target) => renderAliasRow(target.label, {
        objectId: target.objectId, objectKind: target.objectKind, item: nodes.get(target.objectId),
      })).join("");
      title.textContent = detail.title;
      body.innerHTML = `<section class="fabric-inspector-section"><h4>Friendly Label</h4><div class="kv-grid">${aliases || "No observed label target was read for this item."}</div></section>` + detail.html;
      return;
    }
    renderPage();
    const sas = `<section class="fabric-inspector-section fabric-topology-sas"><h4>${context.escapeHtml(detail.title)}</h4>${detail.html}</section>`;
    if (typeof body.querySelector !== "function" || typeof body.insertAdjacentHTML !== "function") {
      body.innerHTML += sas;
      return;
    }
    const aliasRow = body.querySelector(".fabric-alias-row");
    const aliasSection = aliasRow?.closest("section");
    const hero = body.querySelector(".fabric-selected-bay-hero");
    if (aliasSection && hero) {
      hero.after(aliasSection);
      aliasSection.insertAdjacentHTML("afterend", sas);
    } else if (aliasRow?.closest(".kv-grid")) {
      aliasRow.closest(".kv-grid").insertAdjacentHTML("afterend", sas);
    } else body.insertAdjacentHTML("beforeend", sas);
  }

function boardLabel(fabric, id) {
    const model = live.model;
    if (!model?.physical || model.scope !== context.scopeOf(fabric)) return "";
    const bay = model.bayById.get(id) || model.bays.find((item) => item.traceId === id);
    return model.groupByBay.get(bay?.bayId)?.enclosure?.node.label || "";
  }

function refFromElement(element) {
    const target = element?.closest?.("[data-topo-link], [data-topo-port], [data-topo-bay], [data-topo-node], [data-topo-group], [data-topo-tier]");
    if (!target || !target.closest(".fabric-topology")) return null;
    if (target.dataset.topoTier) return { kind: "tier", id: target.dataset.topoTier };
    if (target.dataset.topoPort) return { kind: "port", id: target.dataset.topoPort };
    if (target.dataset.topoLink) return { kind: "link", id: target.dataset.topoLink };
    if (target.dataset.topoBay) return { kind: "bay", id: target.dataset.topoBay };
    if (target.dataset.topoNode) return { kind: "node", id: target.dataset.topoNode };
    return { kind: "group", id: target.dataset.topoGroup };
  }

function applyHighlight() {
    const root = live.container?.querySelector?.(".fabric-topology");
    if (!root || !live.model) return;
    const path = context.activePath(live.model, { hover: live.hover, pin: live.pin, query: live.query });
    const mark = (selector, set, key) => {
      root.querySelectorAll(selector).forEach((element) => {
        const id = element.getAttribute(key);
        element.classList.toggle("is-on-path", Boolean(path && path[set].has(id)));
        element.classList.toggle("is-dim", Boolean(path && !path[set].has(id)));
      });
    };
    mark("[data-topo-node]", "nodes", "data-topo-node");
    mark("[data-topo-link]", "links", "data-topo-link");
    mark("[data-topo-cross]", "links", "data-topo-cross");
    mark("[data-topo-bay]", "bays", "data-topo-bay");
    mark("[data-topo-port]", "ports", "data-topo-port");
    mark("[data-topo-group]", "groups", "data-topo-group");
    showTip(root);
    const clear = root.querySelector("[data-topology-clear]");
    if (clear) clear.hidden = !(live.pin || live.query);
    // The line under the legend names what is traced in full, so text a box
    // had to shorten is readable on hover and on keyboard focus alike.
    const pin = root.querySelector("[data-topology-pin]");
    if (pin) pin.textContent = context.traceSentence(live.model, { hover: live.hover, pin: live.pin });
  }

function showTip(root) {
    const tip = root.querySelector?.("[data-topology-tip]");
    if (!tip) return;
    const card = live.hover && live.hover.kind !== "group" ? cardFor(live.model, live.hover) : null;
    if (!card || !card.rows.length) {
      tip.hidden = true;
      live.tipBox = null;
      return;
    }
    tip.innerHTML = cardHtml(card);
    tip.hidden = false;
    placeTip(root, tip);
  }

const DRAG_SLOP = 4;

const TIP_KEYS = { tier: "data-topo-tier", port: "data-topo-port", link: "data-fabric-topology-link", bay: "data-topo-bay", node: "data-topo-node" };

function hoveredElement(root) {
    if (live.hoverElement?.isConnected && root.contains(live.hoverElement)) return live.hoverElement;
    const key = TIP_KEYS[live.hover?.kind];
    return key ? [...root.querySelectorAll(`[${key}]`)].find((item) => item.getAttribute(key) === live.hover.id) || null : null;
  }

function grown(box, by) {
    return { left: box.left - by, top: box.top - by, right: box.right + by, bottom: box.bottom + by };
  }

function shared(a, b) {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? w * h : 0;
  }

const TIP_KEEP_OFF = 1000;

const TIP_ON_PATH = 1;

const TIP_OTHER = 0.03;

function tipObstacles(root, element) {
    const found = [];
    const svg = root.querySelector(".fabric-topology-svg");
    if (!svg) return found;
    const weight = (item) => (item.classList.contains("is-on-path") ? TIP_ON_PATH : TIP_OTHER);
    for (const item of svg.querySelectorAll("[data-topo-node], [data-topo-bay], .topo-link-label, .topo-tier")) {
      if (item !== element) found.push([grown(item.getBoundingClientRect(), 6), weight(item)]);
    }
    // Lines by their pieces: a line's own box is not its shape.
    const frame = svg.getBoundingClientRect();
    const scale = frame.width / (svg.viewBox?.baseVal?.width || frame.width || 1);
    for (const line of svg.querySelectorAll("polyline.topo-link")) {
      const points = String(line.getAttribute("points") || "").trim().split(/\s+/).map((pair) => pair.split(",").map(Number));
      for (let index = 0; index + 1 < points.length; index += 1) {
        const [[x1, y1], [x2, y2]] = [points[index], points[index + 1]];
        found.push([grown({
          left: frame.left + Math.min(x1, x2) * scale, right: frame.left + Math.max(x1, x2) * scale,
          top: frame.top + Math.min(y1, y2) * scale, bottom: frame.top + Math.max(y1, y2) * scale,
        }, 8), weight(line)]);
      }
    }
    return found;
  }

function placeTip(root, tip) {
    if (typeof root.getBoundingClientRect !== "function" || typeof window === "undefined") return;
    const element = hoveredElement(root);
    if (!element) {
      tip.hidden = true;
      return;
    }
    const frame = root.getBoundingClientRect();
    const [width, height] = [tip.offsetWidth || 0, tip.offsetHeight || 0];
    // The item itself (a line is kept off at the pointer, and by its pieces),
    // its bay for a port, and the pointer.
    const keepOff = [];
    if (!element.matches("polyline, path, line")) keepOff.push(grown(element.getBoundingClientRect(), 8));
    if (live.hover.kind === "port") {
      const bayId = live.model.portByKey.get(live.hover.id)?.bayId;
      const cell = [...root.querySelectorAll("[data-topo-bay]")].find((item) => item.getAttribute("data-topo-bay") === bayId);
      if (cell) keepOff.push(grown(cell.getBoundingClientRect(), 6));
    }
    if (live.pointer) keepOff.push({ left: live.pointer.x - 16, top: live.pointer.y - 16, right: live.pointer.x + 16, bottom: live.pointer.y + 24 });
    if (!keepOff.length) return;
    const anchor = keepOff[0];
    // Where it may stand: the window and this view, above the trace band;
    // inside the drawing's box when the card fits there.
    const limit = (box, other) => ({ left: Math.max(box.left, other.left), top: Math.max(box.top, other.top), right: Math.min(box.right, other.right), bottom: Math.min(box.bottom, other.bottom) });
    const fits = (box) => box.right - box.left >= width && box.bottom - box.top >= height;
    let room = limit(grown(frame, -4), { left: 4, top: 4, right: (window.innerWidth || frame.right) - 4, bottom: (window.innerHeight || frame.bottom) - 4 });
    const band = root.querySelector("[data-topology-trace]")?.getBoundingClientRect();
    if (band && fits({ ...room, bottom: Math.min(room.bottom, band.top - 4) })) room = { ...room, bottom: Math.min(room.bottom, band.top - 4) };
    const drawing = scrollBox(live.container)?.getBoundingClientRect?.();
    if (drawing && fits(limit(room, grown(drawing, -4)))) room = limit(room, grown(drawing, -4));
    const obstacles = [...keepOff.map((box) => [box, TIP_KEEP_OFF]), ...tipObstacles(root, element)];
    const centre = (anchor.left + anchor.right) / 2;
    let best = null;
    const lastX = Math.max(room.left, room.right - width);
    const lastY = Math.max(room.top, room.bottom - height);
    for (let top = room.top; ; top = Math.min(lastY, top + 16)) {
      for (let left = room.left; ; left = Math.min(lastX, left + 16)) {
        const box = { left, top, right: left + width, bottom: top + height };
        const dx = Math.max(anchor.left - box.right, box.left - anchor.right, 0);
        const dy = Math.max(anchor.top - box.bottom, box.top - anchor.bottom, 0);
        // Nearest first; the right of the item before its left.
        let cost = Math.hypot(dx, dy) + ((box.left + box.right) / 2 < centre ? 24 : 0);
        for (const [other, weight] of obstacles) {
          cost += weight * shared(box, other);
          if (best && cost >= best.cost) break;
        }
        if (!best || cost < best.cost) best = { cost, box };
        if (left >= lastX) break;
      }
      if (top >= lastY) break;
    }
    live.tipBox = best.box;
    tip.style.left = `${Math.round(best.box.left - frame.left)}px`;
    tip.style.top = `${Math.round(best.box.top - frame.top)}px`;
  }

function updateSearchStatus() {
    if (!live.status) return;
    const text = searchStatusText();
    Object.assign(live.status, { textContent: text, title: text });
  }

function searchStatusText() {
    if (!live.query) return "";
    const found = context.searchTopology(live.model, live.query);
    if (!found.matches.length) return `No match for "${live.query}".`;
    const names = found.matches.slice(0, 6).map((item) => `${item.label}${item.collapsed ? " (inside a collapsed block)" : ""}`);
    const more = found.matches.length > names.length ? `, and ${found.matches.length - names.length} more` : "";
    return `${found.matches.length} ${found.matches.length === 1 ? "match" : "matches"}: ${names.join(", ")}${more}.`;
  }

const globals = { listening: false, timer: null, widthTimer: null };

function cancelWidthTimer() {
    if (globals.widthTimer !== null && typeof window.clearTimeout === "function") window.clearTimeout(globals.widthTimer);
    globals.widthTimer = null;
  }

function clearView(quiet) {
    live.quiet = quiet || null;
    live.pin = null;
    live.hover = null;
    live.hoverElement = null;
    clearInput();
    applyHighlight();
    live.hooks.clearSelection?.();
    live.hooks.requestRender?.();
  }

function onEscape(event) {
    if (event.key !== "Escape" || !live.container?.querySelector?.(".fabric-topology")) return;
    if (event.target?.closest?.("[data-fabric-alias-form]")) return;
    clearView(refFromElement(event.target) || live.hover || live.pin);
  }

function onResize() {
    // The layout takes the box's width: lay it out again when that changes.
    if (globals.timer !== null) window.clearTimeout(globals.timer);
    globals.timer = window.setTimeout(() => {
      globals.timer = null;
      if (live.container?.querySelector?.(".fabric-topology")) live.hooks.requestRender?.();
    }, 200);
  }

function listen() {
    if (globals.listening) return;
    if (typeof document !== "undefined" && typeof document.addEventListener === "function") document.addEventListener("keydown", onEscape);
    if (typeof window.addEventListener === "function") window.addEventListener("resize", onResize);
    if (typeof document !== "undefined") document.fonts?.addEventListener?.("loadingdone", remeasure);
    watchResolution();
    globals.listening = true;
  }

function unlisten() {
    if (!globals.listening) return;
    if (typeof document !== "undefined" && typeof document.removeEventListener === "function") document.removeEventListener("keydown", onEscape);
    if (typeof window.removeEventListener === "function") window.removeEventListener("resize", onResize);
    if (typeof document !== "undefined") document.fonts?.removeEventListener?.("loadingdone", remeasure);
    hold.resolution?.removeEventListener?.("change", onResolution);
    hold.resolution = null;
    if (globals.timer !== null && typeof window.clearTimeout === "function") window.clearTimeout(globals.timer);
    globals.timer = null;
    globals.listening = false;
  }

function attach(container, searchInput, searchStatus, hooks = {}) {
    if (!container) return;
    Object.assign(live, { container, input: searchInput || null, status: searchStatus || null, hooks: hooks || {} });
    listen();
    if (container.dataset?.topologyAttached) return;
    if (container.dataset) container.dataset.topologyAttached = "true";
    const hover = (event) => {
      if (live.container !== container) return;
      const ref = refFromElement(event.target);
      if (!ref) return;
      if (live.quiet && event.type === "focusin" && sameRef(ref, live.quiet)) return;
      live.quiet = null;
      live.hover = ref;
      // The hover card is placed for this item, and off the pointer if any.
      live.hoverElement = event.target?.closest?.("[data-topo-port], [data-topo-link], [data-topo-bay], [data-topo-node], [data-topo-tier]") || null;
      live.pointer = event.type === "mouseover" && Number.isFinite(event.clientX) ? { x: event.clientX, y: event.clientY } : null;
      applyHighlight();
    };
    const leave = (event) => {
      if (live.container !== container || !live.hover) return;
      live.hover = refFromElement(event.relatedTarget);
      live.hoverElement = live.hover ? event.relatedTarget?.closest?.("[data-topo-port], [data-topo-link], [data-topo-bay], [data-topo-node], [data-topo-tier]") || null : null;
      if (!live.hover) live.pointer = null;
      applyHighlight();
    };
    // The card keeps its place while the pointer stays on the item; it moves
    // only when the pointer comes up to it (along a line, say).
    container.addEventListener("mousemove", (event) => {
      if (live.container !== container || !live.hover || !live.pointer || !live.tipBox) return;
      live.pointer = { x: event.clientX, y: event.clientY };
      const near = grown(live.tipBox, 20);
      if (event.clientX < near.left || event.clientX > near.right || event.clientY < near.top || event.clientY > near.bottom) return;
      const root = container.querySelector(".fabric-topology");
      const tip = root?.querySelector?.("[data-topology-tip]");
      if (tip && !tip.hidden) placeTip(root, tip);
    });
    container.addEventListener("mouseover", hover);
    container.addEventListener("focusin", hover);
    container.addEventListener("mouseout", leave);
    container.addEventListener("focusout", leave);
    container.addEventListener("click", (event) => {
      if (live.container !== container) return;
      if (event.target?.closest?.("[data-topology-fit]")) {
        // Fit puts the fitted drawing at offsets 0/0; Actual size goes back
        // to the offsets held before Fit.
        const box = scrollBox(container);
        if (!live.fit) live.actualScroll = { left: (Number(box?.scrollLeft) || 0) - live.room.left, top: (Number(box?.scrollTop) || 0) - live.room.top };
        else live.restoreScroll = live.actualScroll;
        live.fit = !live.fit;
        live.refocusFit = true;
        live.hooks.requestRender?.();
        return;
      }
      if (event.target?.closest?.("[data-topology-clear]")) {
        // Focus goes back to what was pinned, which stays quiet.
        const was = live.pin;
        live.refocusPin = was;
        clearView(was);
        return;
      }
      const element = event.target?.closest?.("[data-topo-port], [data-topo-link], [data-topo-bay], [data-topo-node], [data-topo-tier]");
      // A stub's link opens its enclosure's view: the page switches to it.
      if (element?.dataset?.fabricEnclosure) return;
      const ref = refFromElement(element);
      if (ref?.kind === "tier") return;
      if (!ref) {
        // A click on the drawing's background clears the view. Not a drag or
        // a scroll of the drawing: the pointer went down and came up in one
        // place, and the box did not move under it. Not its scroll bars, a
        // shelf's expand control, or anything outside the drawing's box.
        const box = event.target?.closest?.(".fabric-topology-scroll");
        const down = live.down;
        live.down = null;
        if (!box || !down || !(live.pin || live.query) || event.target.closest("[data-fabric-expand-slots]")) return;
        const moved = Math.hypot(event.clientX - down.x, event.clientY - down.y) > DRAG_SLOP
          || down.left !== box.scrollLeft || down.top !== box.scrollTop;
        const onBar = event.target === box && (event.offsetX >= box.clientWidth || event.offsetY >= box.clientHeight);
        if (!moved && !onBar) clearView(null);
        return;
      }
      if (sameRef(ref, live.pin)) {
        // A second click on the pinned item unpins it. The page does not see
        // this click, or it would select the item again.
        event.stopPropagation?.();
        clearView(ref);
        return;
      }
      live.quiet = null;
      live.pin = ref;
      applyHighlight();
      if (ownPin(live.model, ref)) {
        // A link or a physical enclosure is not a page selection: the
        // inspector shows its facts.
        live.hooks.clearSelection?.();
        live.hooks.requestRender?.();
      }
    });
    // Where the pointer went down, for telling a click from a drag.
    container.addEventListener("mousedown", (event) => {
      const box = event.target?.closest?.(".fabric-topology-scroll");
      live.down = box ? { x: event.clientX, y: event.clientY, left: box.scrollLeft, top: box.scrollTop } : null;
    });
    container.addEventListener("keydown", (event) => {
      const element = event.target?.closest?.(".fabric-topology [role=\"button\"]");
      if (!element || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    });
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        live.query = searchInput.value.trim();
        const hadPin = Boolean(live.pin);
        if (live.query) live.pin = null;
        updateSearchStatus();
        applyHighlight();
        if (hadPin && live.query) {
          live.hooks.clearSelection?.();
          live.hooks.requestRender?.();
        }
      });
    }
  }

  Object.defineProperties(context, {
    cableName: { configurable: true, get: () => cableName },
    laneCap: { configurable: true, get: () => laneCap },
    cableEnds: { configurable: true, get: () => cableEnds },
    unpairedPhys: { configurable: true, get: () => unpairedPhys },
    slowLanes: { configurable: true, get: () => slowLanes },
    stateCounts: { configurable: true, get: () => stateCounts },
    transportNote: { configurable: true, get: () => transportNote },
    unmatchedTargets: { configurable: true, get: () => unmatchedTargets },
    targetCount: { configurable: true, get: () => targetCount },
    wiringChecks: { configurable: true, get: () => wiringChecks },
    flagTier: { configurable: true, get: () => flagTier },
    flagRows: { configurable: true, get: () => flagRows },
    flagsOf: { configurable: true, get: () => flagsOf },
    headRows: { configurable: true, get: () => headRows },
    hbaRows: { configurable: true, get: () => hbaRows },
    hbaLanes: { configurable: true, get: () => hbaLanes },
    phyPeer: { configurable: true, get: () => phyPeer },
    expanderRows: { configurable: true, get: () => expanderRows },
    cableRows: { configurable: true, get: () => cableRows },
    busPorts: { configurable: true, get: () => busPorts },
    busRows: { configurable: true, get: () => busRows },
    portRows: { configurable: true, get: () => portRows },
    portBrief: { configurable: true, get: () => portBrief },
    cardFor: { configurable: true, get: () => cardFor },
    cardText: { configurable: true, get: () => cardText },
    cardHtml: { configurable: true, get: () => cardHtml },
    tone: { configurable: true, get: () => tone },
    kvRow: { configurable: true, get: () => kvRow },
    phyLine: { configurable: true, get: () => phyLine },
    detailFor: { configurable: true, get: () => detailFor },
    enclosureDetail: { configurable: true, get: () => enclosureDetail },
    kindLabel: { configurable: true, get: () => kindLabel },
    ownPin: { configurable: true, get: () => ownPin },
    live: { configurable: true, get: () => live },
    DEFAULT_HINT: { configurable: true, get: () => DEFAULT_HINT },
    scrollBox: { configurable: true, get: () => scrollBox },
    refExists: { configurable: true, get: () => refExists },
    sameRef: { configurable: true, get: () => sameRef },
    refFromSelection: { configurable: true, get: () => refFromSelection },
    clearInput: { configurable: true, get: () => clearInput },
    expandedKeys: { configurable: true, get: () => expandedKeys },
    boxRect: { configurable: true, get: () => boxRect },
    render: { configurable: true, get: () => render },
    firstViewLeft: { configurable: true, get: () => firstViewLeft },
    sizeRoom: { configurable: true, get: () => sizeRoom },
    keepView: { configurable: true, get: () => keepView },
    mount: { configurable: true, get: () => mount },
    bandWatch: { configurable: true, get: () => bandWatch },
    onBandResize: { configurable: true, get: () => onBandResize },
    hold: { configurable: true, get: () => hold },
    HOLD_WIDTHS: { configurable: true, get: () => HOLD_WIDTHS },
    holdKey: { configurable: true, get: () => holdKey },
    holdBand: { configurable: true, get: () => holdBand },
    putHold: { configurable: true, get: () => putHold },
    measureBand: { configurable: true, get: () => measureBand },
    remeasure: { configurable: true, get: () => remeasure },
    onResolution: { configurable: true, get: () => onResolution },
    watchResolution: { configurable: true, get: () => watchResolution },
    watchBand: { configurable: true, get: () => watchBand },
    unwatchBand: { configurable: true, get: () => unwatchBand },
    release: { configurable: true, get: () => release },
    view: { configurable: true, get: () => view },
    drawnModel: { configurable: true, get: () => drawnModel },
    inspectorDetail: { configurable: true, get: () => inspectorDetail },
    composeInspector: { configurable: true, get: () => composeInspector },
    boardLabel: { configurable: true, get: () => boardLabel },
    refFromElement: { configurable: true, get: () => refFromElement },
    applyHighlight: { configurable: true, get: () => applyHighlight },
    showTip: { configurable: true, get: () => showTip },
    DRAG_SLOP: { configurable: true, get: () => DRAG_SLOP },
    TIP_KEYS: { configurable: true, get: () => TIP_KEYS },
    hoveredElement: { configurable: true, get: () => hoveredElement },
    grown: { configurable: true, get: () => grown },
    shared: { configurable: true, get: () => shared },
    TIP_KEEP_OFF: { configurable: true, get: () => TIP_KEEP_OFF },
    TIP_ON_PATH: { configurable: true, get: () => TIP_ON_PATH },
    TIP_OTHER: { configurable: true, get: () => TIP_OTHER },
    tipObstacles: { configurable: true, get: () => tipObstacles },
    placeTip: { configurable: true, get: () => placeTip },
    updateSearchStatus: { configurable: true, get: () => updateSearchStatus },
    searchStatusText: { configurable: true, get: () => searchStatusText },
    globals: { configurable: true, get: () => globals },
    cancelWidthTimer: { configurable: true, get: () => cancelWidthTimer },
    clearView: { configurable: true, get: () => clearView },
    onEscape: { configurable: true, get: () => onEscape },
    onResize: { configurable: true, get: () => onResize },
    listen: { configurable: true, get: () => listen },
    unlisten: { configurable: true, get: () => unlisten },
    attach: { configurable: true, get: () => attach },
  });
  };
})();
