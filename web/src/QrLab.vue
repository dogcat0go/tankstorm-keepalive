<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import "./base.css";

const me = ref(null);
const username = ref("");
const password = ref("");
const err = ref("");
const lab = ref(null);
const accounts = ref([]);
const qrSrc = ref("");
let timer = 0;
let seq = 0;

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

function leftText(sec) {
  if (sec == null) return "无过期";
  if (sec <= 0) return "已过期";
  const day = sec / 86400;
  if (day >= 2) return day.toFixed(1) + " 天";
  const hour = sec / 3600;
  if (hour >= 1) return hour.toFixed(1) + " 小时";
  return Math.max(1, Math.round(sec / 60)) + " 分钟";
}

function namedLeft(data, name, sec) {
  const has = ((data && data.tickets) || []).some((row) => row.name === name);
  if (!has) return "没有";
  return leftText(sec);
}

function expText(row) {
  if (!row || row.session || row.expires == null) return "无过期";
  return new Date(row.expires * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

function phaseText(p) {
  return {
    idle: "还没取码",
    qr: "等待扫码",
    scanned: "已扫，待确认",
    ok: "登录完成",
    fail: "失败",
    expired: "二维码失效",
  }[p] || p || "—";
}

function takeLab(data) {
  lab.value = data;
  qrSrc.value = data && data.qr ? "/api/qr-lab/qr?t=" + Date.now() : "";
}

async function enter() {
  err.value = "";
  await api("/api/login", { username: username.value, password: password.value });
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
  const mine = seq;
  const [now, list] = await Promise.all([api("/api/qr-lab"), api("/api/qr-lab/accounts")]);
  if (mine !== seq) return;
  takeLab(now);
  accounts.value = list.items || [];
}

async function startQr() {
  err.value = "";
  seq += 1;
  const data = await api("/api/qr-lab/start", {});
  seq += 1;
  takeLab(data);
}

async function checkGame() {
  err.value = "";
  seq += 1;
  const data = await api("/api/qr-lab/check", {});
  seq += 1;
  takeLab(data);
}

async function clearLab() {
  err.value = "";
  seq += 1;
  const data = await api("/api/qr-lab/clear", {});
  seq += 1;
  takeLab(data);
}

async function logout() {
  await api("/api/logout", {});
  me.value = null;
  lab.value = null;
  accounts.value = [];
  qrSrc.value = "";
}

onMounted(async () => {
  await loadMe();
  timer = setInterval(() => {
    if (me.value && me.value.admin) refresh().catch(() => {});
  }, 3000);
});
onUnmounted(() => {
  clearInterval(timer);
});
</script>

<template>
  <main>
    <h1>扫码登录测试</h1>
    <p class="lead" v-if="!me">
      管理员登录后，在这里测现有的 QQ 扫码，并看票据还能用多久。
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
        走现有 ptlogin2 扫码（low_login=0），票据写在独立文件里，不绑攻打号、不拉攻打线程。
        密码登录常常下发无过期的会话 cookie 或更长的 superkey，扫码的 skey 大约一天半。扫完看表里的剩余时间就能对上。
      </p>
      <h2>这次扫码</h2>
      <p class="proc">
        <span class="proc-text">{{ lab ? phaseText(lab.phase) : "—" }}<template v-if="lab && lab.msg"> · {{ lab.msg }}</template></span>
        <button type="button" @click="startQr().catch((e) => (err = e.message))">取二维码</button>
        <button type="button" class="ghost" @click="checkGame().catch((e) => (err = e.message))">访问游戏页校验</button>
        <button type="button" class="ghost" @click="clearLab().catch((e) => (err = e.message))">清掉测试票据</button>
      </p>
      <p v-if="lab && lab.wait_left" class="muted">还剩 {{ lab.wait_left }} 秒，过期会自动停。请用另一台设备扫。</p>
      <img v-if="qrSrc" class="qr" :src="qrSrc" alt="扫码登录测试二维码" />
      <p v-if="lab" class="muted">
        QQ {{ lab.uin || "还没有" }}
        · skey {{ namedLeft(lab, "skey", lab.skey_left) }}
        · p_skey {{ namedLeft(lab, "p_skey", lab.p_skey_left) }}
        · {{ lab.long_term ? "有长效凭据" : "没有 superkey / RK / ptcz" }}
        · 游戏页 {{ lab.game_ok == null ? "还没校验" : lab.game_ok ? "认" : "不认" }}
        · <code>{{ lab.cookie }}</code>
      </p>
      <div class="wide" v-if="lab && lab.tickets && lab.tickets.length">
        <table>
          <thead>
            <tr><th>名字</th><th>域</th><th>过期（北京时间）</th><th>剩余</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in lab.tickets" :key="row.name + row.domain">
              <td><code>{{ row.name }}</code></td>
              <td class="muted">{{ row.domain }}</td>
              <td :class="row.session ? 'off' : ''">{{ expText(row) }}</td>
              <td :class="row.session || (row.left != null && row.left > 7 * 86400) ? 'off' : 'on'">{{ leftText(row.left) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h2>现有攻打号票据</h2>
      <p class="muted">只读本地 cookie 文件的过期时间，不访问游戏，也不改正在跑的线程。无过期表示腾讯没给 Expires，本地会一直当成还能用。</p>
      <p v-if="!accounts.length" class="muted">还没有攻打号 cookie。</p>
      <div class="wide" v-for="it in accounts" :key="it.cookie">
        <p>
          {{ it.username || "未绑定登录账号" }}
          · QQ {{ it.qq || it.uin || "—" }}
          · skey {{ it.missing ? "没有文件" : namedLeft(it, "skey", it.skey_left) }}
          · {{ it.long_term ? "有长效凭据" : "没有长效凭据" }}
          · <code>{{ it.cookie }}</code>
        </p>
        <table v-if="it.tickets && it.tickets.length">
          <thead>
            <tr><th>名字</th><th>域</th><th>过期（北京时间）</th><th>剩余</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in it.tickets" :key="it.cookie + row.name + row.domain">
              <td><code>{{ row.name }}</code></td>
              <td class="muted">{{ row.domain }}</td>
              <td :class="row.session ? 'off' : ''">{{ expText(row) }}</td>
              <td :class="row.session || (row.left != null && row.left > 7 * 86400) ? 'off' : 'on'">{{ leftText(row.left) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <p class="err">{{ err }}</p>
  </main>
</template>
