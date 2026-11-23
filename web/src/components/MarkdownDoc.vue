<script setup>
import { defineAsyncComponent } from "vue";

defineProps({
  modelValue: { type: String, default: "" },
  readonly: { type: Boolean, default: false },
  maxLength: { type: Number, default: 2000 },
  height: { type: String, default: "420px" },
  placeholder: { type: String, default: "支持 Markdown。图片地址单独一行，预览里会显示成图片。" },
});
const emit = defineEmits(["update:modelValue"]);

const Preview = defineAsyncComponent(() => import("./MarkdownPreview.vue"));
const Editor = defineAsyncComponent(() => import("./MarkdownEdit.vue"));
</script>

<template>
  <Preview v-if="readonly" :model-value="modelValue" />
  <Editor
    v-else
    :model-value="modelValue"
    :max-length="maxLength"
    :height="height"
    :placeholder="placeholder"
    @update:model-value="emit('update:modelValue', $event)"
  />
</template>
