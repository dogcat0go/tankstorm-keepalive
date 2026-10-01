<script setup>
import { onMounted, onUnmounted, ref } from "vue";

const me = ref(null);
const mode = ref("login");
const username = ref("");
const password = ref("");
const invite = ref("");
const err = ref("");
const items = ref([]);
const db = ref("");
const cityId = ref("");
const uid = ref("");
const qqTarget = ref("");
const note = ref("");
const attackCity = ref("");
const orders = ref([]);
const devLogin = ref(false);
const registerOpen = ref(false);
let timer = 0;

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") me.value = null;
  if (!r.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function refresh() {
  const data = await api("/api/subs");
  items.value = data.items || [];
  db.value = data.db || "";
  if (me.value && me.value.remote_attack) {
    const atk = await api("/api/attacks");
    orders.value = atk.items || [];
  } else {
    orders.value = [];
  }
}

async function enter() {
  err.value = "";
  const path = mode.value === "register" && registerOpen.value ? "/api/register" : "/api/login";
  await api(path, {
    username: username.value,
    password: password.value,
    invite: invite.value,
  });
  const who = await api("/api/me");
  me.value = who.user;
  qqTarget.value = who.user.qq_target || "";
  await refresh();
}

async function devEnter() {
  err.value = "";
  await api("/api/dev-login", {});
  await loadMe();
}

async function loadMe() {
  const r = await fetch("/api/me", { credentials: "same-origin" });
  if (!r.ok) return;
  const who = await r.json();
  me.value = who.user;
  qqTarget.value = who.user.qq_target || "";
  await refresh();
}

async function addSub() {
  err.value = "";
  await api("/api/subs", { city_id: cityId.value, uid: uid.value });
  cityId.value = "";
  uid.value = "";
  await refresh();
}

async function removeSub(it) {
  await api("/api/subs/delete", { city_id: it.city_id, uid: it.uid });
  await refresh();
}

async function addOrder() {
  err.value = "";
  await api("/api/attacks", { city_id: attackCity.value });
  attackCity.value = "";
  await refresh();
}

function orderStatus(status) {
  return { pending: "排队", running: "正在打", done: "已打完", failed: "没打成" }[status] || status;
}

async function savePush() {
  note.value = "";
  err.value = "";
  await api("/api/push", { qq_target: qqTarget.value });
  note.value = "QQ 号已保存";
}

async function logout() {
  await api("/api/logout", {});
  me.value = null;
  items.value = [];
}

function statusOf(it) {
  if (it.present) return "在城里";
  if (it.checked) return "不在这座城";
  return "这座城还没扫过";
}

onMounted(async () => {
  const meta = await fetch("/api/meta").then((r) => r.json()).catch(() => ({}));
  devLogin.value = !!meta.dev_login;
  registerOpen.value = !!meta.register;
  await loadMe();
  timer = setInterval(() => {
    if (me.value) refresh().catch(() => {});
  }, 4000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <main>
    <h1>城市订阅</h1>
    <p class="lead" v-if="!me">
      {{ registerOpen ? "注册一个账号，" : "使用管理员开通的账号登录，" }}订阅某座城里有没有某个用户 UID。
    </p>
    <form v-if="!me" @submit.prevent="enter().catch((e) => (err = e.message))">
      <label>用户名<input v-model="username" autocomplete="username" required /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
      <label v-if="mode === 'register'">注册口令<input v-model="invite" autocomplete="off" /></label>
      <button type="submit">{{ mode === "register" && registerOpen ? "注册" : "登录" }}</button>
      <button v-if="registerOpen" type="button" class="ghost" @click="mode = mode === 'login' ? 'register' : 'login'">
        {{ mode === "login" ? "去注册" : "去登录" }}
      </button>
      <button v-if="devLogin" type="button" class="ghost" @click="devEnter().catch((e) => (err = e.message))">
        测试进入
      </button>
    </form>
    <template v-else>
      <p class="lead">
        {{ me.username }} · {{ me.tier || "初级" }}<template v-if="me.expires_at"> · 有效期至 {{ me.expires_at }}</template>
        · 库 <code>{{ db }}</code>
        <button type="button" class="ghost" @click="logout">退出</button>
      </p>
      <form @submit.prevent="addSub().catch((e) => (err = e.message))">
        <label>城市 ID<input v-model="cityId" inputmode="numeric" required placeholder="1201" /></label>
        <label>用户 UID<input v-model="uid" inputmode="numeric" required /></label>
        <button type="submit">订阅</button>
      </form>
      <p class="muted">这个 UID 第一次出现在扫描结果里，发一条。之后只有从不在这座城变成在线，再发一条。这一轮扫完还没见到，就记成不在这座城。</p>
      <table>
        <thead>
          <tr><th>城市</th><th>UID</th><th>昵称</th><th>状态</th><th>页</th><th>北京时间</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="it in items" :key="it.city_id + ':' + it.uid">
            <td>{{ it.city_name ? it.city_name + " " : "" }}{{ it.city_id }}</td>
            <td>{{ it.uid }}</td>
            <td>{{ it.name || "—" }}</td>
            <td :class="it.present ? 'on' : 'off'">{{ statusOf(it) }}</td>
            <td>{{ it.present && it.page != null ? it.page : "—" }}</td>
            <td>{{ it.present ? it.seen_at || "—" : (it.checked ? it.city_scanned_at || "—" : "—") }}</td>
            <td><button type="button" class="ghost" @click="removeSub(it)">取消</button></td>
          </tr>
        </tbody>
      </table>
      <p v-if="!items.length" class="muted">还没有订阅。</p>
      <h2>远程扫码攻打</h2>
      <p v-if="!me.remote_attack" class="muted">当前是{{ me.tier || "初级" }}。中级和高级可以提交，由服务器上的攻打号领取并扫码进游戏。</p>
      <template v-else>
        <form @submit.prevent="addOrder().catch((e) => (err = e.message))">
          <label>城市 ID<input v-model="attackCity" inputmode="numeric" required placeholder="2302" /></label>
          <button type="submit">提交</button>
        </form>
        <p class="muted">一次只排一条。攻打号扫码登录后，用 --orders 领取。扫描不用停。</p>
        <table v-if="orders.length">
          <thead>
            <tr><th>城市</th><th>状态</th><th>说明</th><th>北京时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="it in orders" :key="it.id">
              <td>{{ it.city_id }}</td>
              <td>{{ orderStatus(it.status) }}</td>
              <td>{{ it.reason || "—" }}</td>
              <td>{{ it.created_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      <h2>推送</h2>
      <form class="stack" @submit.prevent="savePush().catch((e) => (err = e.message))">
        <label>接收 QQ
          <input v-model="qqTarget" inputmode="numeric" autocomplete="off" placeholder="你的 QQ 号" />
        </label>
        <button type="submit">保存</button>
        <span class="muted">{{ note }}</span>
      </form>
      <p class="muted">私聊发到这个 QQ。机器人地址和 Token 在服务器配置里，页面上不填写。</p>
    </template>
    <p class="err">{{ err }}</p>
  </main>
</template>

<style>
body { margin: 0; font: 15px/1.5 sans-serif; color: #1a1a1a; background: #f6f6f4; }
main { max-width: 880px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 8px; }
h2 { font-size: 16px; margin: 28px 0 8px; }
.lead { margin: 0 0 16px; color: #444; }
form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
form.stack { display: grid; max-width: 520px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: #333; }
input { font: inherit; padding: 8px 10px; border: 1px solid #bbb; border-radius: 6px; background: #fff; }
button { font: inherit; padding: 8px 14px; border: 0; border-radius: 6px; background: #1a1a1a; color: #fff; cursor: pointer; }
button.ghost { background: transparent; color: #333; border: 1px solid #bbb; }
.err { color: #9b1c1c; min-height: 1.5em; }
.muted { color: #777; font-size: 13px; }
table { width: 100%; border-collapse: collapse; background: #fff; margin-top: 12px; }
th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #e6e6e6; vertical-align: top; }
th { font-size: 13px; color: #555; }
.on { color: #0b6b2f; font-weight: 700; }
.off { color: #666; }
code { font-size: 13px; }
</style>
