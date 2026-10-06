<script setup>
import { ElSwitch } from "element-plus";
import "element-plus/es/components/switch/style/css";

const holdMin = defineModel("holdMin", { type: String, required: true });

defineProps({
  me: { type: Object, required: true },
  holdNote: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  holdAll: { type: Boolean, default: false },
  holdAllBusy: { type: Boolean, default: false },
  saveHold: { type: Function, required: true },
  saveHoldAll: { type: Function, required: true },
  openProfile: { type: Function, required: true },
  profileOn: { type: Boolean, default: false },
  openQq: { type: Function, required: true },
  qqOn: { type: Boolean, default: false },
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
      <label class="choice">全天候挂机</label>
      <el-switch
        class="daily-switch"
        size="large"
        inline-prompt
        active-text="开"
        inactive-text="关"
        :model-value="holdAll"
        :loading="holdAllBusy"
        @change="saveHoldAll"
      />
      <span v-if="holdNote" class="muted">{{ holdNote }}</span>
      <span v-if="err && errAt === 'hold'" class="err">{{ err }}</span>
    </form>
    <span class="account-actions">
      <a v-if="me.admin" class="ghost" href="/admin">管理</a>
      <nav class="account-nav" aria-label="账号">
        <button type="button" class="ghost" :class="{ here: profileOn }" @click="openProfile">个人中心</button>
        <button type="button" class="ghost" :class="{ here: qqOn }" @click="openQq">订阅QQ</button>
      </nav>
      <button type="button" class="ghost" @click="logout">退出</button>
    </span>
  </div>
</template>
