<script setup>
import { onMounted, onUnmounted, ref, watch } from "vue";
import { markTile } from "../hidden-mark.js";

const props = defineProps({
  text: { type: String, default: "" },
});

const el = ref(null);
let frame = 0;
let observer = null;
let lock = false;

function draw() {
  const node = el.value;
  if (!node || lock) return;
  lock = true;
  const name = String(props.text || "").trim();
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const tile = markTile(name, dpr);
  node.style.backgroundImage = name ? `url("${tile.canvas.toDataURL()}")` : "none";
  node.style.backgroundSize = `${tile.cssWidth}px ${tile.cssHeight}px`;
  node.style.height = "0px";
  const root = document.documentElement;
  const body = document.body;
  const height = Math.max(
    window.innerHeight || 0,
    root ? root.scrollHeight : 0,
    body ? body.scrollHeight : 0,
  );
  node.style.height = height + "px";
  requestAnimationFrame(() => { lock = false; });
}

function schedule() {
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(draw);
}

onMounted(() => {
  if (document.body && getComputedStyle(document.body).position === "static") {
    document.body.style.position = "relative";
  }
  schedule();
  window.addEventListener("resize", schedule);
  if (window.ResizeObserver && document.documentElement) {
    observer = new ResizeObserver(schedule);
    observer.observe(document.documentElement);
  }
});
onUnmounted(() => {
  cancelAnimationFrame(frame);
  window.removeEventListener("resize", schedule);
  if (observer) observer.disconnect();
});
watch(() => props.text, schedule);
</script>

<template>
  <div ref="el" class="hidden-mark" aria-hidden="true"></div>
</template>

<style>
.hidden-mark {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  z-index: 8000;
  pointer-events: none;
  background-repeat: repeat;
}
</style>
