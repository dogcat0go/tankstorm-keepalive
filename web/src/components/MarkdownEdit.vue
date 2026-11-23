<script setup>
import { MdEditor } from "md-editor-v3";
import "md-editor-v3/lib/style.css";
import "../markdown.js";

defineProps({
  modelValue: { type: String, default: "" },
  maxLength: { type: Number, default: 2000 },
  height: { type: String, default: "420px" },
  placeholder: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue"]);
</script>

<template>
  <MdEditor
    :modelValue="modelValue"
    language="zh-CN"
    preview
    previewTheme="github"
    :maxLength="maxLength"
    :noUploadImg="true"
    :noMermaid="true"
    :noKatex="true"
    :noEcharts="true"
    :toolbarsExclude="['mermaid', 'katex', 'github', 'save', 'htmlPreview', 'catalog', 'prettier']"
    :placeholder="placeholder"
    :style="{ height }"
    @update:modelValue="emit('update:modelValue', $event)"
  />
</template>
