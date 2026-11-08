<script setup>
import { computed, onMounted, ref, watch } from "vue";

const props = defineProps({
  me: { type: Object, required: true },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  savePassword: { type: Function, required: true },
  loadFailLibrary: { type: Function, required: true },
  resetFailLibrary: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const currentPwd = ref("");
const nextPwd = ref("");
const againPwd = ref("");
const pwdNote = ref("");
const failCount = ref(0);
const failQq = ref("");
const failItems = ref([]);
const failUid = ref("");
const failName = ref("");
const failPage = ref(1);
const failNote = ref("");
const failBusy = ref(false);
const failReady = ref(false);
const FAIL_PAGE = 20;

const failQuery = computed(() => failName.value.trim().toLowerCase());
const failShown = computed(() => {
  const q = failQuery.value;
  if (!q) return failItems.value;
  return failItems.value.filter((it) => String(it.name || "").toLowerCase().includes(q));
});
const failView = computed(() => {
  const rows = failShown.value;
  const pages = Math.max(1, Math.ceil(rows.length / FAIL_PAGE));
  const page = Math.min(Math.max(1, failPage.value), pages);
  const start = (page - 1) * FAIL_PAGE;
  return {
    page,
    pages,
    rows: rows.slice(start, start + FAIL_PAGE),
    from: rows.length ? start + 1 : 0,
    to: Math.min(start + FAIL_PAGE, rows.length),
  };
});

watch(failQuery, () => {
  failPage.value = 1;
});

async function loadFail() {
  const data = await props.loadFailLibrary();
  failCount.value = Number(data.count) || 0;
  failQq.value = data.qq || "";
  failItems.value = data.items || [];
  failReady.value = true;
}

function failLabel(it) {
  const name = String((it && it.name) || "").trim();
  const uid = String((it && it.uid) || failUid.value || "").trim();
  return name ? `${name} ${uid}` : uid;
}

async function resetOne(it) {
  failNote.value = "";
  props.showErr("", "fail");
  if (!failQq.value) {
    props.showErr("还没有攻打 QQ，没有战败库", "fail");
    return;
  }
  const uid = String((it && it.uid) || failUid.value || "").trim();
  if (!/^\d{1,32}$/.test(uid)) {
    props.showErr("UID 要是数字", "fail");
    return;
  }
  const label = failLabel(it && it.uid ? it : { uid });
  if (!window.confirm(`从战败库去掉 ${label}？这个人可以再打。`)) return;
  failBusy.value = true;
  try {
    const data = await props.resetFailLibrary(uid);
    failNote.value = data.removed ? `已去掉 ${label}` : "战败库里没有这个人";
    failUid.value = "";
    await loadFail();
  } finally {
    failBusy.value = false;
  }
}

async function resetFail() {
  failNote.value = "";
  props.showErr("", "fail");
  if (!failQq.value) {
    props.showErr("还没有攻打 QQ，没有战败库", "fail");
    return;
  }
  const who = failCount.value ? `目前记了 ${failCount.value} 人。` : "";
  if (!window.confirm(`清空这个攻打号的战败库？${who}以前打不过的人会重新可以打。`)) return;
  failBusy.value = true;
  try {
    const data = await props.resetFailLibrary();
    const n = Number(data.removed) || 0;
    failNote.value = n ? `已清空 ${n} 人` : "战败库已经是空的";
    await loadFail();
  } finally {
    failBusy.value = false;
  }
}

onMounted(() => {
  loadFail().catch((e) => props.showErr(e.message, "fail"));
});

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
    <p>今天拒绝超级强攻 {{ me.storm_day ?? 0 }} 次</p>
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
    <h3>战败库</h3>
    <p class="muted">打不过的人按 UID 记在这个攻打号上，换一座城也还是这个人。清城和绕路都会避开他们。去掉这个 UID，或者整库清空，就可以再打。同一个攻打 QQ 绑了几个登录账号，用的是同一份。</p>
    <p v-if="!failReady" class="muted">正在看战败库。</p>
    <p v-else-if="failQq">目前记了 {{ failCount }} 人。</p>
    <p v-else class="muted">还没有攻打 QQ，没有战败库。</p>
    <form v-if="failQq" class="fail-one" @submit.prevent="resetOne().catch((e) => showErr(e.message, 'fail'))">
      <label>UID
        <input v-model="failUid" inputmode="numeric" placeholder="要重新打的人" :disabled="failBusy" />
      </label>
      <button type="submit" :disabled="!failUid.trim() || failBusy">重置这个人</button>
    </form>
    <form v-if="failItems.length" class="filters" @submit.prevent>
      <label>名称
        <input v-model="failName" placeholder="筛选名称" :disabled="failBusy" />
      </label>
      <button v-if="failQuery" type="button" class="ghost" @click="failName = ''">清除</button>
    </form>
    <p v-if="failItems.length && !failShown.length" class="muted">没有这个名称。</p>
    <p v-if="failShown.length && (failView.pages > 1 || failQuery)" class="muted pager">
      显示 {{ failView.from }}–{{ failView.to }} / {{ failShown.length }} 人<template v-if="failShown.length !== failItems.length">，共 {{ failCount }} 人</template>
      <template v-if="failView.pages > 1">
        <button type="button" class="ghost" :disabled="failView.page <= 1 || failBusy" @click="failPage = failView.page - 1">上一页</button>
        第 {{ failView.page }} / {{ failView.pages }} 页
        <button type="button" class="ghost" :disabled="failView.page >= failView.pages || failBusy" @click="failPage = failView.page + 1">下一页</button>
      </template>
    </p>
    <div v-if="failView.rows.length" class="wide">
      <table class="fail-list">
        <thead>
          <tr><th>名称</th><th>UID</th><th>记下时间</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="it in failView.rows" :key="it.uid">
            <td>{{ it.name || "—" }}</td>
            <td>{{ it.uid }}</td>
            <td>{{ it.at || "—" }}</td>
            <td><button type="button" class="ghost" :disabled="failBusy" @click="resetOne(it).catch((e) => showErr(e.message, 'fail'))">重置</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="failView.pages > 1" class="muted pager">
      <button type="button" class="ghost" :disabled="failView.page <= 1 || failBusy" @click="failPage = failView.page - 1">上一页</button>
      第 {{ failView.page }} / {{ failView.pages }} 页
      <button type="button" class="ghost" :disabled="failView.page >= failView.pages || failBusy" @click="failPage = failView.page + 1">下一页</button>
    </p>
    <p>
      <button type="button" class="ghost" :disabled="!failQq || failBusy" @click="resetFail().catch((e) => showErr(e.message, 'fail'))">清空全部</button>
      <span v-if="failNote" class="muted">{{ failNote }}</span>
      <span v-if="err && errAt === 'fail'" class="err">{{ err }}</span>
    </p>
  </section>
</template>
