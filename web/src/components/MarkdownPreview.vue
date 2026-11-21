<script setup>
import { MdPreview } from "md-editor-v3";
import "md-editor-v3/lib/preview.css";
import "../markdown.js";

defineProps({
  modelValue: { type: String, default: "" },
});
</script>

<template>
  <MdPreview :modelValue="modelValue || ''" previewTheme="github" />
</template>
