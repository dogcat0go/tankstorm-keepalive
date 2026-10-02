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
const cities = ref([]);
const uid = ref("");
const qqTarget = ref("");
const note = ref("");
const attackCity = ref("");
const attackUid = ref("");
const orders = ref([]);
const proc = ref(null);
const qrSrc = ref("");
const devLogin = ref(false);
const registerOpen = ref(false);
const scanGap = ref("300");
const quietStart = ref("");
const quietEnd = ref("");
const scanRanges = ref([]);
const scanNote = ref("");
const scanQuiet = ref(false);
const autoLock = ref(false);
const holdMin = ref("0");
const cardMax = ref("100");
const holdUntil = ref(0);
const clock = ref(Date.now());
let timer = 0;
let atkTimer = 0;
let clockTimer = 0;

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
}

async function refreshAttacks() {
  if (!me.value || !me.value.remote_attack) {
    orders.value = [];
    proc.value = null;
    return;
  }
  const atk = await api("/api/attacks");
  orders.value = atk.items || [];
  proc.value = atk.process || null;
  const left = proc.value && proc.value.hold_left;
  holdUntil.value = left > 0 ? Date.now() + left * 1000 : 0;
  qrSrc.value = proc.value && proc.value.qr ? "/api/attack-qr?t=" + Date.now() : "";
  if (qrSrc.value) qrWait = 0;
}

let qrWait = 0;
function reloadQr() {
  if (!qrSrc.value || qrWait >= 12) return;
  qrWait += 1;
  setTimeout(() => {
    if (qrSrc.value) qrSrc.value = "/api/attack-qr?t=" + Date.now();
  }, 1000);
}

function procText(p) {
  if (!p) return "没在跑";
  if (!p.online) return p.paused && p.detail ? p.detail : "没在跑";
  if (p.paused && p.phase !== "login") return p.detail || "已暂停";
  return p.detail || "空闲，等订单";
}

async function setAttackPause(on) {
  err.value = "";
  const data = await api("/api/attack-pause", { on });
  if (proc.value) proc.value.paused = !!data.paused;
  await refreshAttacks();
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
  autoLock.value = !!who.user.auto_lock;
  holdMin.value = String(who.user.hold_min ?? 0);
  cardMax.value = String(who.user.card_max ?? 100);
  await loadCities();
  await refresh();
  await refreshAttacks();
  await loadScan();
}

async function loadCities() {
  try {
    const data = await api("/api/cities");
    cities.value = data.items || [];
  } catch (e) {
    err.value = e.message;
  }
}

function cityLabel(c) {
  const same = cities.value.filter((x) => x.name === c.name).length;
  return same > 1 ? c.name + " " + c.id : c.name;
}

async function loadScan() {
  if (!me.value || !me.value.admin) return;
  const data = await api("/api/scan-plan");
  scanGap.value = String(data.gap_sec ?? 300);
  quietStart.value = data.quiet_start || "";
  quietEnd.value = data.quiet_end || "";
  scanQuiet.value = !!data.quiet_now;
  scanRanges.value = (data.ranges || []).map((row) => ({
    city_id: String(row.city_id),
    city_name: row.city_name || "",
    start_page: String(row.start_page),
    end_page: String(row.end_page),
  }));
}

async function saveScan() {
  err.value = "";
  scanNote.value = "";
  const data = await api("/api/scan-plan", {
    gap_sec: scanGap.value,
    quiet_start: quietStart.value,
    quiet_end: quietEnd.value,
    ranges: scanRanges.value.map((row) => ({
      city_id: row.city_id,
      start_page: row.start_page,
      end_page: row.end_page,
    })),
  });
  scanGap.value = String(data.gap_sec ?? 300);
  quietStart.value = data.quiet_start || "";
  quietEnd.value = data.quiet_end || "";
  scanQuiet.value = !!data.quiet_now;
  scanRanges.value = (data.ranges || []).map((row) => ({
    city_id: String(row.city_id),
    city_name: row.city_name || "",
    start_page: String(row.start_page),
    end_page: String(row.end_page),
  }));
  scanNote.value = "已保存。正在跑的扫描进程下一轮按这个间隔和名单翻页";
}

