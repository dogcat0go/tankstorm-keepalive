<script setup>
import { ref } from "vue";
import { ElSwitch } from "element-plus";
import "element-plus/es/components/switch/style/css";

const holdMin = defineModel("holdMin", { type: String, required: true });

const props = defineProps({
  me: { type: Object, required: true },
  holdNote: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  holdAll: { type: Boolean, default: false },
  holdAllBusy: { type: Boolean, default: false },
  saveHold: { type: Function, required: true },
  saveHoldAll: { type: Function, required: true },
  savePassword: { type: Function, required: true },
  logout: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const pwdOpen = ref(false);
const currentPwd = ref("");
const nextPwd = ref("");
const againPwd = ref("");
const pwdNote = ref("");

function togglePwd() {
  pwdOpen.value = !pwdOpen.value;
  pwdNote.value = "";
  props.showErr("", "pwd");
}

async function submitPwd() {
  pwdNote.value = "";
  props.showErr("", "pwd");
  if (nextPwd.value !== againPwd.value) {
    props.showErr("两次输入的新密码不一样", "pwd");
    return;
  }
  await props.savePassword(currentPwd.value, nextPwd.value);
  currentPwd.value = "";
  nextPwd.value = "";
  againPwd.value = "";
  pwdNote.value = "已修改";
}
</script>

<template>
  <div class="account-head">
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
      <button type="button" class="ghost" @click="togglePwd">改密码</button>
      <button type="button" class="ghost" @click="logout">退出</button>
    </span>
  </div>
  <form v-if="pwdOpen" class="pwd-form" @submit.prevent="submitPwd().catch((e) => showErr(e.message, 'pwd'))">
    <label>当前密码
      <input v-model="currentPwd" type="password" autocomplete="current-password" required />
    </label>
    <label>新密码
      <input v-model="nextPwd" type="password" autocomplete="new-password" required />
    </label>
    <label>再输入一次
      <input v-model="againPwd" type="password" autocomplete="new-password" required />
    </label>
    <button type="submit">修改</button>
    <button type="button" class="ghost" @click="togglePwd">取消</button>
    <span v-if="pwdNote" class="muted">{{ pwdNote }}</span>
    <span v-if="err && errAt === 'pwd'" class="err">{{ err }}</span>
  </form>
  </div>
</template>
