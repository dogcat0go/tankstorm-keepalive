<script setup>
const holdMin = defineModel("holdMin", { type: String, required: true });

defineProps({
  me: { type: Object, required: true },
  holdNote: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  saveHold: { type: Function, required: true },
  logout: { type: Function, required: true },
  showErr: { type: Function, required: true },
});
</script>

<template>
  <div class="lead account-bar">
    <span class="account-id">
      {{ me.username }} · {{ me.tier || "初级" }}<template v-if="me.expires_at"> · 有效期至 {{ me.expires_at }}</template>
    </span>
    <form v-if="me.remote_attack" class="hold-set" @submit.prevent="saveHold().catch((e) => showErr(e.message, 'hold'))">
      <label class="choice">挂机时间
        <input v-model="holdMin" class="mins" inputmode="numeric" required />
        分钟
      </label>
      <button type="submit">保存</button>
      <span v-if="holdNote" class="muted">{{ holdNote }}</span>
      <span v-if="err && errAt === 'hold'" class="err">{{ err }}</span>
    </form>
    <span class="account-actions">
      <a v-if="me.admin" class="ghost" href="/admin">管理</a>
      <button type="button" class="ghost" @click="logout">退出</button>
    </span>
  </div>
</template>
