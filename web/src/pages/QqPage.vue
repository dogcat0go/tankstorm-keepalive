<script setup>
const qqTarget = defineModel("qqTarget", { type: String, required: true });

defineProps({
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  note: { type: String, default: "" },
  savePush: { type: Function, required: true },
  showErr: { type: Function, required: true },
});
</script>

<template>
  <section>
    <h2>订阅 QQ</h2>
    <p v-if="err && errAt === 'qq'" class="err">{{ err }}</p>
    <form class="stack" @submit.prevent="savePush().catch((e) => showErr(e.message))">
      <label>接收 QQ
        <input v-model="qqTarget" inputmode="numeric" autocomplete="off" placeholder="你的 QQ 号" />
      </label>
      <button type="submit">保存</button>
      <span class="muted">{{ note }}</span>
    </form>
    <p class="muted">私聊发到这个 QQ。机器人地址和 Token 在服务器配置里，页面上不填写。</p>
  </section>
</template>
