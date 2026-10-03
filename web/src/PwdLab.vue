<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import "./base.css";

const me = ref(null);
const username = ref("");
const password = ref("");
const qqUin = ref("");
const qqPwd = ref("");
const lowLogin = ref(false);
const busy = ref(false);
const err = ref("");
const lab = ref(null);
const accounts = ref([]);

let tcaptchaWait = null;
let capInst = null;

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
    idle: "还没登录",
    ok: "登录完成",
    fail: "失败",
    captcha: "要滑块",
  }[p] || p || "—";
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
  const [now, list] = await Promise.all([api("/api/pwd-lab"), api("/api/pwd-lab/accounts")]);
  lab.value = now;
  accounts.value = list.items || [];
}

function dropCaptcha() {
  if (capInst && typeof capInst.destroy === "function") {
    try {
      capInst.destroy();
    } catch (_) {
      /* TCaptcha destroy 偶尔会抛 */
    }
  }
  capInst = null;
}

function loadTCaptcha() {
  if (typeof window !== "undefined" && window.TencentCaptcha) return Promise.resolve();
  if (tcaptchaWait) return tcaptchaWait;
  tcaptchaWait = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://ssl.captcha.qq.com/TCaptcha.js";
    s.async = true;
    s.onload = () => {
      if (window.TencentCaptcha) resolve();
      else {
        tcaptchaWait = null;
        reject(new Error("腾讯验证码脚本没有挂上"));
      }
    };
    s.onerror = () => {
      tcaptchaWait = null;
      reject(new Error("腾讯验证码脚本加载失败"));
    };
    document.head.appendChild(s);
  });
  return tcaptchaWait;
}

function onCaptcha(res) {
  const ticket = res && res.ticket;
  const randstr = res && (res.randstr || res.randStr);
  if (res && res.ret === 0 && ticket && randstr) {
    pwdLogin({ ticket, randstr }).catch((e) => {
      err.value = e.message;
    });
    return;
  }
  if (res && res.ret === 2) return;
  err.value = (res && (res.errorMessage || res.errMessage)) || "滑动验证没过";
}

async function openSlider() {
  const data = lab.value;
  if (!data || data.phase !== "captcha" || !data.sid) {
    throw new Error("还没有验证码会话，请先点密码登录");
  }
  await loadTCaptcha();
  if (capInst && typeof capInst.show === "function") {
    try {
      capInst.show();
      return;
    } catch (_) {
      dropCaptcha();
    }
  } else {
    dropCaptcha();
  }
  try {
    capInst = new window.TencentCaptcha(String(data.aid || "549000912"), onCaptcha, {
      sid: data.sid,
      uin: String(qqUin.value || data.uin || ""),
      type: "popup",
      enableAged: true,
    });
    capInst.show();
  } catch (e) {
    capInst = null;
    throw new Error((e && e.message) || "腾讯验证码打不开");
  }
}

async function pwdLogin(extra) {
  err.value = "";
  busy.value = true;
  try {
    const data = await api("/api/pwd-lab/login", {
      qq: qqUin.value,
      password: qqPwd.value,
      low_login: lowLogin.value,
      ...(extra && extra.ticket ? { ticket: extra.ticket, randstr: extra.randstr } : {}),
    });
    lab.value = data;
    if (data.phase === "captcha") {
      busy.value = false;
      await openSlider();
      return;
    }
    dropCaptcha();
    if (data.phase === "fail") err.value = data.msg || "登录失败";
  } finally {
    busy.value = false;
  }
}

async function checkGame() {
  err.value = "";
  lab.value = await api("/api/pwd-lab/check", {});
}

async function clearLab() {
  err.value = "";
  dropCaptcha();
  lab.value = await api("/api/pwd-lab/clear", {});
}

async function logout() {
  await api("/api/logout", {});
  dropCaptcha();
  me.value = null;
  lab.value = null;
  accounts.value = [];
}

onMounted(loadMe);
onBeforeUnmount(dropCaptcha);
</script>

<template>
  <main>
    <h1>密码登录测试</h1>
    <p class="lead" v-if="!me">
      管理员登录后，在这里测 QQ 账号密码登录，并看票据还能用多久。
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
        走 ptlogin2 账号密码（pt_tea=2）。默认与现有扫码同一套 xlogin（low_login=0），方便对照时效。
        勾选「下次自动登录」才会带 low_login_enable=1、720 小时。票据写在独立文件里，不绑攻打号、不拉攻打线程。
        第一次腾讯常会要滑块，在本页划完即可；划过并登录成功后设备记录会留下，下次密码登录可能就不用再验证。
        清掉测试票据会把设备记录一并清掉，滑块可能又会出现。扫码登录不用在这里测。无过期表示腾讯没给 Expires。
      </p>
      <h2>这次密码登录</h2>
      <form class="stack" @submit.prevent="pwdLogin().catch((e) => (err = e.message))">
        <label>QQ 号<input v-model="qqUin" inputmode="numeric" autocomplete="off" required /></label>
        <label>QQ 密码<input v-model="qqPwd" type="password" autocomplete="off" required /></label>
        <label class="choice">
          <input v-model="lowLogin" type="checkbox" />
          下次自动登录（low_login=1，720 小时）
        </label>
        <p class="muted">{{ lab ? phaseText(lab.phase) : "—" }}<template v-if="lab && lab.msg"> · {{ lab.msg }}</template></p>
        <p class="proc">
          <button type="submit" :disabled="busy">{{ busy ? "登录中…" : "密码登录" }}</button>
          <button
            v-if="lab && lab.phase === 'captcha'"
            type="button"
            class="ghost"
            :disabled="busy"
            @click="openSlider().catch((e) => (err = e.message))"
          >打开滑动验证</button>
          <button type="button" class="ghost" @click="checkGame().catch((e) => (err = e.message))">访问游戏页校验</button>
          <button type="button" class="ghost" @click="clearLab().catch((e) => (err = e.message))">清掉测试票据</button>
        </p>
      </form>
      <p v-if="lab" class="muted">
        QQ {{ lab.uin || "还没有" }}
        · skey {{ namedLeft(lab, "skey", lab.skey_left) }}
        · p_skey {{ namedLeft(lab, "p_skey", lab.p_skey_left) }}
        · {{ lab.long_term ? "有长效凭据" : "没有 superkey / RK / ptcz" }}
        · {{ lab.low_login ? "这次带了 low_login" : "这次没带 low_login" }}
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
