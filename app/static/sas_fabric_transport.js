// Shared Linux SAS port references. PHY observations stay on their hardware
// owner; a missing, ambiguous or stale reference is not a measured zero.
(function () {
  "use strict";
  const list = (value) => Array.isArray(value) ? value : [];
  const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  function resolveTransportPort(fabric, metric) {
    const id = typeof metric === "string" ? metric : metric?.sas_transport_port_id;
    const unread = { id, state: "not_read", phys: [] };
    const table = fabric?.raw?.sas_transport_port_index;
    if (typeof id !== "string" || !record(table) || !Object.prototype.hasOwnProperty.call(table, id)) return unread;
    const port = table[id];
    if (!record(port) || port.id !== id || port.state !== "read") return unread;
    const owner = list(fabric.nodes).find((node) => node.id === port.owner_id && ["controller", "expander"].includes(node.kind));
    const names = list(port.phy_names);
    if (!owner || !names.length || names.length > 256 || names.some((name) => typeof name !== "string") || new Set(names).size !== names.length) return unread;
    const wanted = new Set(names);
    const rows = list(owner.raw?.phys || owner.raw?.phy_rows).filter((row) => record(row) && wanted.has(row.name));
    if (rows.length !== names.length || new Set(rows.map((row) => row.name)).size !== names.length) return unread;
    return { id, state: "read", phys: rows };
  }
  function pathState(fabric, state) {
    const result = { controller: String(state.controller_label || state.controller || ""), state: String(state.state ?? "unknown"), basis: String(state.state_basis ?? "") };
    if (state.sas_transport_port_id) {
      const port = resolveTransportPort(fabric, state);
      result.transportPhyCount = port.state === "read" ? port.phys.length : null;
    }
    return result;
  }
  window.SasFabricTransport = { resolveTransportPort, pathState };
})();
