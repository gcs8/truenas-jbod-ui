"use strict";

// Minimal PNG decode/encode/crop/diff for crawler tiles (#ui-coverage).
// Chromium screenshots are 8-bit RGB or RGBA, non-interlaced; that is all
// this supports. No dependencies beyond node:zlib.

const zlib = require("node:zlib");

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buffer) {
  let c = 0xffffffff;
  for (let i = 0; i < buffer.length; i += 1) c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function size(buffer) {
  if (!SIGNATURE.equals(buffer.subarray(0, 8))) throw new Error("not a PNG");
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  return pb <= pc ? b : c;
}

// Returns { width, height, data } with data as RGBA bytes.
function decode(buffer) {
  if (!SIGNATURE.equals(buffer.subarray(0, 8))) throw new Error("not a PNG");
  let offset = 8;
  let header = null;
  const idat = [];
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString("ascii", offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      header = {
        width: data.readUInt32BE(0), height: data.readUInt32BE(4), depth: data[8], color: data[9], interlace: data[12],
      };
    } else if (type === "IDAT") {
      idat.push(data);
    } else if (type === "IEND") {
      break;
    }
    offset += 12 + length;
  }
  if (!header) throw new Error("PNG without IHDR");
  if (header.depth !== 8 || header.interlace !== 0 || ![2, 6].includes(header.color)) {
    throw new Error(`unsupported PNG (depth ${header.depth}, color ${header.color}, interlace ${header.interlace})`);
  }
  const channels = header.color === 6 ? 4 : 3;
  const stride = header.width * channels;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const out = Buffer.alloc(header.width * header.height * 4);
  let previous = Buffer.alloc(stride);
  let current = Buffer.alloc(stride);
  for (let y = 0; y < header.height; y += 1) {
    const filter = raw[y * (stride + 1)];
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x += 1) {
      const a = x >= channels ? current[x - channels] : 0;
      const b = previous[x];
      const c = x >= channels ? previous[x - channels] : 0;
      let value = line[x];
      if (filter === 1) value += a;
      else if (filter === 2) value += b;
      else if (filter === 3) value += (a + b) >> 1;
      else if (filter === 4) value += paeth(a, b, c);
      current[x] = value & 0xff;
    }
    for (let x = 0; x < header.width; x += 1) {
      const target = (y * header.width + x) * 4;
      out[target] = current[x * channels];
      out[target + 1] = current[x * channels + 1];
      out[target + 2] = current[x * channels + 2];
      out[target + 3] = channels === 4 ? current[x * channels + 3] : 255;
    }
    [previous, current] = [current, previous];
  }
  return { width: header.width, height: header.height, data: out };
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

// Encode RGBA as RGB (screenshots are opaque), filter "up" per row.
function encode({ width, height, data }) {
  const stride = width * 3;
  const raw = Buffer.alloc((stride + 1) * height);
  let previous = Buffer.alloc(stride);
  const row = Buffer.alloc(stride);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const source = (y * width + x) * 4;
      row[x * 3] = data[source];
      row[x * 3 + 1] = data[source + 1];
      row[x * 3 + 2] = data[source + 2];
    }
    const base = y * (stride + 1);
    raw[base] = 2;
    for (let x = 0; x < stride; x += 1) raw[base + 1 + x] = (row[x] - previous[x]) & 0xff;
    previous = Buffer.from(row);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([SIGNATURE, chunk("IHDR", header), chunk("IDAT", zlib.deflateSync(raw, { level: 6 })), chunk("IEND", Buffer.alloc(0))]);
}

function crop(image, x, y, width, height) {
  const w = Math.max(0, Math.min(width, image.width - x));
  const h = Math.max(0, Math.min(height, image.height - y));
  const data = Buffer.alloc(w * h * 4);
  for (let row = 0; row < h; row += 1) {
    const start = ((y + row) * image.width + x) * 4;
    image.data.copy(data, row * w * 4, start, start + w * 4);
  }
  return { width: w, height: h, data };
}

// Grid of tiles at native resolution. Returns [{x, y, width, height, png}].
function tiles(image, tileWidth = 1280, tileHeight = 720) {
  const out = [];
  for (let y = 0; y < image.height; y += tileHeight) {
    for (let x = 0; x < image.width; x += tileWidth) {
      const part = crop(image, x, y, tileWidth, tileHeight);
      out.push({ x, y, width: part.width, height: part.height, png: encode(part) });
    }
  }
  return out;
}

// Fraction of pixels whose max channel difference exceeds `threshold`.
// Different sizes compare the common area and report the size change.
function diff(a, b, threshold = 24) {
  const width = Math.min(a.width, b.width);
  const height = Math.min(a.height, b.height);
  let changed = 0;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const i = (y * a.width + x) * 4;
      const j = (y * b.width + x) * 4;
      const delta = Math.max(Math.abs(a.data[i] - b.data[j]), Math.abs(a.data[i + 1] - b.data[j + 1]), Math.abs(a.data[i + 2] - b.data[j + 2]));
      if (delta > threshold) changed += 1;
    }
  }
  return {
    changedFraction: width * height ? changed / (width * height) : 1,
    sizeChanged: a.width !== b.width || a.height !== b.height,
    before: [b.width, b.height],
    after: [a.width, a.height],
  };
}

module.exports = { decode, encode, crop, tiles, diff, size, crc32 };
