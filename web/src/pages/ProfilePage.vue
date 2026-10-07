<script setup>
import { ref } from "vue";

const props = defineProps({
  me: { type: Object, required: true },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  savePassword: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const currentPwd = ref("");
const nextPwd = ref("");
const againPwd = ref("");
const pwdNote = ref("");

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
  <section>
    <h2>个人中心</h2>
    <p>
      {{ me.username }} · {{ me.tier || "初级" }}<template v-if="me.expires_at"> · 有效期至 {{ me.expires_at }}</template>
    </p>
    <p>区服：<template v-if="me.region">{{ me.region }}区</template><template v-else>还没识别</template></p>
    <p class="muted">攻打号登录游戏后，从游戏链接里自动识别。目前只有 1 区支持订阅敌方。</p>
    <h3>修改密码</h3>
    <form class="stack" @submit.prevent="submitPwd().catch((e) => showErr(e.message, 'pwd'))">
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
      <span v-if="pwdNote" class="muted">{{ pwdNote }}</span>
      <span v-if="err && errAt === 'pwd'" class="err">{{ err }}</span>
    </form>
    <p class="muted">改完之后这次登录还在。别处登录的要重新登录。</p>
  </section>
</template>