function addScanCity() {
  scanRanges.value.push({ city_id: "", city_name: "", start_page: "0", end_page: "0" });
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
  autoLock.value = !!who.user.auto_lock;
  holdMin.value = String(who.user.hold_min ?? 0);
  cardMax.value = String(who.user.card_max ?? 100);
  await loadCities();
  await refresh();
  await refreshAttacks();
  await loadScan();
}

async function saveAutoLock(ev) {
  const on = ev.target.checked;
  err.value = "";
  try {
    const data = await api("/api/auto-lock", { on });
    autoLock.value = !!data.auto_lock;
    err.value = attackLoginError(data.login);
    await refreshAttacks();
  } catch (e) {
    ev.target.checked = autoLock.value;
    err.value = e.message;
  }
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
  const data = await api("/api/attacks", {
    city_id: attackCity.value,
    uid: attackUid.value,
    minutes: holdMin.value,
    cards: cardMax.value,
  });
  attackCity.value = "";
  attackUid.value = "";
  if (data.hold_min != null) holdMin.value = String(data.hold_min);
  if (data.card_max != null) cardMax.value = String(data.card_max);
  err.value = attackLoginError(data.login);
  await refreshAttacks();
}

function beatText(it) {
  if (it.beats == null) return "—";
  return String(it.beats);
}

function orderCity(it) {
  if (!it) return "";
  return it.city_name ? it.city_id + " " + it.city_name : String(it.city_id ?? "");
}

function holdClock() {
  if (!holdUntil.value) return "";
  const sec = Math.max(0, Math.round((holdUntil.value - clock.value) / 1000));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
}

function holdCell(it) {
  const text = holdClock();
  if (!text || it.status === "pending" || it.status === "running") return "—";
  const finished = orders.value.find((row) => row.status !== "pending" && row.status !== "running");
  return finished && finished.id === it.id ? text : "—";
}

async function pushLogin() {
  err.value = "";
  const data = await api("/api/attack-login", {});
  err.value = attackLoginError(data.login);
  await refreshAttacks();
}

function attackLoginError(login) {
  if (login === "no_account") return "服务器还没配置攻打号，二维码发不出去";
  if (login === "taken") return "这个攻打号已经绑定别的登录账号，不能接着用";
  if (login === "unbound") return "这个登录账号还没绑定攻打号";
  return "";
}

