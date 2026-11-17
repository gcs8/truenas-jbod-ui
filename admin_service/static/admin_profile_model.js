// Pure profile and layout model for the admin page (#1345): bay matrices and
// their text form, ordering detection, preview rows and drive scale, and the
// slot label, slot size and list text fields. No DOM, no requests and no page
// state. admin.js binds these names from here, so base.html loads this script
// before admin.js.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AdminProfileModel = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function countSlots(layout) {
    return (Array.isArray(layout) ? layout : []).reduce(
      (total, row) => total + (Array.isArray(row) ? row.filter((value) => Number.isInteger(value)).length : 0),
      0
    );
  }

  function buildRectangularProfileLayout(rows, columns, slotCount, ordering = "row-major-bottom") {
    const safeRows = Math.max(1, Number(rows) || 1);
    const safeColumns = Math.max(1, Number(columns) || 1);
    const maxSlots = safeRows * safeColumns;
    const safeSlotCount = Math.max(1, Math.min(Number(slotCount) || maxSlots, maxSlots));
    const layout = Array.from({ length: safeRows }, () => []);
    const rowIndices = String(ordering || "").endsWith("-top")
      ? Array.from({ length: safeRows }, (_, index) => index)
      : Array.from({ length: safeRows }, (_, index) => safeRows - 1 - index);
    let nextSlot = 0;

    if (String(ordering || "").startsWith("column-major")) {
      for (let columnIndex = 0; columnIndex < safeColumns; columnIndex += 1) {
        rowIndices.forEach((rowIndex) => {
          if (nextSlot >= safeSlotCount) {
            return;
          }
          layout[rowIndex].push(nextSlot);
          nextSlot += 1;
        });
      }
      return layout;
    }

    rowIndices.forEach((rowIndex) => {
      for (let columnIndex = 0; columnIndex < safeColumns; columnIndex += 1) {
        if (nextSlot >= safeSlotCount) {
          return;
        }
        layout[rowIndex].push(nextSlot);
        nextSlot += 1;
      }
    });
    return layout;
  }

  function serializeProfileLayoutText(layout) {
    const previewRows = Array.isArray(layout) ? layout : [];
    const widestSlot = previewRows
      .flat()
      .filter((value) => Number.isInteger(value))
      .reduce((widest, value) => Math.max(widest, String(value).length), 2);
    return previewRows
      .map((row) => (Array.isArray(row) ? row : [])
        .map((slotValue) => {
          if (slotValue === null || slotValue === undefined) {
            return "--";
          }
          return String(slotValue).padStart(widestSlot, "0");
        })
        .join(" "))
      .join("\n");
  }

  function parseProfileLayoutText(value, rows, columns, slotCount) {
    const safeRows = Math.max(1, Number(rows) || 1);
    const safeColumns = Math.max(1, Number(columns) || 1);
    const safeSlotCount = Math.max(1, Number(slotCount) || 1);
    const rawLines = String(value || "").split(/\r?\n/);
    while (rawLines.length > 1 && !rawLines[rawLines.length - 1].trim()) {
      rawLines.pop();
    }

    if (rawLines.length > safeRows) {
      return { layout: null, error: `Custom matrix expects ${safeRows} row${safeRows === 1 ? "" : "s"}.` };
    }
    while (rawLines.length < safeRows) {
      rawLines.push("");
    }

    const seenSlots = new Set();
    let populatedSlots = 0;
    const normalizedRows = [];
    for (let rowIndex = 0; rowIndex < rawLines.length; rowIndex += 1) {
      const trimmedLine = rawLines[rowIndex].trim();
      const tokens = trimmedLine ? trimmedLine.split(/[,\s]+/) : [];
      if (tokens.length > safeColumns) {
        return {
          layout: null,
          error: `Row ${rowIndex + 1} cannot be wider than ${safeColumns} column${safeColumns === 1 ? "" : "s"}.`,
        };
      }
      const normalizedRow = [];
      for (const token of tokens) {
        if (/^(--|-|gap|empty|x|\.)$/i.test(token)) {
          normalizedRow.push(null);
          continue;
        }
        if (!/^\d+$/.test(token)) {
          return { layout: null, error: `Custom matrix token "${token}" is not a slot number or "--".` };
        }
        const slotNumber = Number.parseInt(token, 10);
        if (seenSlots.has(slotNumber)) {
          return { layout: null, error: `Custom matrix repeats slot ${slotNumber}.` };
        }
        seenSlots.add(slotNumber);
        normalizedRow.push(slotNumber);
        populatedSlots += 1;
      }
      normalizedRows.push(normalizedRow);
    }

    if (populatedSlots !== safeSlotCount) {
      return {
        layout: null,
        error: `Custom matrix must contain exactly ${safeSlotCount} visible slot number${safeSlotCount === 1 ? "" : "s"}.`,
      };
    }
    return { layout: normalizedRows, error: null };
  }

  function layoutsEqual(leftRows, rightRows) {
    return JSON.stringify(leftRows || []) === JSON.stringify(rightRows || []);
  }

  function detectGeneratedLayoutOrdering(layout, rows, columns, slotCount) {
    const generatedOrderings = [
      "row-major-bottom",
      "row-major-top",
      "column-major-bottom",
      "column-major-top",
    ];
    for (const ordering of generatedOrderings) {
      if (layoutsEqual(layout, buildRectangularProfileLayout(rows, columns, slotCount, ordering))) {
        return ordering;
      }
    }
    return null;
  }

  function buildProfileRows(profile) {
    if (Array.isArray(profile.slot_layout) && profile.slot_layout.length) {
      return profile.slot_layout;
    }
    const slotCount = Number(profile.slot_count) || (Number(profile.rows) || 1) * (Number(profile.columns) || 1);
    return buildRectangularProfileLayout(profile.rows, profile.columns, slotCount, "row-major-bottom");
  }

  function normalizeProfilePreviewRows(rows) {
    return Array.isArray(rows) ? rows.filter((row) => Array.isArray(row)) : [];
  }

  function normalizeProfilePreviewRowGroups(profile) {
    return (Array.isArray(profile?.row_groups) ? profile.row_groups : [])
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0);
  }

  function inferProfilePreviewDriveScale(profile, slotCount) {
    if (profile?.bay_size === "3.5" || profile?.bay_size === "2.5") {
      return profile.bay_size;
    }
    if (profile?.face_style === "top-loader" || profile?.face_style === "unifi-drive") {
      return "3.5";
    }
    if (slotCount <= 2) {
      return "2.5";
    }
    if (profile?.face_style === "front-drive" || profile?.face_style === "rear-drive") {
      return "3.5";
    }
    return "default";
  }

  function slugify(value, fallback = "item") {
    const normalized = String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return normalized || fallback;
  }

  function splitDelimitedLines(value) {
    const seen = new Set();
    return String(value || "")
      .split(/[\r\n,]+/)
      .map((item) => item.trim())
      .filter((item) => {
        if (!item || seen.has(item)) {
          return false;
        }
        seen.add(item);
        return true;
      });
  }

  function serializeDelimitedLines(values) {
    return Array.isArray(values) ? values.filter(Boolean).join("\n") : "";
  }

  function normalizeSlotLabelMap(rawMap) {
    const source = rawMap && typeof rawMap === "object" ? rawMap : {};
    const normalized = {};
    Object.entries(source).forEach(([rawKey, rawValue]) => {
      const slotNumber = Number.parseInt(rawKey, 10);
      const label = String(rawValue || "").trim();
      if (!Number.isNaN(slotNumber) && slotNumber >= 0 && label) {
        normalized[slotNumber] = label;
      }
    });
    return normalized;
  }

  const ALLOWED_M2_SIZES = new Set(["2230", "2242", "2260", "2280", "22110"]);

  function normalizeSlotSizeMap(rawMap) {
    const source = rawMap && typeof rawMap === "object" ? rawMap : {};
    const normalized = {};
    Object.entries(source).forEach(([rawKey, rawValue]) => {
      const slotNumber = Number.parseInt(rawKey, 10);
      const sizeLabel = String(rawValue || "").trim();
      if (!Number.isNaN(slotNumber) && slotNumber >= 0 && ALLOWED_M2_SIZES.has(sizeLabel)) {
        normalized[slotNumber] = sizeLabel;
      }
    });
    return normalized;
  }

  function parseSlotLabelsText(value) {
    const parsed = {};
    String(value || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const match = line.match(/^(\d+)\s*[:=]\s*(.+)$/);
        if (!match) {
          return;
        }
        const slotNumber = Number.parseInt(match[1], 10);
        const label = String(match[2] || "").trim();
        if (!Number.isNaN(slotNumber) && slotNumber >= 0 && label) {
          parsed[slotNumber] = label;
        }
      });
    return parsed;
  }

  function serializeSlotLabelsText(slotLabels) {
    return Object.entries(normalizeSlotLabelMap(slotLabels))
      .sort((left, right) => Number(left[0]) - Number(right[0]))
      .map(([slotNumber, label]) => `${slotNumber}=${label}`)
      .join("\n");
  }

  function parseSlotSizesText(value) {
    const parsed = {};
    String(value || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const match = line.match(/^(\d+)\s*[:=]\s*(\d{4,5})$/);
        if (!match) {
          return;
        }
        const slotNumber = Number.parseInt(match[1], 10);
        const sizeLabel = String(match[2] || "").trim();
        if (!Number.isNaN(slotNumber) && slotNumber >= 0 && ALLOWED_M2_SIZES.has(sizeLabel)) {
          parsed[slotNumber] = sizeLabel;
        }
      });
    return parsed;
  }

  function serializeSlotSizesText(slotSizes) {
    return Object.entries(normalizeSlotSizeMap(slotSizes))
      .sort((left, right) => Number(left[0]) - Number(right[0]))
      .map(([slotNumber, sizeLabel]) => `${slotNumber}=${sizeLabel}`)
      .join("\n");
  }

  return {
    countSlots, buildRectangularProfileLayout, serializeProfileLayoutText, parseProfileLayoutText,
    layoutsEqual, detectGeneratedLayoutOrdering, buildProfileRows, normalizeProfilePreviewRows,
    normalizeProfilePreviewRowGroups, inferProfilePreviewDriveScale, slugify, splitDelimitedLines,
    serializeDelimitedLines, normalizeSlotLabelMap, ALLOWED_M2_SIZES, normalizeSlotSizeMap,
    parseSlotLabelsText, serializeSlotLabelsText, parseSlotSizesText, serializeSlotSizesText,
  };
});
