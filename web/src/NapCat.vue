<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import "./base.css";

const me = ref(null);
const username = ref("");
const password = ref("");
const qq = ref("");
const api = ref("");
const token = ref("");
const web = ref("");
const webToken = ref("");
const platform = ref("");
const apply = ref(false);
const busy = ref(false);
const qrBusy = ref(false);
const upBusy = ref("");
const err = ref("");
const info = ref(null);
const result = ref(null);
const qrcode = ref("");
const loginMsg = ref("");
const loggedIn = ref(false);
let qrTimer = 0;

async function apiCall(path, body) {
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

function leftText(sec) {
  if (sec == null) return "无过期";
  if (sec <= 0) return "已过期";
  const hour = sec / 3600;
  if (hour >= 1) return hour.toFixed(1) + " 小时";
  return Math.max(1, Math.round(sec / 60)) + " 分钟";
}

function expText(row) {
  if (!row || row.session || row.expires == null) return "无过期";
  return new Date(row.expires * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

async function enter() {
  err.value = "";
  await apiCall("/api/login", { username: username.value, password: password.value });
  await loadMe();
}

async function loadMe() {
  const r = await fetch("/api/me", { credentials: "same-origin" });
  if (!r.ok) return;
  const who = await r.json();
  me.value = who.user;
  if (!who.user.admin) return;
  await refresh();
}

function stopQr() {
  if (qrTimer) {
    clearInterval(qrTimer);
    qrTimer = 0;
  }
}

async function refresh() {
  if (!me.value || !me.value.admin) return;
  info.value = await apiCall("/api/test", { action: "list" });
}

function useRow(row) {
  qq.value = row.qq || "";
  api.value = "";
  token.value = "";
  web.value = "";
  webToken.value = "";
  result.value = null;
  err.value = "";
}

function napcatBody(action) {
  const body = { action, qq: qq.value };
  if (api.value.trim()) body["地址"] = api.value.trim();
  if (token.value) body.Token = token.value;
  if (web.value.trim()) body["界面"] = web.value.trim();
  if (webToken.value) body["界面Token"] = webToken.value;
  if (platform.value) body["平台"] = platform.value;
  return body;
}

async function askQr() {
  if (qrBusy.value) return;
  qrBusy.value = true;
  err.value = "";
  try {
    const data = await apiCall("/api/test", napcatBody("qr"));
    loggedIn.value = !!data.logged_in;
    qrcode.value = data.logged_in ? "" : (data.qrcode || "");
    loginMsg.value = data.msg || "";
    if (!data.ok) err.value = data.msg || "没有拿到 NapCat 登录码";
    if (data.logged_in) stopQr();
    else if (!qrTimer) qrTimer = setInterval(() => askQr().catch((e) => (err.value = e.message)), 3000);
  } finally {
    qrBusy.value = false;
  }
}

async function upBox(row) {
  const id = row ? row.qq : qq.value;
  if (upBusy.value) return;
  if (!id) {
    err.value = "先填攻打 QQ";
    return;
  }
  upBusy.value = id;
  err.value = "";
  try {
    const data = await apiCall("/api/test", {
      action: "up",
      qq: id,
      username: row && row.username || "",
    });
    loginMsg.value = data.msg || "";
    if (!data.ok) err.value = data.msg || "没能起 NapCat";
    await refresh();
  } finally {
    upBusy.value = "";
  }
}

async function pull() {
  err.value = "";
  busy.value = true;
  try {
    const body = napcatBody("cookies");
    body.apply = apply.value;
    result.value = await apiCall("/api/test", body);
    await refresh();
  } finally {
    busy.value = false;
  }
}

async function logout() {
  await apiCall("/api/logout", {});
  me.value = null;
  info.value = null;
  result.value = null;
}

onMounted(loadMe);
onUnmounted(stopQr);
</script>

<template>
  <main>
    <h1>攻打号 NapCat 测试</h1>
    <p class="lead" v-if="!me">
      管理员登录后，先取 NapCat 自己的登录二维码，登录后再要网页票据。
      <a class="ghost" href="/admin">管理</a>
      <a class="ghost" href="/">回到订阅</a>
    </p>
    <form v-if="!me" @submit.prevent="enter().catch((e) => (err = e.message))">
      <label>用户名<input v-model="username" autocomplete="username" required /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
      <button type="submit">登录</button>
    </form>
    <template v-else-if="!me.admin">
      <p class="lead">
        {{ me.username }} 没有管理权限。
        <a class="ghost" href="/">回到订阅</a>
        <button type="button" class="ghost" @click="logout">退出</button>
      </p>
    </template>
    <template v-else>
      <p class="lead">
        {{ me.username }}
        <a class="ghost" href="/admin">管理</a>
        <a class="ghost" href="/">回到订阅</a>
        <button type="button" class="ghost" @click="logout">退出</button>
      </p>
      <p class="muted">
        这个页面就是测试入口 /api/test。第一次登录 NapCat 会向它的界面要登录二维码，扫的是 NapCat 自己的码。
        登录之后再 POST /get_cookies 要网页票据。不收消息、不发消息、不另开线程。
        OneBot 地址和界面地址写在 config.local.json 的登录.账号.NapCat，也可以在下面临时填。界面 Token 是 WebUI 口令。
        同时取票数是 {{ info ? info.limit : 1 }}，现在进行中 {{ info ? info.inflight : 0 }}。
        名额满了这次直接返回，不会排队再开一条。
        勾选写入时，攻打线程正在跑就不会改它手里的 cookie。
        还没有 NapCat 的号，点「起 NapCat」。程序拉起容器并写好地址和 Token，不用自己起 Docker。接着再取登录码。
      </p>
      <form class="stack" @submit.prevent="pull().catch((e) => (err = e.message))">
        <label>攻打 QQ<input v-model="qq" inputmode="numeric" autocomplete="off" /></label>
        <label>NapCat 地址<input v-model="api" placeholder="留空则用该账号配置里的地址" autocomplete="off" /></label>
        <label>Token<input v-model="token" type="password" placeholder="留空则用配置里的 Token" autocomplete="off" /></label>
        <label>NapCat 界面<input v-model="web" placeholder="留空则用配置里的界面，例如 http://127.0.0.1:6099" autocomplete="off" /></label>
        <label>界面 Token<input v-model="webToken" type="password" placeholder="留空则用配置里的界面Token" autocomplete="off" /></label>
        <label>平台
          <select v-model="platform">
            <option value="">跟随登录.平台</option>
            <option value="空间">空间</option>
            <option value="大厅">大厅</option>
          </select>
        </label>
        <label class="choice">
          <input v-model="apply" type="checkbox" />
          游戏页认了就写入这个攻打号的 cookie
        </label>
        <p class="proc">
          <button type="button" :disabled="!!upBusy" @click="upBox().catch((e) => (err = e.message))">{{ upBusy === qq ? "正在起…" : "起 NapCat" }}</button>
          <button type="button" :disabled="qrBusy" @click="askQr().catch((e) => (err = e.message))">取 NapCat 登录码</button>
          <button type="submit" :disabled="busy">{{ busy ? "取票中…" : "向 NapCat 要票据" }}</button>
          <button type="button" class="ghost" @click="refresh().catch((e) => (err = e.message))">刷新列表</button>
        </p>
      </form>
      <p v-if="loginMsg" class="muted">{{ loginMsg }}</p>
      <img v-if="qrcode" class="qr" :src="qrcode" alt="NapCat 登录二维码" />
      <p v-if="result" class="muted">
        {{ result.msg }}
        <template v-if="result.host"> · {{ result.host }}</template>
        <template v-if="result.uin"> · NapCat QQ {{ result.uin }}</template>
        · 游戏页 {{ result.game_ok == null ? "还没验" : result.game_ok ? "认" : "不认" }}
        · {{ result.applied ? "已写入" : "未写入" }}
        · {{ result.worker ? "攻打线程在跑" : "攻打线程没在跑" }}
      </p>
      <div class="wide" v-if="result && result.tickets && result.tickets.length">
        <table>
          <thead>
            <tr><th>名字</th><th>域</th><th>过期（北京时间）</th><th>剩余</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in result.tickets" :key="row.name + row.domain">
              <td><code>{{ row.name }}</code></td>
              <td class="muted">{{ row.domain }}</td>
              <td>{{ expText(row) }}</td>
              <td>{{ leftText(row.left) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h2>已绑定的攻打号</h2>
      <p v-if="!info || !info.items.length" class="muted">还没有绑定的攻打 QQ。</p>
      <div class="wide" v-for="row in (info && info.items) || []" :key="row.qq">
        <p>
          {{ row.username || "未绑定登录账号" }}
          · QQ {{ row.qq }}
          · {{ row.configured ? row.host : "没配 NapCat" }}
          · {{ row.token_set ? "有 Token" : "没 Token" }}
          · {{ row.web_set ? row.web_host : "没配界面" }}
          · {{ row.web_token_set ? "有界面 Token" : "没界面 Token" }}
          · {{ row.online ? "线程在线" : "线程不在" }}
          · skey {{ row.missing ? "没有文件" : leftText(row.skey_left) }}
          <button type="button" class="ghost" :disabled="!!upBusy" @click="upBox(row).catch((e) => (err = e.message))">{{ upBusy === row.qq ? "正在起…" : "起 NapCat" }}</button>
          <button type="button" class="ghost" @click="useRow(row)">填到上面</button>
        </p>
      </div>
    </template>
    <p class="err">{{ err }}</p>
  </main>
</template>