function orderStatus(status) {
  return { pending: "排队", running: "正在打", blocked: "等通路", done: "已打完", failed: "没打成" }[status] || status;
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
  orders.value = [];
  proc.value = null;
  qrSrc.value = "";
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
  atkTimer = setInterval(() => {
    if (me.value) refreshAttacks().catch(() => {});
  }, 5000);
  clockTimer = setInterval(() => {
    clock.value = Date.now();
  }, 1000);
});
onUnmounted(() => {
  clearInterval(timer);
  clearInterval(atkTimer);
  clearInterval(clockTimer);
});
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
      <details v-if="me.admin" class="fold">
        <summary>扫描安排</summary>
        <form class="stack scan-form" @submit.prevent="saveScan().catch((e) => (err = e.message))">
          <label>间隔秒<input v-model="scanGap" inputmode="numeric" required /></label>
          <label>停扫开始<input v-model="quietStart" type="time" /></label>
          <label>停扫结束<input v-model="quietEnd" type="time" /></label>
          <p class="muted">北京时间。例如 01:00 到 05:00 这段不翻页，游戏连接保持。23:00 到 05:00 这样跨过零点也可以。两个都空着就是全天扫。页码从 0 起，含结束页；订阅页面上的页数要减 1。一座城只填一段。</p>
          <p v-if="scanQuiet">现在处于停扫时段，扫描会跳过。</p>
          <table class="scan">
            <thead>
              <tr><th>城市</th><th>起始页</th><th>结束页</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in scanRanges" :key="i">
                <td>
                  <select v-model="row.city_id" required>
                    <option value="" disabled>选择城市</option>
                    <option v-if="row.city_id && !cities.some((c) => String(c.id) === String(row.city_id))" :value="String(row.city_id)">{{ row.city_name || row.city_id }}</option>
                    <option v-for="c in cities" :key="c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
                  </select>
                </td>
                <td><input v-model="row.start_page" inputmode="numeric" required /></td>
                <td><input v-model="row.end_page" inputmode="numeric" required /></td>
                <td><button type="button" class="ghost" @click="scanRanges.splice(i, 1)">去掉</button></td>
              </tr>
            </tbody>
          </table>
          <p v-if="!scanRanges.length" class="muted">还没有城市。加上之后，扫描进程按这里的页范围翻。</p>
          <p>
            <button type="button" class="ghost" @click="addScanCity">加一座城</button>
            <button type="submit">保存扫描安排</button>
            <span class="muted">{{ scanNote }}</span>
          </p>
        </form>
      </details>
      <form @submit.prevent="addSub().catch((e) => (err = e.message))">
        <label>城市
          <select v-model="cityId" required>
            <option value="" disabled>选择城市</option>
            <option v-for="c in cities" :key="c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
          </select>
        </label>
        <label>用户 UID<input v-model="uid" inputmode="numeric" required /></label>
        <button type="submit">订阅</button>
      </form>
      <p class="muted">这个 UID 第一次出现在扫描结果里，发一条。之后只有从不在这座城变成在线，再发一条。这一轮扫完还没见到，就记成不在这座城。</p>
      <p v-if="me.remote_attack" class="lock-row">
        <label class="switch">
          <input type="checkbox" :checked="autoLock" @change="saveAutoLock" />
          自动锁敌
        </label>
        <span class="muted">{{ autoLock ? "已打开。订阅的人在城里就排队攻打，打开时人已经在的，马上排一条。" : "已关闭。" }}路径上有打不过的人就停在原地。同一个人一直在城里，不会重复排。</span>
      </p>
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
        <form class="attack-row" @submit.prevent="addOrder().catch((e) => (err = e.message))">
          <label>城市
            <select v-model="attackCity" required>
              <option value="" disabled>选择城市</option>
              <option v-for="c in cities" :key="c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
            </select>
          </label>
          <label>UID<input v-model="attackUid" inputmode="numeric" placeholder="留空则打整座城" /></label>
          <label>挂机保活分钟<input v-model="holdMin" class="mins" inputmode="numeric" required /></label>
          <label>最多恢复卡<input v-model="cardMax" class="mins" inputmode="numeric" required /></label>
          <button type="submit">提交攻打</button>
        </form>
        <p class="muted">打完或打不过之后，游戏连接再保持这么久，可和自动锁敌一起用。有打不过的人挡路时，这段时间会继续看路径，通了立刻接着打原来的订单。0 表示打完就下线。</p>
        <p>
          <button type="button" class="ghost" @click="pushLogin().catch((e) => (err = e.message))">推送登录二维码</button>
        </p>
        <p class="proc">
          <span class="proc-text">攻打进程：{{ procText(proc) }}<template v-if="proc && proc.online && proc.seen_at && !proc.paused"> · {{ proc.seen_at }}</template></span>
          <button v-if="proc && proc.online && proc.paused && proc.phase !== 'login'" type="button" class="ghost" @click="setAttackPause(false).catch((e) => (err = e.message))">继续</button>
          <button v-else-if="proc && proc.online" type="button" class="ghost" @click="setAttackPause(true).catch((e) => (err = e.message))">暂停</button>
        </p>
        <p v-if="proc && proc.online && proc.here">人在 {{ proc.here }}</p>
        <p class="muted">每 5 秒刷新一次。还没绑定攻打号时，第一次点推送登录，二维码显示在这下面。扫上的 QQ 会记下，以后换成别的 QQ 就暂停。</p>
        <img v-if="qrSrc" class="qr" :src="qrSrc" alt="攻打号登录二维码" @error="reloadQr" />
        <div class="orders" v-if="orders.length">
        <table>
          <thead>
            <tr><th>城市</th><th>UID</th><th>击退敌方数量</th><th>状态</th><th>说明</th><th>保活剩余倒计时</th><th>北京时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="it in orders" :key="it.id">
              <td>{{ orderCity(it) }}</td>
              <td>{{ it.uid || "整座城" }}</td>
              <td>{{ beatText(it) }}</td>
              <td>{{ orderStatus(it.status) }}</td>
              <td class="reason">{{ it.reason || "—" }}</td>
              <td>{{ holdCell(it) }}</td>
              <td>{{ it.created_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
        <div class="order-cards">
          <article class="order-card" v-for="it in orders" :key="'c' + it.id">
            <p><span class="k">城市</span>{{ orderCity(it) }}</p>
            <p><span class="k">UID</span>{{ it.uid || "整座城" }}</p>
            <p><span class="k">击退敌方数量</span>{{ beatText(it) }}</p>
            <p><span class="k">状态</span>{{ orderStatus(it.status) }}</p>
            <p class="reason"><span class="k">说明</span>{{ it.reason || "—" }}</p>
            <p><span class="k">保活剩余倒计时</span>{{ holdCell(it) }}</p>
            <p><span class="k">北京时间</span>{{ it.created_at || "—" }}</p>
          </article>
        </div>
        </div>
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
details.fold { margin: 28px 0 8px; }
details.fold summary { font-size: 16px; font-weight: 700; cursor: pointer; }
details.fold form { margin-top: 8px; }
.lock-row { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; }
label.switch { flex-direction: row; align-items: center; gap: 8px; font-size: 15px; font-weight: 700; }
.lead { margin: 0 0 16px; color: #444; }
form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
form.stack { display: grid; max-width: 520px; }
form.scan-form { max-width: none; }
.wide { overflow-x: auto; }
.proc { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.orders { overflow-x: auto; }
.orders table { min-width: 760px; }
.orders th, .orders td { white-space: nowrap; }
.orders td.reason { white-space: normal; min-width: 16em; max-width: 28em; }
.order-cards { display: none; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: #333; }
input, select { font: inherit; padding: 8px 10px; border: 1px solid #bbb; border-radius: 6px; background: #fff; max-width: 100%; }
input.mins { width: 6em; }
button { font: inherit; padding: 8px 14px; border: 0; border-radius: 6px; background: #1a1a1a; color: #fff; cursor: pointer; }
button.ghost { background: transparent; color: #333; border: 1px solid #bbb; }
p button.ghost { margin-left: 8px; }
.err { color: #9b1c1c; min-height: 1.5em; }
.muted { color: #777; font-size: 13px; }
table { width: 100%; border-collapse: collapse; background: #fff; margin-top: 12px; }
table.scan td input { width: 7em; box-sizing: border-box; }
table.scan td select { min-width: 9em; }
th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #e6e6e6; vertical-align: top; }
th { font-size: 13px; color: #555; }
.on { color: #0b6b2f; font-weight: 700; }
.off { color: #666; }
code { font-size: 13px; }
img.qr { width: 220px; height: auto; background: #fff; padding: 8px; border: 1px solid #ddd; }
@media (max-width: 720px) {
  main { padding: 16px 12px 40px; }
  form.attack-row { display: grid; grid-template-columns: 1fr 1fr; }
  form.attack-row > button { grid-column: 1 / -1; }
  .proc-text { flex: 1 1 100%; }
  .orders table { display: none; }
  .order-cards { display: grid; gap: 10px; margin-top: 12px; }
  .order-card { background: #fff; border-radius: 8px; padding: 12px; }
  .order-card p { margin: 0 0 6px; }
  .order-card .k { display: block; color: #777; font-size: 12px; }
  .order-card .reason { white-space: normal; word-break: break-word; }
}
</style>
