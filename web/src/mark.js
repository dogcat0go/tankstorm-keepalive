import "./base.css";
import { paintMark, revealYellow } from "./hidden-mark.js";

const fileInput = document.querySelector("#file");
const note = document.querySelector("#note");
const before = document.querySelector("#before");
const after = document.querySelector("#after");
const MAX_EDGE = 1600;

function fit(canvas, width, height) {
  canvas.width = width;
  canvas.height = height;
}

function showPixels(rgba, width, height) {
  fit(after, width, height);
  after.getContext("2d").putImageData(new ImageData(rgba, width, height), 0, 0);
}

function revealCanvas(source) {
  const data = source.getContext("2d").getImageData(0, 0, source.width, source.height);
  showPixels(revealYellow(data.data, source.width, source.height), source.width, source.height);
}

function drawImage(img) {
  const scale = Math.min(1, MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
  const width = Math.max(1, Math.round(img.naturalWidth * scale));
  const height = Math.max(1, Math.round(img.naturalHeight * scale));
  fit(before, width, height);
  before.getContext("2d").drawImage(img, 0, 0, width, height);
  revealCanvas(before);
  note.textContent = "第一张是截图，第二张是掩膜之后。";
}

function sample() {
  const width = 1170;
  const height = 1280;
  fit(before, width, height);
  drawSample(before.getContext("2d"), width, height, true);
  revealCanvas(before);
}

function drawSample(ctx, width, height, marked) {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#f6f6f4";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#fff";
  ctx.fillRect(48, 150, width - 96, 980);
  ctx.fillStyle = "#1a1a1a";
  ctx.font = "700 48px sans-serif";
  ctx.fillText("订阅这座城里的人", 96, 280);
  ctx.fillStyle = "#777";
  ctx.font = "400 36px sans-serif";
  ctx.fillText("城里现在能看到的名单在下面。", 96, 360);
  ctx.fillStyle = "#d4e9ff";
  ctx.fillRect(96, 460, 280, 96);
  ctx.fillStyle = "#16375f";
  ctx.font = "700 40px sans-serif";
  ctx.fillText("登录", 180, 520);
  if (marked) paintMark(ctx, "示例账号", width, height);
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  const img = new Image();
  img.onload = () => {
    drawImage(img);
    URL.revokeObjectURL(img.src);
  };
  img.onerror = () => {
    note.textContent = "这张图打不开。";
  };
  img.src = URL.createObjectURL(file);
});

sample();
