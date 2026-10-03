<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import "./base.css";

const me = ref(null);
const mode = ref("login");
const username = ref("");
const password = ref("");
const invite = ref("");
const err = ref("");
const items = ref([]);
const cityCounts = ref([]);
const subOpen = ref({});
const db = ref("");
const cityId = ref("");
const cities = ref([]);
const uid = ref("");
const qqTarget = ref("");
const note = ref("");
const attackCity = ref("");
const attackUid = ref("");
const orders = ref([]);
const storms = ref([]);
const proc = ref(null);
const qrSrc = ref("");
const devLogin = ref(false);
const registerOpen = ref(false);
const autoLock = ref(false);
const holdMin = ref("0");
const cardMax = ref("100");
const retreatMode = ref("hops");
const retreatHops = ref("3");
const retreatCity = ref("0");
const retreatNote = ref("");
const lockCards = ref("3");
const lockNote = ref("");
const advanced = ref(false);
const attackAdvanced = ref(false);
const clearMode = ref("head");
const clearFrom = ref("1");
const clearTo = ref("5");
const clearWait = ref("0");
const clearScan = ref("0");
const clearRows = ref([]);
const clearNote = ref("");
const prioUid = ref("");
const prioRank = ref("1");
const modoCards = ref("0");
const moveCity = ref("");
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
  cityCounts.value = data.counts || [];
  db.value = data.db || "";
}

