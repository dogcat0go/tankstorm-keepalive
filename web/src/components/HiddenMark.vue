<script setup>
import { onMounted, onUnmounted, ref, watch } from "vue";
import { paintMark } from "../hidden-mark.js";

const props = defineProps({
  text: { type: String, default: "" },
});

const el = ref(null);
let frame = 0;

function draw() {
  const canvas = el.value;
  const name = String(props.text || "").trim();
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const width = Math.max(1, Math.round(window.innerWidth * dpr));
  const height = Math.max(1, Math.round(window.innerHeight * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, width, height);
  paintMark(ctx, name, width, height);
}

function schedule() {
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(draw);
}

onMounted(() => {
  schedule();
  window.addEventListener("resize", schedule);
});
onUnmounted(() => {
  cancelAnimationFrame(frame);
  window.removeEventListener("resize", schedule);
});
watch(() => props.text, schedule);
</script>

<template>
  <canvas ref="el" class="hidden-mark" aria-hidden="true"></canvas>
</template>

<style>
.hidden-mark {
  position: fixed;
  inset: 0;
  z-index: 8000;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
</style>
