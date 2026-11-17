// Pure SMART value formatters for the main page: the detail rows, the SMART
// tooltip and the heat map read their text from here. No DOM, no state and
// no requests. app.js takes its names from here, so base.html loads this
// script first and the saved HTML copy inlines it before app.js.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AppSmartFormatters = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const ANNUALIZED_MIN_POWER_ON_HOURS = 24 * 30;

  // A SMART field listed in the summary's unread_fields was reported in a form
  // that could not be read: it is "not read", neither absent ("n/a") nor zero.
  // The summary API already takes an inventory reading of the same field when
  // it has one, so the page does not fill a listed field from the slot.
  function smartFieldNotRead(smartEntry, ...fields) {
    const unread = smartEntry?.data?.unread_fields;
    return Array.isArray(unread) && fields.some((field) => unread.includes(field));
  }

  // `formatted` unless it is a no-value text and one of `fields` was not read.
  function smartNotReadOr(smartEntry, fields, formatted) {
    const noValue = ["n/a", "Unavailable", "Loading..."].includes(formatted);
    return noValue && smartFieldNotRead(smartEntry, ...fields) ? "not read" : formatted;
  }

  function formatTemperatureValue(slot, smartEntry) {
    const notRead = smartFieldNotRead(smartEntry, "temperature_c");
    const temperature = smartEntry?.data?.temperature_c ?? (notRead ? null : slot.temperature_c);
    if (Number.isInteger(temperature)) {
      return `${temperature} C`;
    }
    if (notRead) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatLastSmartTestValue(slot, smartEntry) {
    const data = smartEntry?.data;
    if (smartFieldNotRead(smartEntry, "last_test_type", "last_test_status")) {
      return "not read";
    }
    const testType = data?.last_test_type || slot.last_smart_test_type;
    const testStatus = data?.last_test_status || slot.last_smart_test_status;
    const ageHours = data?.last_test_age_hours;
    const lifetimeNotRead = smartFieldNotRead(smartEntry, "last_test_lifetime_hours", "last_test_age_hours");
    const lifetimeHours = data?.last_test_lifetime_hours ?? (lifetimeNotRead ? null : slot.last_smart_test_lifetime_hours);

    if (testType && testStatus) {
      let suffix = "";
      if (Number.isInteger(ageHours)) {
        suffix = ` (${ageHours}h ago)`;
      } else if (Number.isInteger(lifetimeHours)) {
        suffix = ` (@ ${lifetimeHours}h)`;
      } else if (lifetimeNotRead) {
        suffix = " (time not read)";
      }
      return `${testType}: ${testStatus}${suffix}`;
    }

    if (smartEntry?.loading) {
      return "Loading...";
    }

    return "n/a";
  }

  function formatPowerOnValue(smartEntry) {
    const hours = smartEntry?.data?.power_on_hours;
    const days = smartEntry?.data?.power_on_days;
    if (Number.isInteger(hours)) {
      return Number.isInteger(days) ? `${hours} hr (${days} d)` : `${hours} hr`;
    }
    // Reported in a form that could not be read: neither absent nor zero.
    const unread = smartEntry?.data?.unread_fields;
    if (Array.isArray(unread) && (unread.includes("power_on_hours") || unread.includes("power_on_days"))) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return smartEntry?.data?.message ? "Unavailable" : "n/a";
  }

  function formatPowerCycleValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.power_cycle_count, smartEntry, "power_cycle_count");
  }

  function formatPowerOnResetsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.power_on_resets, smartEntry, "power_on_resets");
  }

  function formatSectorSizeValue(slot, smartEntry) {
    const sizes = [["Logical", "logical_block_size"], ["Physical", "physical_block_size"]].map(([label, field]) => {
      const notRead = smartFieldNotRead(smartEntry, field);
      return { label, notRead, value: smartEntry?.data?.[field] ?? (notRead ? null : slot?.[field]) };
    });
    if (sizes.every(({ value }) => Number.isInteger(value))) {
      return sizes.map(({ label, value }) => `${label} ${value} B`).join(" / ");
    }
    if (sizes.some(({ notRead }) => notRead)) {
      return sizes.map(({ label, value, notRead }) => (Number.isInteger(value) ? `${label} ${value} B` : notRead ? `${label} not read` : null))
        .filter(Boolean).join(" / ");
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return smartEntry?.data?.message ? "Unavailable" : "n/a";
  }

  function formatRotationValue(smartEntry) {
    const rpm = smartEntry?.data?.rotation_rate_rpm;
    if (rpm === 0) {
      return "SSD";
    }
    if (Number.isInteger(rpm)) {
      return `${rpm} rpm`;
    }
    if (smartFieldNotRead(smartEntry, "rotation_rate_rpm")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatFormFactorValue(smartEntry) {
    const formFactor = smartEntry?.data?.form_factor;
    if (formFactor) {
      return formFactor;
    }
    if (smartFieldNotRead(smartEntry, "form_factor")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatBooleanFlagValue(value, smartEntry, trueLabel = "Enabled", falseLabel = "Disabled") {
    if (value === true) {
      return trueLabel;
    }
    if (value === false) {
      return falseLabel;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatCacheFlagValue(value, smartEntry) {
    return formatBooleanFlagValue(value, smartEntry);
  }

  function formatReadCacheValue(smartEntry) {
    return smartNotReadOr(smartEntry, ["read_cache_enabled"],
      formatCacheFlagValue(smartEntry?.data?.read_cache_enabled, smartEntry));
  }

  function formatWritebackCacheValue(smartEntry) {
    return smartNotReadOr(smartEntry, ["writeback_cache_enabled"],
      formatCacheFlagValue(smartEntry?.data?.writeback_cache_enabled, smartEntry));
  }

  function formatTrimSupportedValue(smartEntry) {
    return formatBooleanFlagValue(smartEntry?.data?.trim_supported, smartEntry, "Supported", "Not Supported");
  }

  function formatHexIdentifier(value) {
    if (value === null || value === undefined || value === "") {
      return null;
    }
    const text = String(value).trim().split(/error:/i, 1)[0].trim();
    if (!text) {
      return null;
    }
    if (/^0x[0-9a-f]+$/i.test(text)) {
      return text.toLowerCase();
    }
    if (/^[0-9a-f]+$/i.test(text)) {
      return `0x${text.toLowerCase()}`;
    }
    return null;
  }

  function formatTransportValue(smartEntry) {
    const transport = smartEntry?.data?.transport_protocol;
    if (transport) {
      return transport;
    }
    if (smartFieldNotRead(smartEntry, "transport_protocol")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatLogicalUnitIdValue(slot, smartEntry) {
    if (smartFieldNotRead(smartEntry, "logical_unit_id")) {
      return "not read";
    }
    const logicalUnitId = smartEntry?.data?.logical_unit_id || slot?.logical_unit_id || slot?.multipath?.lunid;
    const formatted = formatHexIdentifier(logicalUnitId);
    if (formatted) {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatSasAddressValue(slot, smartEntry) {
    if (smartFieldNotRead(smartEntry, "sas_address")) {
      return "not read";
    }
    const sasAddress = smartEntry?.data?.sas_address || slot?.sas_address;
    const formatted = formatHexIdentifier(sasAddress);
    if (formatted) {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatMetricBytes(value) {
    if (!Number.isFinite(value) || value <= 0) {
      return null;
    }
    const suffixes = ["B", "KB", "MB", "GB", "TB", "PB", "EB"];
    let size = value;
    let suffixIndex = 0;
    while (size >= 1000 && suffixIndex < suffixes.length - 1) {
      size /= 1000;
      suffixIndex += 1;
    }
    const fractionDigits = size >= 100 || suffixIndex === 0 ? 0 : size >= 10 ? 1 : 2;
    return `${size.toFixed(fractionDigits)} ${suffixes[suffixIndex]}`;
  }

  function formatOptionalCount(value, smartEntry, field = null) {
    if (Number.isInteger(value)) {
      return value.toLocaleString();
    }
    if (field && smartFieldNotRead(smartEntry, field)) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatEnduranceValue(smartEntry) {
    const remaining = smartEntry?.data?.endurance_remaining_percent;
    const used = smartEntry?.data?.endurance_used_percent;
    if (Number.isInteger(remaining) && Number.isInteger(used)) {
      return `${remaining}% remaining (${used}% used)`;
    }
    if (Number.isInteger(remaining)) {
      return `${remaining}% remaining`;
    }
    if (Number.isInteger(used)) {
      return `${used}% used`;
    }
    if (smartFieldNotRead(smartEntry, "endurance_used_percent", "endurance_remaining_percent")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatAvailableSpareValue(smartEntry) {
    const spare = smartEntry?.data?.available_spare_percent;
    const threshold = smartEntry?.data?.available_spare_threshold_percent;
    if (Number.isInteger(spare) && Number.isInteger(threshold)) {
      return `${spare}% (threshold ${threshold}%)`;
    }
    if (Number.isInteger(spare)) {
      return smartFieldNotRead(smartEntry, "available_spare_threshold_percent") ? `${spare}% (threshold not read)` : `${spare}%`;
    }
    if (smartFieldNotRead(smartEntry, "available_spare_percent", "available_spare_threshold_percent")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatBytesValue(value, smartEntry) {
    const formatted = formatMetricBytes(value);
    if (formatted) {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatBytesWrittenValue(smartEntry) {
    return smartNotReadOr(smartEntry, ["bytes_written"], formatBytesValue(smartEntry?.data?.bytes_written, smartEntry));
  }

  function formatBytesReadValue(smartEntry) {
    return smartNotReadOr(smartEntry, ["bytes_read"], formatBytesValue(smartEntry?.data?.bytes_read, smartEntry));
  }

  function computeAnnualizedBytes(totalBytes, powerOnHours, minimumHours = ANNUALIZED_MIN_POWER_ON_HOURS) {
    const bytes = nullableMetricNumber(totalBytes);
    const hours = nullableMetricNumber(powerOnHours);
    if (!Number.isFinite(bytes) || !Number.isFinite(hours) || hours < minimumHours || hours <= 0) {
      return null;
    }
    return Math.round(bytes * 24 * 365 / hours);
  }

  function formatAnnualizedReadValue(smartEntry) {
    const annualizedRead = smartEntry?.data?.annualized_bytes_read
      ?? computeAnnualizedBytes(
        smartEntry?.data?.bytes_read,
        smartEntry?.data?.power_on_hours
      );
    const formatted = formatMetricBytes(annualizedRead);
    if (formatted) {
      return `${formatted}/yr`;
    }
    if (smartFieldNotRead(smartEntry, "annualized_bytes_read")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatAnnualizedWriteValue(smartEntry) {
    const formatted = formatMetricBytes(smartEntry?.data?.annualized_bytes_written);
    if (formatted) {
      return `${formatted}/yr`;
    }
    if (smartFieldNotRead(smartEntry, "annualized_bytes_written")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatEstimatedRemainingWriteValue(smartEntry) {
    return smartNotReadOr(smartEntry, ["estimated_remaining_bytes_written"],
      formatBytesValue(smartEntry?.data?.estimated_remaining_bytes_written, smartEntry));
  }

  function formatReadCommandsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.read_commands, smartEntry, "read_commands");
  }

  function formatWriteCommandsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.write_commands, smartEntry, "write_commands");
  }

  function formatReadErrorCountValue(smartEntry) {
    if (smartEntry?.data?.read_error_count !== undefined && smartEntry?.data?.read_error_count !== null) {
      return formatOptionalCount(smartEntry?.data?.read_error_count, smartEntry);
    }
    return formatOptionalCount(smartEntry?.data?.uncorrected_read_errors, smartEntry);
  }

  function formatWriteErrorCountValue(smartEntry) {
    if (smartEntry?.data?.write_error_count !== undefined && smartEntry?.data?.write_error_count !== null) {
      return formatOptionalCount(smartEntry?.data?.write_error_count, smartEntry);
    }
    return formatOptionalCount(smartEntry?.data?.uncorrected_write_errors, smartEntry);
  }

  function readErrorCountLabel(smartEntry) {
    return Number.isInteger(smartEntry?.data?.read_error_count) ? "Read Error Count" : "Uncorrected Read";
  }

  function writeErrorCountLabel(smartEntry) {
    return Number.isInteger(smartEntry?.data?.write_error_count) ? "Write Error Count" : "Uncorrected Write";
  }

  function formatMediaErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.media_errors, smartEntry, "media_errors");
  }

  function formatPredictiveErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.predictive_errors, smartEntry);
  }

  function formatNonMediumErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.non_medium_errors, smartEntry);
  }

  function formatUnsafeShutdownsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.unsafe_shutdowns, smartEntry, "unsafe_shutdowns");
  }

  function formatHardwareResetsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.hardware_resets, smartEntry, "hardware_resets");
  }

  function formatInterfaceCrcErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.interface_crc_errors, smartEntry, "interface_crc_errors");
  }

  function formatSmartHealthStatusValue(smartEntry) {
    const status = smartEntry?.data?.smart_health_status;
    if (status) {
      return status;
    }
    if (smartFieldNotRead(smartEntry, "smart_health_status")) {
      return "not read";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function nullableMetricNumber(value) {
    // Missing observations and non-numeric types must not become measured zero.
    if (typeof value !== "number" && typeof value !== "string") {
      return null;
    }
    if (typeof value === "string" && value.trim() === "") {
      return null;
    }
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  return {
    ANNUALIZED_MIN_POWER_ON_HOURS, smartFieldNotRead, smartNotReadOr, formatTemperatureValue,
    formatLastSmartTestValue, formatPowerOnValue, formatPowerCycleValue, formatPowerOnResetsValue,
    formatSectorSizeValue, formatRotationValue, formatFormFactorValue, formatBooleanFlagValue, formatCacheFlagValue,
    formatReadCacheValue, formatWritebackCacheValue, formatTrimSupportedValue, formatHexIdentifier,
    formatTransportValue, formatLogicalUnitIdValue, formatSasAddressValue, formatMetricBytes, formatOptionalCount,
    formatEnduranceValue, formatAvailableSpareValue, formatBytesValue, formatBytesWrittenValue, formatBytesReadValue,
    computeAnnualizedBytes, formatAnnualizedReadValue, formatAnnualizedWriteValue, formatEstimatedRemainingWriteValue,
    formatReadCommandsValue, formatWriteCommandsValue, formatReadErrorCountValue, formatWriteErrorCountValue,
    readErrorCountLabel, writeErrorCountLabel, formatMediaErrorsValue, formatPredictiveErrorsValue,
    formatNonMediumErrorsValue, formatUnsafeShutdownsValue, formatHardwareResetsValue, formatInterfaceCrcErrorsValue,
    formatSmartHealthStatusValue, nullableMetricNumber,
  };
});
