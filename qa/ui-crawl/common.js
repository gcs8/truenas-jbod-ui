"use strict";

// Shared by inventory.js and actuation.js (kept apart to avoid a require cycle).

const SAFETY_CLASSES = ["read", "app-write-revert", "led", "forbidden"];

function substitute(value, vars) {
  if (typeof value === "string") return value.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
  if (Array.isArray(value)) return value.map((item) => substitute(item, vars));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, substitute(item, vars)]));
  return value;
}

module.exports = { SAFETY_CLASSES, substitute };
