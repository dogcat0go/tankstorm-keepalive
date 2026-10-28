<script setup>
import { onMounted, ref } from "vue";
import "./base.css";

const me = ref(null);
const username = ref("");
const password = ref("");
const qq = ref("");
const api = ref("");
const token = ref("");
const platform = ref("");
const apply = ref(false);
const busy = ref(false);
const err = ref("");
const info = ref(null);
const result = ref(null);

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

async function refresh() {
  if (!me.value || !me.value.admin) return;
  info.value = await apiCall("/api/attack-napcat");
}

function useRow(row) {
  qq.value = row.qq || "";
  api.value = "";
  token.value = "";
  result.value = null;
  err.value = "";
}

async function pull() {
  err.value = "";
  busy.value = true;
  try {
    const body = { qq: qq.value, apply: apply.value };
    if (api.value.trim()) body["地址"] = api.value.trim();
    if (token.value) body.Token = token.value;
    if (platform.value) body["平台"] = platform.value;
    result.value = await apiCall("/api/attack-napcat", body);
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
</script>

<template>
  <main>
    <h1>攻打号 NapCat</h1>
    <p class="lead" v-if="!me">
      管理员登录后，向这个攻打号自己的 NapCat 要一次网页票据。
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
        只发 POST /get_cookies，不收消息、不发消息、不另开线程。
        地址写在 config.local.json 的登录.账号.NapCat，也可以在下面临时填。
        同时取票数是 {{ info ? info.limit : 1 }}，现在进行中 {{ info ? info.inflight : 0 }}。
        名额满了这次直接返回，不会排队再开一条。
        勾选写入时，攻打线程正在跑就不会改它手里的 cookie。
      </p>
      <form class="stack" @submit.prevent="pull().catch((e) => (err = e.message))">
        <label>攻打 QQ<input v-model="qq" inputmode="numeric" autocomplete="off" required /></label>
        <label>NapCat 地址<input v-model="api" placeholder="留空则用该账号配置里的地址" autocomplete="off" /></label>
        <label>Token<input v-model="token" type="password" placeholder="留空则用配置里的 Token" autocomplete="off" /></label>
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
          <button type="submit" :disabled="busy">{{ busy ? "取票中…" : "向 NapCat 要票据" }}</button>
          <button type="button" class="ghost" @click="refresh().catch((e) => (err = e.message))">刷新列表</button>
        </p>
      </form>
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
          · {{ row.online ? "线程在线" : "线程不在" }}
          · skey {{ row.missing ? "没有文件" : leftText(row.skey_left) }}
          <button type="button" class="ghost" @click="useRow(row)">填到上面</button>
        </p>
      </div>
    </template>
    <p class="err">{{ err }}</p>
  </main>
</template>
