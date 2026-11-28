// 账号写进画面的黄色偏差里。平时看不出来，蓝色掩膜把这块偏差拉高就能读出来。

export const MARK_ALPHA = 0.016;
const MARK_FONT = 30;
const REVEAL_RADIUS = 12;

export function markTile(text, dpr) {
  const name = String(text || "").trim();
  const scale = Math.min(Math.max(Number(dpr) || 1, 1), 3);
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  ctx.font = `700 ${MARK_FONT}px sans-serif`;
  const gapX = Math.max(ctx.measureText(name || "帐").width + 48, 180);
  const gapY = 96;
  const tileW = Math.ceil(gapX);
  const tileH = Math.ceil(gapY * 2);
  canvas.width = tileW;
  canvas.height = tileH;
  if (!name) return { canvas, cssWidth: tileW / scale, cssHeight: tileH / scale };
  ctx.font = `700 ${MARK_FONT}px sans-serif`;
  ctx.fillStyle = `rgba(255, 255, 0, ${MARK_ALPHA})`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, tileW, tileH);
  ctx.clip();
  for (let ix = -1; ix <= 1; ix += 1) {
    for (let iy = -1; iy <= 1; iy += 1) {
      const row = iy * 2;
      ctx.save();
      ctx.translate(tileW / 2 + ix * tileW, gapY / 2 + (row + 0) * gapY);
      ctx.rotate(-Math.PI / 8);
      ctx.fillText(name, 0, 0);
      ctx.restore();
      ctx.save();
      ctx.translate(ix * tileW, gapY * 1.5 + row * gapY);
      ctx.rotate(-Math.PI / 8);
      ctx.fillText(name, 0, 0);
      ctx.restore();
    }
  }
  ctx.restore();
  return { canvas, cssWidth: tileW / scale, cssHeight: tileH / scale };
}

export function paintMark(ctx, text, width, height) {
  const name = String(text || "").trim();
  if (!name || width < 1 || height < 1) return;
  ctx.save();
  ctx.font = `700 ${MARK_FONT}px sans-serif`;
  ctx.fillStyle = `rgba(255, 255, 0, ${MARK_ALPHA})`;
  ctx.textBaseline = "middle";
  ctx.translate(width / 2, height / 2);
  ctx.rotate(-Math.PI / 8);
  const gapX = Math.max(ctx.measureText(name).width + 48, 180);
  const gapY = 96;
  const span = Math.hypot(width, height);
  let row = 0;
  for (let y = -span; y <= span; y += gapY, row += 1) {
    const shift = row % 2 ? gapX / 2 : 0;
    for (let x = -span; x <= span; x += gapX) {
      ctx.fillText(name, x + shift, y);
    }
  }
  ctx.restore();
}

function boxBlur(src, w, h, radius) {
  const tmp = new Float32Array(src.length);
  const dst = new Float32Array(src.length);
  const win = radius * 2 + 1;
  for (let y = 0; y < h; y++) {
    const row = y * w;
    let sum = 0;
    for (let k = -radius; k <= radius; k++) sum += src[row + Math.max(0, k)];
    for (let x = 0; x < w; x++) {
      tmp[row + x] = sum / win;
      sum -= src[row + Math.max(0, x - radius)];
      sum += src[row + Math.min(w - 1, x + radius + 1)];
    }
  }
  for (let x = 0; x < w; x++) {
    let sum = 0;
    for (let k = -radius; k <= radius; k++) sum += tmp[Math.max(0, k) * w + x];
    for (let y = 0; y < h; y++) {
      dst[y * w + x] = sum / win;
      sum -= tmp[Math.max(0, y - radius) * w + x];
      sum += tmp[Math.min(h - 1, y + radius + 1) * w + x];
    }
  }
  return dst;
}

export function revealYellow(rgba, w, h) {
  const n = w * h;
  const yellow = new Float32Array(n);
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    yellow[i] = (rgba[p] + rgba[p + 1]) * 0.5 - rgba[p + 2];
  }
  const radius = Math.max(6, Math.min(REVEAL_RADIUS, Math.round(Math.min(w, h) / 40)));
  const base = boxBlur(yellow, w, h, radius);
  const signal = new Float32Array(n);
  const sample = [];
  for (let i = 0; i < n; i++) {
    signal[i] = yellow[i] - base[i];
    if ((i & 31) === 0) sample.push(signal[i]);
  }
  sample.sort((a, b) => a - b);
  const mid = sample[sample.length >> 1] || 0;
  const high = sample[Math.min(sample.length - 1, Math.floor(sample.length * 0.985))] || mid;
  const span = Math.max(high - mid, 1.2);
  const out = new Uint8ClampedArray(rgba.length);
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    const lift = Math.max(0, Math.min(1.4, (signal[i] - mid) / span));
    const blue = Math.max(0, Math.min(255, 255 - lift * 220));
    out[p] = blue * 0.12;
    out[p + 1] = blue * 0.28;
    out[p + 2] = blue;
    out[p + 3] = 255;
  }
  return out;
}
