"use strict";

// Test-only preload (NODE_OPTIONS=--require ...) for tests/js/ui_crawl_r4.test.js
// (#1117): while the file named by UI_CRAWL_TEST_CLOCK_FLAG exists, the wall
// clock (Date) of this process reads one hour earlier, which is what a
// backward wall-clock step looks like to the code. The monotonic clock
// (process.hrtime) is not touched. Never loaded outside tests.

const fs = require("node:fs");

const flag = process.env.UI_CRAWL_TEST_CLOCK_FLAG;
if (flag) {
  const RealDate = Date;
  const offset = () => {
    try {
      return fs.existsSync(flag) ? -3_600_000 : 0;
    } catch {
      return 0;
    }
  };
  class ShiftedDate extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(RealDate.now() + offset());
      else super(...args);
    }

    static now() {
      return RealDate.now() + offset();
    }
  }
  globalThis.Date = ShiftedDate;
}
