/* Storage Fabric Topology: identity and attribution, one reading for every surface. */
(function () {
  window.SasFabricTopologyEvidence = function (context) {
// The model's helpers, by the names the attribution below uses. Each is
// looked up when called, so a later module's definitions are the ones read.
const text = (...args) => context.text(...args);
const list = (...args) => context.list(...args);
const chipOfNode = (...args) => context.chipOfNode(...args);
const nodeName = (...args) => context.nodeName(...args);
const sasAddressOf = (...args) => context.sasAddressOf(...args);
const phyReading = (...args) => context.phyReading(...args);
const deviceKind = (...args) => context.deviceKind(...args);
const cableEndAccepts = (...args) => context.cableEndAccepts(...args);
const { NOT_READ, NAME_NOT_UNIQUE } = context;

// One role ledger per physical PHY, across every observation of its chip:
// each row's device type, by the shared vocabulary, and each transport
// census record that lists it (by number, or by a name that resolves to its
// row). A record not read is unread evidence, never a role. Fixed on first
// use.
function phyRoles(model, id, phy) {
    const chip = chipOfNode(model, id)?.id || id;
    if (!model.roleLedger) model.roleLedger = new Map();
    const key = `${chip}|${phy}`;
    if (model.roleLedger.has(key)) return model.roleLedger.get(key);
    const reading = phyReading(model, id, phy);
    const roles = new Set();
    // The finer kind of each link: an initiator, an expander, an inferred
    // cable end; which cable end it can be is the table's.
    const attached = new Set();
    const unknown = [];
    const unread = [];
    const add = (kind) => {
      attached.add(kind);
      roles.add(({ initiator: "link", "sata initiator": "link", expander: "link", cable: "link" })[kind] || kind);
    };
    for (const device of reading.devices) {
      for (const part of device.split(" / ")) {
        const kind = deviceKind(part);
        if (!kind) continue;
        if (kind === "unknown") unknown.push(part);
        else if (kind === "unread") {
          // A row whose own type is not read: what is attached is not read.
          unread.push(part === NAME_NOT_UNIQUE ? "its PHY name is on more than one row of its table, so no census record identifies it" : `its row says ${part}`);
          add("unread");
        }
        else add(kind);
      }
    }
    // A CORE row names its attached device's type; a row that names none
    // has it not read (on the transport platform types come from the census).
    if (!model.transport && reading.observations.some((item) => !text(item.row.device))) {
      unread.push("its row names no attached device type");
      add("unread");
    }
    const listing = list(model.censusRows).filter((port) => (chipOfNode(model, port.owner_id)?.id || port.owner_id) === chip && list(port.phys).includes(phy));
    const records = listing.filter((port) => port.state === "read");
    if (listing.some((port) => port.state === "ambiguous")) unread.unshift(`the transport census names it by ${listing.find((port) => port.state === "ambiguous").ambiguous.join(", ")}, a PHY name on more than one row, so that record identifies no PHY`);
    if (listing.some((port) => !["read", "ambiguous"].includes(port.state))) unread.unshift("its transport census record was not read");
    const kinds = new Set(records.map((port) => port.remote_kind || ""));
    for (const kind of kinds) {
      if (kind === "end_device") add("disk");
      else if (kind === "expander") add("expander");
      else {
        unread.push(`the transport census does not say what it is attached to`);
        add("unread");
      }
    }
    if (unknown.length) roles.add("unknown");
    // The targets the read expander records name, each one device whatever
    // order the records came in: a present chip, or by SAS address or id a
    // device not in this payload. An address two chips carry names neither.
    // A record's id and its SAS address each name a target: where they name
    // two, both are kept, and an id naming something other than an expander
    // is a target of its own.
    const targets = new Map();
    for (const port of records.filter((item) => item.remote_kind === "expander")) {
      const address = sasAddressOf(port.remote_sas_address);
      const remote = port.remote_id ? model.nodeById.get(port.remote_id) : null;
      if (remote?.kind === "expander") targets.set(chipOfNode(model, port.remote_id)?.id || port.remote_id, nodeName(model, chipOfNode(model, port.remote_id)?.id || port.remote_id));
      else if (remote) targets.set(`kind:${port.remote_id}`, `${nodeName(model, port.remote_id)}, which is not an expander`);
      else if (port.remote_id && !address) targets.set(`id:${port.remote_id}`, `a device not in this payload (${port.remote_id})`);
      if (address) {
        const found = [...new Set(model.nodes.filter((entry) => entry.kind === "expander" && [entry.address, sasAddressOf(entry.rawId)].includes(address)).map((entry) => chipOfNode(model, entry.id)?.id || entry.id))];
        const keyed = found.length === 1 ? found[0] : found.length > 1 ? `ambiguous:${address}` : `address:${address}`;
        targets.set(keyed, found.length === 1 ? nodeName(model, found[0]) : found.length > 1 ? `SAS address ${address}, which more than one expander here carries` : `a device not in this payload (${address})`);
      }
    }
    // The disks the read end-device records name, one per identity: two
    // disks on one PHY are no one disk's. A record missing a field is not
    // read for it, never a second disk.
    const disks = diskGroups(records.filter((item) => item.remote_kind === "end_device").map(censusPlace), context.addressAmbiguity(model));
    const entry = { roles, attached, unknown: [...new Set(unknown)], unread, records, kinds, targets, disks };
    model.roleLedger.set(key, entry);
    return entry;
  }

const ROLE_WORDS = { disk: "a disk", link: "a link to an HBA or expander", sep: "an enclosure processor", none: "nothing attached", unread: "an attachment not read" };
const KIND_WORDS = { initiator: "an HBA (an initiator)", "sata initiator": "a SATA initiator", expander: "an expander", cable: "an inferred cable end" };

// The targets a PHY's read census records name, as words, in one order.
function targetWords(targets) {
  return [...targets.values()].sort().join(" and ");
}

// Every device of a CORE HBA a device handle names in this payload: its
// expanders (drawn or only in the expander list), its bays' disks, its
// enclosures' processors. Handles are the HBA's own, so each kind is one
// device per record.
function handleDevices(model, id, handle) {
    if (!handle) return { expanders: [], bays: [], seps: [], hbas: [] };
    const expanders = new Set([...model.nodes.filter((entry) => entry.kind === "expander" && entry.controllerId === id && model.sas.expanders.get(entry.id)?.handle === handle).map((entry) => entry.id),
      ...[...model.sas.expanders.values()].filter((record) => record.units.has(id) && record.handles.has(handle)).map((record) => record.id)]);
    return {
      expanders: [...expanders].map((key) => model.nodeById.get(key) || { id: key }),
      bays: [...new Set(model.bays.filter((bay) => bay.ports.some((port) => port.controllerId === id && port.handle === handle)))],
      seps: list(model.sas.seps?.get(id)?.get(handle)),
      // The HBA itself, where any of its rows gives the handle as its own.
      hbas: list(model.sas.controllers.get(id)?.phys).some((row) => row.ownHandle === handle) ? [id] : [],
    };
  }

// What the builder's state for a handle says the one device is.
const HANDLE_KINDS = { end_device: "disk", sep: "sep", other_device: "other" };

// Which of a CORE HBA's lists were read, decided before any of its handles
// names anything: a list the payload does not say was read could carry a
// second device with any handle, so with one unread, or with no list states
// reported at all, no handle of that HBA names one device or none.
function listCoverage(model, id, handle) {
    const lists = model.sas.controllers.get(id)?.lists;
    const name = nodeName(model, id);
    if (!lists) return `the payload does not say which lists of ${name} were read, so what device handle ${handle} names is ${NOT_READ}`;
    const unread = ["expanders", "devices", "enclosures"].filter((key) => lists[key] !== "read");
    return unread.length ? `${name}'s ${unread.join(" and ")} ${unread.length === 1 ? "list was" : "lists were"} not read, so what device handle ${handle} names is ${NOT_READ}` : "";
  }

// What a CORE HBA row's device handle names, over every record of the
// payload that carries a handle of that HBA and what the builder found
// across the whole unit before selection: one expander, one disk attached
// directly, the enclosure processor, a device of the HBA's own list, more
// than one device or records that disagree with the row (not read), none
// read, or nothing here. Two devices read are ambiguous whatever else was
// read; otherwise every list must have been read before any device is
// named or called absent. A device is named only where every record of it
// agrees with the row: its kind is the row's, and a disk's own path has no
// expander and names this HBA as its parent.
function handleTarget(model, id, phy) {
    const rows = list(model.sas.controllers.get(id)?.phys).filter((item) => item.phy === phy);
    if (rows.length !== 1) return { state: rows.length ? "rows" : "" };
    const [row] = rows;
    if (!row.handle) return { state: row.rate !== null ? "none" : "", row };
    const name = nodeName(model, id);
    const { expanders, bays, seps, hbas } = handleDevices(model, id, row.handle);
    if (expanders.length > 1) return { state: "ambiguous", row, why: `device handle ${row.handle} names more than one expander of ${name}` };
    const kinds = [...expanders.map(() => "expander"), ...bays.map(() => "disk"), ...seps.map(() => "sep"), ...hbas.map(() => "hba")];
    const built = HANDLE_KINDS[row.handleState];
    const hba = model.sas.controllers.get(id);
    if (row.handleState === "ambiguous" || hba?.ambiguousHandles?.has(row.handle) || kinds.length > 1 || (built && kinds.length && kinds[0] !== built)) return { state: "ambiguous", row, why: `device handle ${row.handle} names more than one device of ${name}` };
    if (row.handleState === "conflict") return { state: "mismatch", row, why: `the records of ${name} disagree about what device handle ${row.handle} names` };
    if (hbas.length) return { state: "mismatch", row, why: `device handle ${row.handle} is one ${name} gives as its own` };
    const coverage = context.listCoverage(model, id, row.handle);
    if (coverage) return { state: "mismatch", row, why: coverage };
    if (row.handleState === "mismatch") return { state: "mismatch", row, why: `device handle ${row.handle} names a device of ${name}'s device list whose parent or type is not this row's` };
    // A reply the builder could not read never lets the rest name one device.
    if (row.handleState === "not_read") return { state: "mismatch", row, why: `a list of ${name} that could carry device handle ${row.handle} was not read, so what it names is ${NOT_READ}` };
    if (row.handleState === "unlisted_expander") return { state: "mismatch", row, why: `device handle ${row.handle} names an expander of ${name}'s device list that its expander list does not carry` };
    const kind = deviceKind(row.device);
    const says = (what) => ({ state: "mismatch", row, why: `its row says ${row.device || "no device type"}, but its device handle ${row.handle} names ${what}` });
    // An expander only some of whose records give it this handle, or place
    // it in this unit, is not named by it.
    const torn = expanders.find((entry) => { const record = model.sas.expanders.get(entry.id); return record && (record.handles.size > 1 || record.units.size > 1); });
    if (torn) return { state: "mismatch", row, why: `the records of ${nodeName(model, chipOfNode(model, torn.id)?.id || torn.id)} disagree about its device handle or its controller unit` };
    if (expanders.length === 1) return { state: "expander", row, id: chipOfNode(model, expanders[0].id)?.id || expanders[0].id };
    if (bays.length === 1) {
      const [bay] = bays;
      const port = bay.ports.find((item) => item.controllerId === id && item.handle === row.handle);
      const disk = `the disk in ${bay.label}${port?.device ? ` (${port.device})` : ""}`;
      if (kind !== "disk") return says(disk);
      // Attached directly only where its own path says so: no expander, and
      // this HBA's own handle as its parent.
      if (port.expanderId || (port.parent && row.ownHandle && port.parent !== row.ownHandle)) return { state: "mismatch", row, why: `device handle ${row.handle} names ${disk}, whose path ${port.expanderId ? `runs through ${nodeName(model, chipOfNode(model, port.expanderId)?.id || port.expanderId)}` : `names parent ${port.parent}, not ${row.ownHandle}`}` };
      return { state: port.parent && row.ownHandle ? "disk" : "disk-unread", row, bay, port, disk };
    }
    if (seps.length === 1 || built === "sep") return kind === "sep" ? { state: "sep", row } : says("the enclosure processor");
    if (built === "disk") return kind === "disk" ? { state: "device", row } : says("a disk of the HBA's device list");
    if (built === "other") return { state: "other", row };
    // Every list was read (above): nothing here carries the handle.
    return { state: "absent", row };
  }

// Where a CORE HBA's own row is read, a cable from it claims a PHY only
// where that row's handle names the cable's far end.
function handleClash(model, link, side, phy) {
    if (side !== "near" || model.transport || model.nodeById.get(link.source)?.kind !== "controller" || !list(model.sas.controllers.get(link.source)?.phys).length) return "";
    const target = handleTarget(model, link.source, phy);
    const far = chipOfNode(model, link.target)?.id || link.target;
    if (target.state === "expander" && target.id === far) return "";
    return target.state === "ambiguous" || target.state === "mismatch" ? target.why
      : target.state === "rows" ? `its number is on more than one row of ${nodeName(model, link.source)}`
      : target.state === "none" || !target.state ? `its row's device handle is ${NOT_READ}`
      : target.state === "expander" ? `its row's device handle ${target.row.handle} names ${nodeName(model, target.id)}, not this cable's far end`
      : `its row's device handle ${target.row.handle} names no expander of this cable`;
  }

const chipId = (model, id) => chipOfNode(model, id)?.id || id;

// A lane list as a record gives it, in the shape the model reads a list in.
function parseLanes(raw) {
    const whole = (values) => list(values).filter((value) => Number.isInteger(value));
    const rows = list(raw.lanes).filter((lane) => context.isRecord(lane) && Number.isInteger(lane.source_phy) && Number.isInteger(lane.target_phy)).map((lane) => ({ source: lane.source_phy, target: lane.target_phy }));
    return { count: raw.lane_count, sourcePhys: whole(raw.source_phys), targetPhys: whole(raw.target_phys), rows };
  }

// Every lane list a link's records give besides the one shown as reported.
function laneListsOf(link) {
    return list(link.listRecords).map(parseLanes);
  }

// Every claim the payload makes on a PHY as a cable's lane, whichever
// record makes it, decided once for every consumer: each list of each
// link record, drawn or not (at both ends and in its pairs, one PHY per
// lane or not), the far end the view infers for a drawn cable, and on
// Linux what each node record reports as its parent port's PHYs. By chip
// and PHY, the records that claim it, each with the chips at its two ends
// and the PHYs it lists at that end; and by parent port name, the devices
// that report being on it.
function laneClaims(model) {
    if (model.laneClaimIndex) return model.laneClaimIndex;
    const claims = new Map();
    const ports = new Map();
    const claim = (id, phys, source, target) => {
      const listed = new Set(phys.filter((phy) => Number.isInteger(phy)));
      const entry = { pair: `${chipId(model, source)}|${chipId(model, target)}`, source, target, listed };
      for (const key of [...listed].map((phy) => `${chipId(model, id)}|${phy}`)) (claims.get(key) || claims.set(key, []).get(key)).push(entry);
    };
    const drawn = model.links.flatMap((entry) => [entry, ...list(entry.stubCables)]).filter((entry) => context.LANE_LINKS.has(entry.kind) && entry.lanes);
    for (const { source, target, lanes } of [...drawn, ...list(model.laneRecords).map((record) => ({ ...record, lanes: parseLanes(record.lanes) }))]) {
      claim(source, [...lanes.sourcePhys, ...lanes.rows.map((lane) => lane.source)], source, target);
      claim(target, [...lanes.targetPhys, ...lanes.rows.map((lane) => lane.target)], source, target);
    }
    for (const item of list(model.parentClaims)) {
      if (item.port) ports.set(item.port, (ports.get(item.port) || new Set()).add(chipId(model, item.child)));
      if (typeof item.parent === "string") claim(item.parent, item.phys, item.parent, item.child);
    }
    model.laneClaimIndex = { claims, ports };
    return model.laneClaimIndex;
  }

// Every SAS address the payload gives a controller or expander: each record
// of it, and its own node. And, by address, the chips given it.
function addressesOf(model, id) {
    const node = model.nodeById.get(id);
    return new Set([...((model.sas.expanders.get(id) || model.sas.controllers.get(id))?.addresses || []), sasAddressOf(node?.rawId), sasAddressOf(node?.rawAddress), sasAddressOf(node?.address)].filter(Boolean));
  }

function addressHolders(model) {
    if (model.addressHolderIndex) return model.addressHolderIndex;
    const holders = new Map();
    for (const id of new Set([...model.sas.expanders.keys(), ...model.sas.controllers.keys(), ...model.nodes.filter((entry) => ["controller", "expander"].includes(entry.kind)).map((entry) => entry.id)])) {
      for (const address of addressesOf(model, id)) holders.set(address, (holders.get(address) || new Set()).add(chipId(model, id)));
    }
    model.addressHolderIndex = holders;
    return holders;
  }

// How a cable is said where another cable's card names it.
function cableWords(model, source, target) {
    const name = (id) => (model.nodeById.has(chipId(model, id)) ? nodeName(model, id) : "a device this view does not draw");
    return `the cable from ${name(source)} to ${name(target)}`;
  }

// A cable's members as the ledger reads them: each listed PHY's evidence
// must be a kind the table says can end this cable there, and every target
// a read census record names must be its other end. Otherwise which PHYs
// the cable uses is not read, for lanes, rates, counters and inspectors.
function memberConflicts(model, link) {
    if (!link.lanes) return [];
    const found = [];
    for (const [side, id, phys] of [["near", link.source, link.lanes.sourcePhys], ["far", link.target, link.lanes.targetPhys]]) {
      const other = chipOfNode(model, side === "near" ? link.target : link.source)?.id || (side === "near" ? link.target : link.source);
      for (const phy of new Set(phys)) {
        const ledger = phyRoles(model, id, phy);
        const kind = [...ledger.attached].find((item) => !cableEndAccepts(link.kind, side, item));
        const stray = [...ledger.roles].find((role) => !["link"].includes(role));
        if (kind || stray) found.push({ side, id, phy, why: `its row or the transport census names ${KIND_WORDS[kind] || ROLE_WORDS[stray] || "a device type whose role is not read"}, which cannot be this end of the cable` });
        else if ([...ledger.targets.keys()].some((key) => key !== other)) found.push({ side, id, phy, why: `the transport census names ${targetWords(ledger.targets)}` });
        else if (handleClash(model, link, side, phy)) found.push({ side, id, phy, why: handleClash(model, link, side, phy) });
      }
    }
    // More than one record of the link, giving different lists: no one list.
    if (link.listsDisagree) found.push({ side: "near", id: link.source, phy: link.lanes.sourcePhys[0], why: "the payload has more than one record of this link, and their lane lists differ" });
    // The SAS address of an end is one address by every record of that end
    // and by its own node, no other device's, and the address the list gives
    // for that end where it gives one: otherwise the list is not this cable's.
    for (const [side, id, address, phys] of [["near", link.source, link.listEnds[0], link.lanes.sourcePhys], ["far", link.target, link.listEnds[1], link.lanes.targetPhys]]) {
      const own = addressesOf(model, id);
      const elsewhere = (value) => [...(addressHolders(model).get(value) || [])].some((chip) => chip !== chipId(model, id));
      const at = { side, id, phy: phys[0] ?? link.lanes.sourcePhys[0] };
      if (own.size > 1) found.push({ ...at, why: `the records of ${nodeName(model, id)} give it more than one SAS address` });
      else if ([...own].some(elsewhere)) found.push({ ...at, why: `the SAS address of ${nodeName(model, id)} is also given for another device` });
      else if (address && ((own.size && !own.has(address)) || elsewhere(address))) found.push({ ...at, why: `the lane list gives another SAS address than ${nodeName(model, id)}'s for this end` });
    }
    // A Linux list is its parent port's, by every source that reads the port.
    if (model.transport) {
      const why = context.parentPortClash(model, link);
      if (why) found.push({ side: "near", id: link.source, phy: link.lanes.sourcePhys[0], why });
    }
    // Both ends must name each other on every lane before it is drawn or
    // judged: a retained list never stands in for its ends' own rows.
    if (!model.transport) {
      // The list must be the whole link, decided over both ends' tables.
      const whole = context.laneSetClash(model, link);
      if (whole) found.push(whole);
      // A CORE list without its pairs cannot show which ends name each other.
      if (!link.lanes.rows.length && link.lanes.count && (model.sas.controllers.has(link.source) || model.sas.expanders.has(link.source) || model.sas.expanders.has(link.target))) found.push({ side: "far", id: link.target, phy: link.lanes.targetPhys[0] ?? link.lanes.sourcePhys[0], why: "its lane pairs are not reported, so the two ends are not shown to name each other" });
      for (const lane of link.lanes.rows) {
        const why = reciprocalClash(model, link, lane);
        if (why) found.push({ side: "far", id: link.target, phy: lane.target, why });
      }
    }
    // A PHY anything else claims is no one cable's, and a cable that lists
    // one has no membership read: another cable's record (any of its lists),
    // another device's report of its parent port, a list of a link this view
    // does not draw, or a bay's path. The first such PHY at each end is named.
    const { claims } = laneClaims(model);
    const mine = `${chipId(model, link.source)}|${chipId(model, link.target)}`;
    for (const [side, id, phys] of [["near", link.source, link.lanes.sourcePhys], ["far", link.target, link.lanes.targetPhys]]) {
      for (const phy of new Set(phys)) {
        const key = `${chipId(model, id)}|${phy}`;
        // Another cable's record, or a record between the same two chips
        // that lists other PHYs at this end than this list does.
        const rival = list(claims.get(key)).find((entry) => entry.pair !== mine || entry.listed.size !== new Set(phys).size || phys.some((member) => !entry.listed.has(member)));
        const bay = list(ownerIndex(model).get(key)).find((item) => item.kind === "bay");
        if (!rival && !bay) continue;
        found.push({ side, id, phy, why: !rival ? `${bay.label} also claims it, as a disk port` : rival.pair !== mine ? `a record of ${cableWords(model, rival.source, rival.target)} also lists it` : "another record between these two ends lists it with other PHYs" });
        break;
      }
    }
    return found;
  }

// Whether a Linux cable's list is its parent port's whole membership,
// decided once over every source that reads the port: the list, what the
// cable's child reports of the parent port it is on (its name, its
// resolved parent, its PHYs and how many), and the port's one census
// record (its two ends, its PHY numbers, and its PHY names, each one row of
// the parent's table). A source that lacks an identifier or disagrees with
// the others leaves which PHYs the cable uses not read. A payload that
// reports no port census at all is judged on what the child reports.
function parentPortClash(model, link) {
    const near = nodeName(model, link.source);
    const far = nodeName(model, link.target);
    const listed = new Set(link.lanes.sourcePhys);
    const whole = (phys) => Array.isArray(phys) && phys.length === listed.size && new Set(phys).size === listed.size && phys.every((phy) => listed.has(phy));
    const child = link.ancestry;
    if (child?.torn) return `the records of ${far} disagree about the parent port it is on`;
    if (!child?.port) return `${far} does not name the parent port it is on, so no port's PHY list is shown to be this cable's`;
    if ((laneClaims(model).ports.get(child.port)?.size || 0) > 1) return `more than one device reports parent port ${child.port} as the port it is on`;
    if (child.parent !== link.source) return `${far} does not report ${near} as its resolved parent`;
    if (!whole(child.members) || child.count !== listed.size) return `the PHYs ${far} reports for parent port ${child.port} are not this cable's whole PHY list`;
    if (!model.transport.reported) return "";
    const id = `sas-port:${child.port}`;
    const ports = model.censusRows.filter((port) => port.id === id || (port.owner_id === link.source && port.remote_id === link.target));
    if (!ports.length) return `the port census has no record of parent port ${child.port}`;
    if (ports.length > 1) return `more than one census record names parent port ${child.port} or its two ends`;
    const [port] = ports;
    if (port.id !== id || port.state !== "read" || port.owner_id !== link.source || port.remote_kind !== "expander" || port.remote_id !== link.target) return `the census has no read record of parent port ${child.port} from ${near} to ${far}`;
    // Any other census record of the parent that lists one of the PHYs, by
    // number or by name, read or not, is a second claim to it.
    if (model.censusRows.some((entry) => entry !== port && chipId(model, entry.owner_id) === chipId(model, link.source) && list(entry.phys).some((phy) => listed.has(phy)))) return `another census record of ${near} also lists a PHY of parent port ${child.port}`;
    if (!whole(port.listed.numbers)) return `the PHY numbers of parent port ${child.port} are not this cable's whole PHY list`;
    if (!whole(port.listed.names)) return `the PHY names of parent port ${child.port} do not name the same PHYs of ${near} as its PHY numbers`;
    return "";
  }

// Whether a CORE lane list is the whole link, decided once over both ends'
// complete PHY tables rather than over the PHYs it lists: both ends are rows
// of one controller unit (handles are that unit's own), neither end's handle
// is one the unit carries on more than one device, and every row by which
// the near end names the far chip, and the far chip names the near end, is
// a PHY the list gives at that end. With no table at either end nothing
// shows the two ends name each other: the list is only reported.
function laneSetClash(model, link) {
    const nearHba = model.sas.controllers.get(link.source);
    const nearChip = nearHba || model.sas.expanders.get(link.source);
    const farChip = model.sas.expanders.get(link.target);
    const nearName = nodeName(model, link.source);
    const farName = nodeName(model, link.target);
    const first = link.lanes.targetPhys[0] ?? link.lanes.sourcePhys[0];
    const at = (side, phy, why) => ({ side, id: side === "near" ? link.source : link.target, phy, why });
    if (!nearChip && !farChip) return at("far", first, "neither end's PHY table is in the payload, so the two ends are not shown to name each other");
    // One end without a table is said per lane (its row is not read).
    if (!nearChip || !farChip) return null;
    const unit = nearHba ? link.source : nearChip.controllerId;
    if (!unit || farChip.controllerId !== unit) return at("far", first, `${farName} is a device of ${farChip.controllerId ? nodeName(model, farChip.controllerId) : `a controller unit ${NOT_READ}`}, another controller unit than ${nearName}'s, so its handles do not name ${nearName}`);
    const ambiguous = model.sas.controllers.get(unit)?.ambiguousHandles || new Set();
    const nearOwn = new Set((nearHba ? nearHba.phys.map((row) => row.ownHandle) : [nearChip.handle]).filter(Boolean));
    for (const handle of [...nearOwn, farChip.handle].filter(Boolean)) {
      const devices = handleDevices(model, unit, handle);
      const holders = devices.expanders.length + devices.bays.length + devices.seps.length + devices.hbas.length;
      if (holders > 1 || ambiguous.has(handle)) return at("far", first, `device handle ${handle} names more than one device of ${nodeName(model, unit)}`);
    }
    if (!nearHba) {
      const coverage = context.listCoverage(model, unit, farChip.handle);
      if (coverage) return at("far", first, coverage);
    }
    if (!farChip.handle || !nearOwn.size) return null;
    // Every row naming the other end, at each end, must be in the list there.
    // (A listed PHY whose own row does not name the other end, or whose
    // number is on two rows, is said per lane by reciprocalClash.)
    const towardFar = nearChip.phys.filter((row) => row.handle === farChip.handle).map((row) => row.phy);
    const towardNear = farChip.phys.filter((row) => row.handle && nearOwn.has(row.handle)).map((row) => row.phy);
    for (const [side, rows, listed, name, other] of [["near", towardFar, link.lanes.sourcePhys, nearName, farName], ["far", towardNear, link.lanes.targetPhys, farName, nearName]]) {
      const left = rows.find((phy) => !listed.includes(phy));
      if (left !== undefined) return at(side, left, `PHY ${left} of ${name} also names ${other}, which the lane list leaves out`);
    }
    return null;
  }

// Where a CORE cable's two ends do not name each other on one lane: the far
// row's attached handle must be the near end's own and its remote PHY the
// near PHY; an expander's near row must name the far chip and PHY in turn.
// A field not read cannot show that they do. Rows that are not there are
// said as not read elsewhere.
function reciprocalClash(model, link, lane) {
    const nearHba = model.sas.controllers.get(link.source);
    const nearChip = nearHba || model.sas.expanders.get(link.source);
    const farChip = model.sas.expanders.get(link.target);
    const near = nearChip?.byPhy.get(lane.source);
    const far = farChip?.byPhy.get(lane.target);
    const nearName = nodeName(model, link.source);
    // A payload with no PHY table at either end measures nothing of this
    // cable to attribute: its list is only what was reported.
    if (!nearChip && !farChip) return "";
    // A row not read cannot show the two ends name each other.
    if (!near) return `PHY ${lane.source} of ${nearName} has no one row read, so the two ends are not shown to name each other`;
    if (!far) return `its row is not read, so the two ends are not shown to name each other`;
    const nearOwn = nearHba ? near.ownHandle : nearChip.handle;
    if (!far.handle || !nearOwn || far.handle !== nearOwn) return `its row's device handle ${far.handle || NOT_READ} is not ${nearName}'s own (${nearOwn || NOT_READ}), so the two ends do not name each other`;
    if (far.remote !== lane.source) return `its row names remote PHY ${far.remote ?? NOT_READ}, not PHY ${lane.source} of ${nearName}`;
    if (!nearHba && (near.handle !== farChip.handle || !near.handle)) return `PHY ${lane.source} of ${nearName} names device handle ${near.handle || NOT_READ}, not this cable's far end`;
    if (!nearHba && near.remote !== lane.target) return `PHY ${lane.source} of ${nearName} names remote PHY ${near.remote ?? NOT_READ}, not PHY ${lane.target}`;
    // The far chip must report the near end as its parent.
    if (farChip.parent !== nearOwn) return `${nodeName(model, link.target)} reports parent ${farChip.parent || NOT_READ}, not ${nearName}'s own handle ${nearOwn}`;
    // And these rows must be the only rows of the unit that name these ends.
    const unit = [...model.sas.expanders.values()].filter((record) => record.units.has(farChip.controllerId));
    const claims = (handle, phy) => unit.flatMap((record) => record.phys).filter((row) => row.handle === handle && row.remote === phy).length;
    if (claims(nearOwn, lane.source) !== 1) return `${claims(nearOwn, lane.source)} rows of the unit name PHY ${lane.source} of ${nearName}, so this is not shown to be the one link`;
    if (claims(farChip.handle, lane.target) !== (nearHba ? 0 : 1)) return `another row of the unit names PHY ${lane.target} of ${nodeName(model, link.target)}, so this is not shown to be the one link`;
    return "";
  }

// A disk's place as a transport census record reports it: its port
// address, and an enclosure and bay number where it reads them.
function censusPlace(port) {
    const bay = Number.isInteger(port.bay_identifier) ? port.bay_identifier : null;
    return { address: sasAddressOf(port.remote_sas_address), enclosure: sasAddressOf(port.enclosure_identifier), bay };
  }

// Every disk port address no one place identifies, decided once over the
// whole payload and read by every consumer: the ledger, the claims and the
// disk groups below.
function addressAmbiguity(model) {
    if (!model.ambiguousAddresses) model.ambiguousAddresses = context.ambiguousAddresses(model);
    return model.ambiguousAddresses;
  }

// Records grouped into disks, in any order: one port address is one disk,
// and two records at one census slot (both bay numbers read, no enclosure
// read in both that differs) are one disk, a dual-ported disk's two port
// addresses included. A field one record does not read joins nothing and
// splits nothing; nor does an address the payload places at more than one
// slot (`ambiguous`): it identifies no disk, so it joins no records. Each
// group: its addresses, its slots, a label.
function diskGroups(places, ambiguous = new Set()) {
    const groups = [];
    const key = (address) => (address && !ambiguous.has(address) ? address : "");
    for (const place of places.filter((item) => item.address || item.bay !== null || item.bayId)) {
      // A census slot is one place only where its bay number and its
      // enclosure are both read: a missing enclosure joins nothing.
      const joins = groups.filter((group) => (key(place.address) && group.keys.has(place.address))
        || (place.bay !== null && place.bay !== undefined && place.enclosure && group.places.some((other) => other.bay === place.bay && other.enclosure === place.enclosure))
        || (place.bayId && group.places.some((other) => other.bayId === place.bayId)));
      const into = joins[0] || { addresses: new Set(), keys: new Set(), places: [] };
      for (const other of joins.slice(1)) {
        other.addresses.forEach((address) => into.addresses.add(address));
        other.keys.forEach((address) => into.keys.add(address));
        into.places.push(...other.places);
        groups.splice(groups.indexOf(other), 1);
      }
      if (place.address) into.addresses.add(place.address);
      if (key(place.address)) into.keys.add(place.address);
      into.places.push(place);
      if (!joins.length) groups.push(into);
    }
    for (const group of groups) {
      const bays = [...new Set(group.places.map((place) => (place.bayLabel ? place.bayLabel : place.bay !== null && place.bay !== undefined ? `transport bay ${place.bay}` : "")).filter(Boolean))].sort();
      group.slots = group.places.filter((place) => place.bay !== null && place.bay !== undefined);
      group.label = `${[...group.addresses].sort().join(", ") || "a disk"}${bays.length ? ` at ${bays.join(" and ")}` : ""}`;
    }
    return groups.sort((left, right) => left.label.localeCompare(right.label));
  }

// The disks a PHY's records name, as words: disks at distinct census slots
// are that many disks; otherwise the records do not name one disk.
function diskWords(groups) {
    const labels = groups.map((group) => group.label).join("; ");
    // Two groups are two disks only where some read place of each is
    // provably another slot: another bay number, or the same number in
    // another read enclosure. A bay of this view is not comparable with one.
    const apart = (one, two) => one.slots.some((left) => two.slots.some((right) => left.bay !== right.bay || (left.enclosure && right.enclosure && left.enclosure !== right.enclosure)));
    return groups.every((one, index) => groups.slice(index + 1).every((two) => apart(one, two))) ? `the transport census names ${groups.length === 2 ? "two" : groups.length} disks on it (${labels})`
      : `its records do not name one disk on it (${labels})`;
  }

// Whether a cable's members are read: at each end that lists them, its
// distinct PHYs, none repeated, as many as its lanes.
function membershipOf(link) {
    if (!link.lanes) return { read: false, ends: [], pairs: true };
    const ends = [["near", link.source, link.lanes.sourcePhys], ["far", link.target, link.lanes.targetPhys]]
      .filter(([side, , listed]) => side === "near" || listed.length)
      .map(([side, id, listed]) => {
        const distinct = new Set(listed).size;
        return { side, id, distinct, repeated: listed.length - distinct, read: distinct === listed.length && distinct === link.lanes.count };
      });
    // Where the source pairs the lanes, the pairs are the members: one per
    // lane, each end's PHYs distinct and the same set as its list.
    const rows = link.lanes.rows;
    const same = (values, listed) => {
      const set = new Set(values);
      return set.size === values.length && set.size === link.lanes.count && (!listed.length || (set.size === new Set(listed).size && listed.every((phy) => set.has(phy))));
    };
    const pairs = !rows.length || (rows.length === link.lanes.count && same(rows.map((lane) => lane.source), link.lanes.sourcePhys) && same(rows.map((lane) => lane.target), link.lanes.targetPhys));
    return { read: pairs && ends.every((end) => end.read), ends, pairs };
  }

function membershipRead(model, link) {
    return membershipOf(link).read && !context.memberConflicts(model, link).length;
  }

// Who a measured PHY belongs to, for every surface: a bay whose port is
// placed on it, or a cable that lists it at one end. One claimant and one
// row for its number: that owner. More than one claimant, a number on two
// rows, or a bay's PHY the census also lists as a link: the owner is not
// read, and every surface says so. The claims are fixed on first use, before
// neighbours are grouped.
// What the read transport census says is attached to an owner's PHY: the
// kinds every read record listing it names. A record not read names none.
function censusKinds(model, id, phy) {
    const ledger = context.phyRoles(model, id, phy);
    return { kinds: ledger.kinds, records: ledger.records };
  }

// Where an HBA's own row names a handle (CORE): the device it names, said
// only once every record is checked; only a read handle that nothing here
// carries goes outside this payload ("").
function handleDestination(model, id, phy) {
    const target = context.handleTarget(model, id, phy);
    const { row } = target;
    if (target.state === "none") return `destination ${context.NOT_READ}: its row names no device handle`;
    if (target.state === "ambiguous" || target.state === "mismatch") return `destination ${context.NOT_READ}: ${target.why}`;
    if (target.state === "disk") return `to ${target.disk}, attached directly; its path is not joined to this PHY`;
    if (target.state === "disk-unread") return `to ${target.disk}; whether it is attached directly is ${context.NOT_READ}`;
    if (target.state === "device") return `a disk the HBA's device list names (handle ${row.handle}), in no bay of this view`;
    if (target.state === "sep") return `the enclosure processor (handle ${row.handle})`;
    if (target.state === "other") return `a device the HBA's device list names (handle ${row.handle}) that is not a disk, expander or enclosure processor`;
    if (target.state !== "expander") return "";
    // A row whose own type cannot be a cable from this HBA names no expander.
    const kind = context.deviceKind(row.device);
    if (kind && !context.cableEndAccepts("controller-expander", "near", kind)) return `destination ${context.NOT_READ}: its row says ${row.device}, but its device handle names ${context.nodeName(model, target.id)}`;
    return `to ${context.nodeName(model, target.id)}; which cable it is in is ${context.NOT_READ}`;
  }

// A PHY's destination, said only where it is read: its owner by the one
// attribution, or what the read census alone names, or not read.
function destinationOf(model, id, phy) {
    const owner = phyOwner(model, id, phy);
    if (owner.known || owner.disputed) return owner.text;
    if (!model.transport) return handleDestination(model, id, phy);
    const { kinds, records, unread } = context.phyRoles(model, id, phy);
    if (!records.length) return `destination ${context.NOT_READ}${unread.length ? `: ${unread[0]}` : ""}`;
    // Two kinds named for one PHY are two roles: the attribution has said
    // its role is not read above.
    if (kinds.has("end_device")) return "a disk attached directly; no bay's path is joined to it";
    // Every read record's target, whatever order they came in: two targets,
    // present or not, are a destination not read.
    const { targets } = context.phyRoles(model, id, phy);
    if (targets.size > 1 || [...targets.keys()].some((key) => key.startsWith("ambiguous:"))) return `destination ${context.NOT_READ}: the transport census names ${context.targetWords(targets)}`;
    const [key] = [...targets.keys()];
    if (key && model.nodeById.has(key)) return `to ${context.nodeName(model, key)}; which cable it is in is ${context.NOT_READ}`;
    if (key) return `destination ${context.NOT_READ}: the transport census names ${targets.get(key)}`;
    return `destination ${context.NOT_READ}`;
  }

function ownerIndex(model) {
    if (model.phyOwners) return model.phyOwners;
    const owners = new Map();
    const claim = (id, phy, claimant) => {
      const key = `${context.chipOfNode(model, id)?.id || id}|${phy}`;
      if (!owners.has(key)) owners.set(key, []);
      const held = owners.get(key).find((other) => other.key === claimant.key);
      if (!held) owners.get(key).push(claimant);
      else {
        held.address = held.address || claimant.address;
        held.handle = held.handle || claimant.handle;
      }
    };
    const ambiguous = context.addressAmbiguity(model);
    for (const bay of model.bays) {
      for (const port of bay.ports) {
        if (!port.expanderId) continue;
        const address = ambiguous.has(port.sasAddress) ? port.sasAddress : "";
        // A CORE path's handle that names another device of its HBA too
        // (an expander, another bay's disk, an enclosure processor) does not
        // say which disk it is.
        const found = !model.transport && port.controllerId ? context.handleDevices(model, port.controllerId, port.handle) : null;
        const handle = found && (found.expanders.length + found.bays.length + found.seps.length + found.hbas.length > 1 || model.sas.controllers.get(port.controllerId)?.ambiguousHandles?.has(port.handle)) ? port.handle : "";
        for (const phy of port.phy ? [port.phy.phy] : context.list(port.phys)) claim(port.expanderId, phy, { key: `bay:${bay.bayId}`, kind: "bay", bay, label: bay.label, address, handle, controllerId: port.controllerId });
      }
    }
    // A cable claims the PHYs at an end only where its members there are
    // read: an invalid list claims none.
    // Every list any record of the link gives claims so, not only the one
    // shown as reported.
    for (const link of model.links.filter((entry) => context.LANE_LINKS.has(entry.kind) && entry.lanes)) for (const lanes of [link.lanes, ...laneListsOf(link)]) {
      const membership = membershipOf({ lanes });
      const readEnd = (side) => membership.pairs && membership.ends.some((end) => end.side === side && end.read);
      if (readEnd("near")) for (const phy of new Set(lanes.sourcePhys)) claim(link.source, phy, { key: `link:${link.id}`, kind: "cable", link, side: "near", label: `the cable to ${context.nodeName(model, link.target)}` });
      if (readEnd("far")) for (const phy of new Set(lanes.targetPhys)) claim(link.target, phy, { key: `link:${link.id}`, kind: "cable", link, side: "far", label: `the cable from ${context.nodeName(model, link.source)}` });
    }
    // A list of a link this view does not draw (an end that is not a drawn
    // node) is a claim too, where it is one PHY per lane: no cable to name.
    const drawn = new Set(model.links.flatMap((entry) => [entry, ...list(entry.stubCables)]).map((entry) => entry.id));
    for (const record of list(model.laneRecords).filter((item) => !drawn.has(item.key))) {
      const lanes = parseLanes(record.lanes);
      const membership = membershipOf({ lanes });
      const readEnd = (side) => membership.pairs && membership.ends.some((end) => end.side === side && end.read);
      const claimant = { key: `list:${record.key}`, kind: "list", label: "a lane list of a link this view does not draw" };
      if (readEnd("near")) for (const phy of new Set(lanes.sourcePhys)) claim(record.source, phy, claimant);
      if (readEnd("far")) for (const phy of new Set(lanes.targetPhys)) claim(record.target, phy, claimant);
    }
    model.phyOwners = owners;
    return owners;
  }

function phyOwner(model, id, phy) {
    // A list of a link this view does not draw disputes a bay's or a
    // cable's claim; with no other claimant it names no owner to show.
    const held = ownerIndex(model).get(`${context.chipOfNode(model, id)?.id || id}|${phy}`) || [];
    const claimants = held.some((item) => item.kind !== "list") ? held : [];
    const reading = context.phyReading(model, id, phy);
    const bays = claimants.filter((item) => item.kind === "bay").map((item) => item.bay);
    const cables = claimants.filter((item) => item.kind === "cable");
    // What the PHY's own rows say is attached must agree with its claimant:
    // a bay's PHY that a row or the census calls a link, or a cable's PHY
    // that a row or the census calls a disk, has its owner not read.
    // The PHY's role evidence must agree with its claimant: a bay's PHY that
    // a row or the census calls a link or the enclosure processor, or a
    // cable's PHY that names a disk or the enclosure processor, has its owner
    // not read; so has a device type outside the vocabulary. With no
    // claimant, evidence of two roles is the PHY's role not read.
    const ledger = context.phyRoles(model, id, phy);
    const evidence = ledger.roles;
    const clash = (allowed) => [...evidence].find((role) => role !== allowed) || "";
    // A bay claims a disk PHY only through a disk port address that
    // identifies one slot, and only where one disk is named on the PHY.
    const ambiguousBay = claimants.find((item) => item.kind === "bay" && item.address);
    const handleBay = claimants.find((item) => item.kind === "bay" && item.handle);
    // The disks named on the PHY: its census records' and each claiming
    // bay's own paths on it, one per identity.
    const chip = context.chipOfNode(model, id)?.id || id;
    const bayPlaces = claimants.filter((item) => item.kind === "bay").flatMap((item) => item.bay.ports
      .filter((port) => port.sasAddress && (context.chipOfNode(model, port.expanderId)?.id || port.expanderId) === chip && (port.phy ? port.phy.phy === phy : context.list(port.phys).includes(phy)))
      .map((port) => ({ address: port.sasAddress, bayId: item.bay.bayId, bayLabel: item.bay.label })));
    const ambiguousIds = context.addressAmbiguity(model);
    const places = [...ledger.records.filter((port) => port.remote_kind === "end_device").map(context.censusPlace), ...bayPlaces];
    const disks = bayPlaces.length ? context.diskGroups(places, ambiguousIds) : ledger.disks;
    // Any record on the PHY whose address names no one place leaves which
    // disk is on it not read, not only the claiming path's own address.
    const sharedAddress = ambiguousBay?.address || places.map((place) => place.address).find((address) => address && ambiguousIds.has(address)) || "";
    const bayClash = bays.length > 0 ? clash("disk") || (sharedAddress ? "address" : handleBay ? "bay-handle" : disks.length > 1 ? "disks" : "") : "";
    // A cable's PHY must be a kind the table says can end that cable at that
    // end, and every target the census names must be the cable's other end.
    const cableClash = cables.length !== 1 ? (cables.length ? clash("link") : "") : (() => {
      const [{ link: cable, side }] = cables;
      const role = clash("link");
      if (role) return role;
      const kind = [...ledger.attached].find((item) => !context.cableEndAccepts(cable.kind, side, item));
      if (kind) return `kind:${kind}`;
      const other = context.chipOfNode(model, side === "near" ? cable.target : cable.source)?.id || (side === "near" ? cable.target : cable.source);
      if ([...ledger.targets.keys()].some((key) => key !== other)) return "target";
      if (context.handleClash(model, cable, side, phy)) return "handle";
      // One conflict state for the whole cable: a retained list whose other
      // member's evidence does not fit claims none of its members.
      return context.memberConflicts(model, cable).length ? "members" : "";
    })();
    const roleClash = !claimants.length && (evidence.size > 1 || ledger.disks.length > 1);
    const known = claimants.length === 1 && !reading.conflict && !bayClash && !cableClash;
    const disputed = !known && (claimants.length > 1 || reading.conflict || Boolean(bayClash) || Boolean(cableClash) || roleClash);
    const hba = model.nodeById.get(id)?.kind === "controller";
    const says = (role) => (role === "unknown" ? `its row says ${ledger.unknown.join(" and ")}, a device type whose role is not read`
      : role === "target" ? `the transport census names ${context.targetWords(ledger.targets)}: destination ${context.NOT_READ}`
      : role === "handle" ? `${context.handleClash(model, cables[0].link, cables[0].side, phy)}: destination ${context.NOT_READ}`
      : role === "members" ? (() => {
        const [first] = context.memberConflicts(model, cables[0].link);
        return `the cable's members are ${context.NOT_READ} (PHY ${first.phy} at ${context.nodeName(model, first.id)}: ${first.why})`;
      })()
      : role === "address" ? `${ambiguousBay ? "its disk port address" : "a record on it names disk port address"} ${sharedAddress}, which is reported for more than one slot, so which disk is on it is ${context.NOT_READ}`
      : role === "bay-handle" ? `its device handle ${handleBay.handle} names more than one device of ${context.nodeName(model, handleBay.controllerId)}, so which disk is on it is ${context.NOT_READ}`
      : role === "disks" ? context.diskWords(disks)
      : role.startsWith("kind:") ? `its row or the transport census names ${context.KIND_WORDS[role.slice(5)]}, which cannot be this end of that cable`
      : role === "unread" ? `${ledger.unread.find((item) => !/census record was not read/.test(item)) || ledger.unread[0]}, so what is attached is not read`
      : role === "disk" ? "its row or the transport census names a disk on it" : role === "none" ? "its row or the transport census says nothing is attached" : `its row or the transport census calls it ${context.ROLE_WORDS[role]}`);
    const why = reading.conflict ? `its number is on more than one row of its ${hba ? "HBA" : "expander"}` : claimants.length > 1 ? `${claimants.map((item) => item.label).join(" and ")} all claim it`
      : bayClash || cableClash ? `${claimants[0].label} claims it, but ${says(bayClash || cableClash)}`
      : roleClash && evidence.size <= 1 ? context.diskWords(ledger.disks)
      : roleClash ? `its rows or the transport census name ${[...evidence].sort().map((role) => (role === "unknown" ? `a device type whose role is not read (${ledger.unknown.join(" and ")})` : context.ROLE_WORDS[role])).join(" and ")}${reading.devices.length ? ` (${reading.devices.join(", ")})` : ""}` : "";
    const sole = claimants[0];
    const text = known ? (sole.kind === "bay" ? sole.label : sole.side === "near" ? `cable to ${context.nodeName(model, sole.link.target)}` : `cable from ${context.nodeName(model, sole.link.source)}${sole.link.lanes.targetBasis === "inferred" ? ", inferred" : ""}`)
      : disputed ? `${roleClash ? "role" : "owner"} ${context.NOT_READ}: ${why}` : "";
    // A PHY no claimant owns and no read evidence classifies: why its role
    // is not read, where something says so.
    const note = known || disputed ? "" : evidence.has("unknown") ? `role ${context.NOT_READ}: ${says("unknown")}` : (!evidence.size || evidence.has("unread")) && ledger.unread.length ? `role ${context.NOT_READ}: ${ledger.unread[0]}` : "";
    return { bays, cables, claimants, known, kind: known ? sole.kind : "", link: known && sole.kind === "cable" ? sole.link : null, disputed, reading, text, note, roles: evidence, targetClash: cableClash === "target" };
  }

// Every disk port address no one place identifies, over the whole payload
// before any scope: an address two bays of this view report (their ports,
// traces or inventory slots), two census records place at two bay numbers
// or enclosures, a census record places in another enclosure than the bay
// that reports it, or the builder found ambiguous. A place a record does
// not read is not read, never another place; a census bay number is never
// compared with a bay of this view (their numbering need not agree).
function ambiguousAddresses(model) {
    const ambiguous = new Set();
    const census = new Map();
    for (const port of context.list(model.censusRows).filter((item) => item.remote_kind === "end_device")) {
      const place = context.censusPlace(port);
      if (!place.address) continue;
      if (port.remote_address_state === "ambiguous") ambiguous.add(place.address);
      census.set(place.address, [...(census.get(place.address) || []), place]);
    }
    const views = new Map();
    for (const bay of model.bays) {
      for (const address of new Set([...context.list(bay.reported?.addresses), ...bay.ports.map((port) => port.sasAddress)].filter(Boolean))) {
        if (!views.has(address)) views.set(address, []);
        views.get(address).push(bay);
      }
    }
    for (const address of new Set([...census.keys(), ...views.keys()])) {
      const places = census.get(address) || [];
      const bays = views.get(address) || [];
      const apart = (one, two) => (one.bay !== null && two.bay !== null && one.bay !== two.bay) || (one.enclosure && two.enclosure && one.enclosure !== two.enclosure);
      const elsewhere = places.some((place) => place.enclosure && bays.some((bay) => context.list(bay.reported?.enclosures).length && !bay.reported.enclosures.includes(place.enclosure)));
      // A bay whose own records report two enclosures is placed in neither.
      const torn = bays.some((bay) => context.list(bay.reported?.enclosures).length > 1);
      if (bays.length > 1 || elsewhere || torn || places.some((one, index) => places.slice(index + 1).some((two) => apart(one, two)))) ambiguous.add(address);
    }
    return ambiguous;
  }

// Whether a disk port's PHY is that port's own: the one attribution gives
// the PHY to the port's bay, and every path of that bay on the PHY names
// this port's address. Otherwise its rate and counters are not the port's.
function portPhyOwner(model, port) {
    const phy = port.phy?.phy;
    if (!Number.isInteger(phy) || !port.expanderId) return { ok: false, why: "" };
    const owner = phyOwner(model, port.expanderId, phy);
    if (!owner.known) return { ok: false, why: owner.text, owner };
    const bay = model.bayById.get(port.bayId);
    if (owner.kind !== "bay" || owner.bays[0]?.bayId !== port.bayId) return { ok: false, why: `${owner.text}'s, not ${bay?.label || "this bay"}'s`, owner };
    const chip = context.chipOfNode(model, port.expanderId)?.id || port.expanderId;
    const addresses = new Set(context.list(bay?.ports).filter((item) => item.phy?.phy === phy && (context.chipOfNode(model, item.expanderId)?.id || item.expanderId) === chip).map((item) => item.sasAddress));
    if (addresses.size !== 1 || addresses.has("")) return { ok: false, why: `which disk port of ${bay.label} uses PHY ${phy} is ${context.NOT_READ}`, owner };
    return { ok: true, why: "", owner };
  }

  Object.defineProperties(context, {
    phyRoles: { configurable: true, get: () => phyRoles },
    ROLE_WORDS: { configurable: true, get: () => ROLE_WORDS },
    KIND_WORDS: { configurable: true, get: () => KIND_WORDS },
    targetWords: { configurable: true, get: () => targetWords },
    handleDevices: { configurable: true, get: () => handleDevices },
    HANDLE_KINDS: { configurable: true, get: () => HANDLE_KINDS },
    listCoverage: { configurable: true, get: () => listCoverage },
    handleTarget: { configurable: true, get: () => handleTarget },
    handleClash: { configurable: true, get: () => handleClash },
    memberConflicts: { configurable: true, get: () => memberConflicts },
    parentPortClash: { configurable: true, get: () => parentPortClash },
    reciprocalClash: { configurable: true, get: () => reciprocalClash },
    laneSetClash: { configurable: true, get: () => laneSetClash },
    censusPlace: { configurable: true, get: () => censusPlace },
    addressAmbiguity: { configurable: true, get: () => addressAmbiguity },
    diskGroups: { configurable: true, get: () => diskGroups },
    diskWords: { configurable: true, get: () => diskWords },
    membershipOf: { configurable: true, get: () => membershipOf },
    membershipRead: { configurable: true, get: () => membershipRead },
    censusKinds: { configurable: true, get: () => censusKinds },
    handleDestination: { configurable: true, get: () => handleDestination },
    destinationOf: { configurable: true, get: () => destinationOf },
    ownerIndex: { configurable: true, get: () => ownerIndex },
    phyOwner: { configurable: true, get: () => phyOwner },
    ambiguousAddresses: { configurable: true, get: () => ambiguousAddresses },
    portPhyOwner: { configurable: true, get: () => portPhyOwner },
  });
  };
})();