async function refreshAttacks() {
  if (!me.value) {
    orders.value = [];
    storms.value = [];
    proc.value = null;
    return;
  }
  const atk = await api("/api/attacks");
  orders.value = atk.items || [];
  storms.value = atk.storms || [];
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
  takeUser(who.user);
  await loadCities();
  pinRetreatCity();
  await refresh();
  await refreshAttacks();
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

async function devEnter() {
  err.value = "";
  await api("/api/dev-login", {});
  await loadMe();
}

async function loadMe() {
  const r = await fetch("/api/me", { credentials: "same-origin" });
  if (!r.ok) return;
  const who = await r.json();
  takeUser(who.user);
  await loadCities();
  pinRetreatCity();
  await refresh();
  await refreshAttacks();
}

function takeUser(user) {
  me.value = user;
  qqTarget.value = user.qq_target || "";
  autoLock.value = !!user.auto_lock;
  holdMin.value = String(user.hold_min ?? 0);
  cardMax.value = String(user.card_max ?? 100);
  const picked = user.retreat_mode;
  retreatMode.value = picked === "off" || picked === "city" || picked === "hops" ? picked : "hops";
  retreatHops.value = String(user.retreat_hops ?? 3);
  retreatCity.value = String(user.retreat_city || 0);
  lockCards.value = String(user.lock_cards ?? 3);
  clearMode.value = user.clear_mode === "range" ? "range" : "head";
  clearFrom.value = String(user.clear_from || 1);
  clearTo.value = String(user.clear_to || 5);
  clearWait.value = String(user.clear_wait ?? 0);
  clearScan.value = String(user.clear_scan ?? 0);
  clearRows.value = (user.clear_priority || []).map((row) => ({
    uid: String(row.uid),
    rank: String(row.rank),
  }));
  modoCards.value = String(user.modo_cards ?? 0);
}

function pinRetreatCity() {
  if (retreatMode.value === "hops" && retreatCity.value === "0") {
    const hit = cities.value.find((c) => c.name === "马奇诺");
    if (hit) retreatCity.value = String(hit.id);
  }
}

function addPriority() {
  err.value = "";
  const uid = prioUid.value.trim();
  if (!/^\d{1,32}$/.test(uid)) throw new Error("优先 UID 要是数字");
  if (clearRows.value.some((row) => row.uid === uid)) throw new Error("这个 UID 已经在名单里");
  if (clearRows.value.length >= 50) throw new Error("优先 UID 最多 50 个");
  const rank = Number(prioRank.value || "1");
  if (!Number.isInteger(rank) || rank < 1 || rank > 99) throw new Error("优先级要是 1 到 99");
  clearRows.value = clearRows.value.concat([{ uid, rank: String(rank) }]);
  prioUid.value = "";
}

async function saveClearPlan() {
  err.value = "";
  clearNote.value = "";
  const data = await api("/api/clear-plan", {
    mode: clearMode.value,
    page_from: clearFrom.value,
    page_to: clearTo.value,
    wait_min: clearWait.value,
    scan_sec: clearScan.value,
    priority: clearRows.value.map((row) => ({ uid: row.uid, rank: row.rank })),
  });
  if (data.user) takeUser(data.user);
  clearNote.value = "已保存";
}

async function saveLockCards() {
  err.value = "";
  lockNote.value = "";
  const data = await api("/api/lock-cards", { cards: lockCards.value });
  if (data.user) takeUser(data.user);
  lockNote.value = "已保存";
}

async function saveRetreat() {
  err.value = "";
  retreatNote.value = "";
  const data = await api("/api/retreat", {
    mode: retreatMode.value,
    hops: retreatHops.value,
    city_id: retreatCity.value,
  });
  if (data.user) takeUser(data.user);
  pinRetreatCity();
  retreatNote.value = "已保存";
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

async function moveToCity() {
  err.value = "";
  if (!moveCity.value) throw new Error("要选移动到哪座城");
  const data = await api("/api/attack-move", { city_id: moveCity.value });
  if (proc.value) proc.value.move_note = data.move_note || "";
  await refreshAttacks();
}

async function addModo() {
  err.value = "";
  const data = await api("/api/modo", { cards: modoCards.value });
  if (data.modo_cards != null) modoCards.value = String(data.modo_cards);
  err.value = attackLoginError(data.login);
  await refreshAttacks();
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
  if (it.kind === "modo") return "摩多军团";
  return it.city_name ? it.city_id + " " + it.city_name : String(it.city_id ?? "");
}

function orderUid(it) {
  if (it && it.kind === "modo") return "首都周边";
  return (it && it.uid) || "整座城";
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
  if (!text || it.status === "pending" || it.status === "running" || it.status === "wait") return "—";
  const finished = orders.value.find((row) => row.status !== "pending" && row.status !== "running" && row.status !== "wait");
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
  if (login === "taken") return "这个攻打 QQ 已经绑定别的登录账号，不能接着用";
  if (login === "unbound") return "这个登录账号还没绑定攻打号";
  return "";
}

function orderStatus(status) {
  return { pending: "排队", running: "正在打", blocked: "等通路", wait: "等空城", done: "已打完", failed: "没打成", ended: "已结束" }[status] || status;
}

function orderOpen(it) {
  return !!it && (it.status === "pending" || it.status === "running" || it.status === "blocked" || it.status === "wait");
}

async function cancelOrder(it) {
  err.value = "";
  await api("/api/attacks/cancel", { id: it.id });
  await refreshAttacks();
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
  cityCounts.value = [];
  orders.value = [];
  storms.value = [];
  proc.value = null;
  qrSrc.value = "";
}

function statusOf(it) {
  if (it.present) return "在城里";
  if (it.checked) return "不在这座城";
  return "这座城还没扫过";
}

function whenOf(it) {
  if (it.present) return it.seen_at || "—";
  if (it.checked) return it.city_scanned_at || "—";
  return "—";
}

function cityText(it) {
  return it.city_name ? it.city_name + " " + it.city_id : String(it.city_id);
}

const prioShown = computed(() =>
  clearRows.value
    .map((row, i) => ({ row, i }))
    .sort((a, b) => (Number(a.row.rank) || 99) - (Number(b.row.rank) || 99) || a.i - b.i)
    .map((item) => item.row)
);

const players = computed(() => {
  const order = [];
  const map = {};
  for (const it of items.value) {
    let group = map[it.uid];
    if (!group) {
      group = { uid: it.uid, name: "", present: false, cities: [] };
      map[it.uid] = group;
      order.push(group);
    }
    if (!group.name && it.name) group.name = it.name;
    if (it.present) group.present = true;
    group.cities.push(it);
  }
  return order;
});

function isSubOpen(uid) {
  const saved = subOpen.value[uid];
  return saved == null ? true : saved;
}

function onSubToggle(uid, ev) {
  subOpen.value = { ...subOpen.value, [uid]: ev.target.open };
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
    <h1>坦克风暴AI助手</h1>
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
        <a v-if="me.admin" class="ghost" href="/admin">管理</a>
        <button type="button" class="ghost" @click="logout">退出</button>
      </p>
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
        <button type="button" class="ghost" @click="advanced = !advanced">{{ advanced ? "收起" : "高级配置" }}</button>
        <span class="muted">{{ autoLock ? "已打开。订阅的人在城里就排队攻打，打开时人已经在的，马上排一条。" : "已关闭。" }}这一单没打完就跳过，等这个人下次再出现才排。同一个人一直在城里，不会重复排。</span>
      </p>
      <div v-if="me.remote_attack && advanced" class="advanced">
        <form class="lock-row" @submit.prevent="saveLockCards().catch((e) => (err = e.message))">
          <label>1小时内最多恢复卡<input v-model="lockCards" class="mins" inputmode="numeric" required /></label>
          <button type="submit">保存</button>
          <span class="muted">{{ lockNote }}</span>
        </form>
        <p class="muted">只限制自动锁敌。最近 1 小时里最多开这么多张，用满就不再开，这一单跳过。手动攻打不占这个数。</p>
        <form class="lock-row retreat-row" @submit.prevent="saveRetreat().catch((e) => (err = e.message))">
          <span class="switch">打完后退</span>
          <label class="choice"><input type="radio" value="off" v-model="retreatMode" />不后退</label>
          <label class="choice"><input type="radio" value="hops" v-model="retreatMode" />后退几座城</label>
          <label class="choice"><input type="radio" value="city" v-model="retreatMode" />退到指定城市</label>
          <label v-if="retreatMode === 'hops'">座数<input v-model="retreatHops" class="mins" inputmode="numeric" required /></label>
          <label v-if="retreatMode !== 'off'">{{ retreatMode === 'hops' ? '朝向' : '退到' }}
            <select v-model="retreatCity" :required="retreatMode === 'city'">
              <option v-if="retreatMode === 'city'" value="0" disabled>选择城市</option>
              <option v-if="retreatMode === 'hops' && !cities.some((c) => c.name === '马奇诺')" value="0">马奇诺</option>
              <option v-for="c in cities" :key="'r' + c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
            </select>
          </label>
          <button type="submit">保存</button>
          <span class="muted">{{ retreatNote }}</span>
        </form>
        <p class="muted">只对自动锁敌打完的订单。后退几座城是朝所选城市走这么远就停，不走进终点。退到指定城市是走进那座城，不打它。两种走法只能选一种。</p>
      </div>
      <p v-if="!items.length" class="muted">还没有订阅。</p>
      <div v-else class="subs">
        <details v-for="p in players" :key="p.uid" class="sub" :open="isSubOpen(p.uid)" @toggle="onSubToggle(p.uid, $event)">
          <summary>
            <span class="sub-name">{{ p.name || p.uid }}</span>
            <span v-if="p.name" class="muted">{{ p.uid }}</span>
            <span v-if="p.present" class="on">在城里</span>
            <span class="muted">{{ p.cities.length }} 座城</span>
          </summary>
          <div v-for="it in p.cities" :key="it.city_id" class="sub-city">
            <span class="sub-where">{{ cityText(it) }}</span>
            <span class="sub-status" :class="it.present ? 'on' : 'off'">{{ statusOf(it) }}<template v-if="it.present && it.page != null"> · 第{{ it.page }}页</template></span>
            <span class="sub-time muted">{{ whenOf(it) }}</span>
            <button type="button" class="ghost" @click="removeSub(it)">取消</button>
          </div>
        </details>
      </div>
      <h2>城市人数</h2>
      <p class="muted">订阅过的城。人数是最近一次扫描打开面板时的玩家数量，同一座城只列一行。</p>
      <p v-if="!cityCounts.length" class="muted">还没有订阅的城市。</p>
      <div v-else class="wide">
        <table>
          <thead>
            <tr><th>城市</th><th>人数</th><th>扫描时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="it in cityCounts" :key="it.city_id">
              <td>{{ cityText(it) }}</td>
              <td>{{ it.user_cnt == null ? "—" : it.user_cnt }}</td>
              <td>{{ it.scanned_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h2>远程扫码攻打</h2>
      <p v-if="!me.remote_attack" class="muted">当前是{{ me.tier || "初级" }}。可以刷本国首都旁边的两座摩多军团。打人和清城要中级或高级。</p>
      <template v-if="me.remote_attack">
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
          <button type="button" class="ghost" @click="attackAdvanced = !attackAdvanced">{{ attackAdvanced ? "收起" : "高级配置" }}</button>
        </p>
        <div v-if="attackAdvanced" class="advanced">
          <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => (err = e.message))">
            <span class="switch">清城扫页</span>
            <label class="choice"><input type="radio" value="head" v-model="clearMode" />前5页</label>
            <label class="choice"><input type="radio" value="range" v-model="clearMode" />指定范围</label>
            <template v-if="clearMode === 'range'">
              <label>从<input v-model="clearFrom" class="mins" inputmode="numeric" required /></label>
              <label>到<input v-model="clearTo" class="mins" inputmode="numeric" required /></label>
            </template>
          </form>
          <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => (err = e.message))">
            <span class="switch">空城再打</span>
            <label class="choice">分钟<input v-model="clearWait" class="mins" inputmode="numeric" required /></label>
          </form>
          <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => (err = e.message))">
            <span class="switch">扫页冷却</span>
            <label class="choice">秒<input v-model="clearScan" class="mins" inputmode="numeric" required /></label>
          </form>
          <form class="lock-row" @submit.prevent="addPriority().catch((e) => (err = e.message))">
            <span class="switch">优先 UID</span>
            <label>UID<input v-model="prioUid" inputmode="numeric" /></label>
            <label>优先级<input v-model="prioRank" class="mins" inputmode="numeric" /></label>
            <button type="submit">添加</button>
          </form>
          <div v-for="(row, i) in prioShown" :key="row.uid" class="prio-row">
            <span class="muted">{{ i + 1 }}</span>
            <span>{{ row.uid }}</span>
            <label>优先级<input v-model="row.rank" class="mins" inputmode="numeric" /></label>
            <button type="button" class="ghost" @click="clearRows = clearRows.filter((item) => item.uid !== row.uid)">删除</button>
          </div>
          <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => (err = e.message))">
            <button type="submit">保存</button>
            <span class="muted">{{ clearNote }}</span>
          </form>
          <p class="muted">只对留空 UID 的清城。攻打号自己扫这些页。优先名单里数字小的先打，同一级按扫到的先后。名单以外的人排在后面，再往后的页不打。扫页冷却是两次扫页至少隔开的秒数，填 0 表示每次出手后的冷却都扫。到点就在那次冷却里再扫，新上来的人按同样的顺序接着打。最多 50 个 UID。这几页没人时，过上面的分钟再启动同一条订单。0 表示空了就结束。</p>
        </div>
      </template>
        <form class="lock-row" @submit.prevent="addModo().catch((e) => (err = e.message))">
          <span class="switch">刷摩多军团</span>
          <label class="choice">恢复卡<input v-model="modoCards" class="mins" inputmode="numeric" required /></label>
          <button type="submit">提交</button>
        </form>
        <p class="muted">按攻打号的国家，去首都旁边两座摩多军团。先走进那座城，召唤支援兵，再打。这两座共用这么多张恢复卡，先打的那座最多用一半。0 表示不用卡，行动力不够就停。</p>
        <p>
          <button type="button" class="ghost" @click="pushLogin().catch((e) => (err = e.message))">推送登录二维码</button>
        </p>
        <p class="proc">
          <span class="proc-text">攻打 QQ {{ proc && proc.qq ? proc.qq : "还没绑定" }}：{{ procText(proc) }}<template v-if="proc && proc.online && proc.seen_at && !proc.paused"> · {{ proc.seen_at }}</template></span>
          <button v-if="proc && proc.online && proc.paused && proc.phase !== 'login'" type="button" class="ghost" @click="setAttackPause(false).catch((e) => (err = e.message))">继续</button>
          <button v-else-if="proc && proc.online" type="button" class="ghost" @click="setAttackPause(true).catch((e) => (err = e.message))">暂停</button>
        </p>
        <p v-if="proc && proc.online" class="proc here-row">
          <span v-if="proc.here">人在 {{ proc.here }}</span>
          <template v-if="me.remote_attack">
          <label class="choice">移动到
            <select v-model="moveCity">
              <option value="" disabled>选择城市</option>
              <option v-for="c in cities" :key="'mv' + c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
            </select>
          </label>
          <button type="button" @click="moveToCity().catch((e) => (err = e.message))">移动</button>
          <span v-if="proc.move_note" class="muted">{{ proc.move_note }}</span>
          </template>
        </p>
        <div v-if="storms.length" class="storms">
          <p class="muted">最近 1 小时拒绝的超级强攻</p>
          <p v-for="(s, i) in storms" :key="i">{{ s.at }} · {{ s.name }}</p>
        </div>
        <p class="muted">每 5 秒刷新一次。每个攻打 QQ 各有一条线程，状态按 QQ 号分开。还没打完的最多两条，下面最多显示三条。还没绑定的，点推送登录会在下面出二维码。同一个 QQ 不能绑给两个登录账号。</p>
        <img v-if="qrSrc" class="qr" :src="qrSrc" alt="攻打号登录二维码" @error="reloadQr" />
        <div class="orders" v-if="orders.length">
        <table>
          <thead>
            <tr><th>城市</th><th>UID</th><th>击退敌方数量</th><th>状态</th><th>说明</th><th>保活剩余倒计时</th><th>北京时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="it in orders" :key="it.id">
              <td>{{ orderCity(it) }}</td>
              <td>{{ orderUid(it) }}</td>
              <td>{{ beatText(it) }}</td>
              <td>{{ orderStatus(it.status) }}<button v-if="orderOpen(it)" type="button" class="ghost" @click="cancelOrder(it).catch((e) => (err = e.message))">关停</button></td>
              <td class="reason">{{ it.reason || "—" }}</td>
              <td>{{ holdCell(it) }}</td>
              <td>{{ it.created_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
        <div class="order-cards">
          <article class="order-card" v-for="it in orders" :key="'c' + it.id">
            <p><span class="k">城市</span>{{ orderCity(it) }}</p>
            <p><span class="k">UID</span>{{ orderUid(it) }}</p>
            <p><span class="k">击退敌方数量</span>{{ beatText(it) }}</p>
            <p><span class="k">状态</span>{{ orderStatus(it.status) }}</p>
            <p class="reason"><span class="k">说明</span>{{ it.reason || "—" }}</p>
            <p><span class="k">保活剩余倒计时</span>{{ holdCell(it) }}</p>
            <p><span class="k">北京时间</span>{{ it.created_at || "—" }}</p>
            <p v-if="orderOpen(it)"><button type="button" class="ghost" @click="cancelOrder(it).catch((e) => (err = e.message))">关停</button></p>
          </article>
        </div>
        </div>
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

