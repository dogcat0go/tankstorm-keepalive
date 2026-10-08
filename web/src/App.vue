<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMenu, ElMenuItem } from "element-plus";
import "element-plus/es/components/menu/style/css";
import "./base.css";
import AccountBar from "./pages/AccountBar.vue";
import AttackStatus from "./pages/AttackStatus.vue";
import AttackPage from "./pages/AttackPage.vue";
import DailyPage from "./pages/DailyPage.vue";
import WatchPage from "./pages/WatchPage.vue";
import QqPage from "./pages/QqPage.vue";
import ProfilePage from "./pages/ProfilePage.vue";

const me = ref(null);
const mode = ref("login");
const username = ref("");
const password = ref("");
const invite = ref("");
const err = ref("");
const errAt = ref("");
const notice = ref(null);
const items = ref([]);
const cityCounts = ref([]);
const subOpen = ref({});
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
const bindWait = ref(false);
const devLogin = ref(false);
const registerOpen = ref(false);
const autoLock = ref(false);
const holdMin = ref("0");
const holdAll = ref(false);
const holdAllBusy = ref(false);
const holdNote = ref("");
const cardMax = ref("100");
const retreatMode = ref("hops");
const retreatHops = ref("3");
const retreatCity = ref("0");
const retreatFail = ref(false);
const retreatNote = ref("");
const lockCards = ref("3");
const lockHint = ref("");
const locking = ref("");
const lockNote = ref("");
const advanced = ref(false);
const attackAdvanced = ref(false);
const clearMode = ref("head");
const clearFrom = ref("1");
const clearTo = ref("5");
const clearWait = ref("0");
const clearScan = ref("0");
const clearRetreatMode = ref("off");
const clearRetreatHops = ref("3");
const clearRetreatCity = ref("0");
const clearRetreatFail = ref(false);
const clearRows = ref([]);
const clearNote = ref("");
const prioUid = ref("");
const prioRank = ref("1");
const modoCards = ref("0");
const tab = ref("attack");
const dailyQq = ref("");
const dailyTasks = ref([]);
const campaignTasks = computed(() => dailyTasks.value.filter((it) => it.extra));
const progressTasks = computed(() => dailyTasks.value.filter((it) => !it.extra));
const dailyJobs = ref([]);
const pveStages = ref("");
let pveStagesReady = false;
const fundBuilding = ref("");
const fundTimes = ref("1");
const fundBuildings = [
  { id: "10132", name: "比萨斜塔" },
  { id: "10133", name: "埃菲尔铁塔" },
  { id: "10134", name: "大本钟" },
  { id: "10135", name: "女神像" },
  { id: "10136", name: "红场" },
  { id: "10137", name: "帝国大厦" },
  { id: "10138", name: "万磁陀螺" },
  { id: "10139", name: "英雄徽章雕塑" },
];
const dailyNote = ref("");
const dailyAt = ref("");
const dailyAtNote = ref("");
const dailySwitching = ref("");
let dailyAtReady = false;
const moveCity = ref("");
const holdUntil = ref(0);
const clock = ref(Date.now());
let timer = 0;
let atkTimer = 0;
let clockTimer = 0;

function showErr(message, where) {
  const text = message || "";
  err.value = text;
  errAt.value = text ? (where || (me.value ? tab.value : "login")) : "";
}

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
  if (me.value && data.region != null) {
    me.value = { ...me.value, region: Number(data.region) || 0 };
  }
}

async function refreshDaily() {
  if (!me.value) {
    dailyQq.value = "";
    dailyTasks.value = [];
    dailyJobs.value = [];
    pveStages.value = "";
    pveStagesReady = false;
    dailyAt.value = "";
    dailyAtNote.value = "";
    dailyAtReady = false;
    return;
  }
  const data = await api("/api/daily");
  dailyQq.value = data.qq || "";
  dailyTasks.value = data.tasks || [];
  dailyJobs.value = data.jobs || [];
  if (!pveStagesReady) {
    pveStages.value = data.stages || "";
    pveStagesReady = true;
  }
  if (!dailyAtReady) {
    dailyAt.value = data.schedule || "";
    dailyAtReady = true;
    dailyAtNote.value = data.schedule ? "已设定每天 " + data.schedule : "";
  }
}

async function saveDailySwitch(it, on) {
  const prev = !!it.on;
  const next = !!on;
  if (next === prev || dailySwitching.value) return;
  it.on = next;
  dailySwitching.value = it.key;
  showErr("");
  try {
    const data = await api("/api/daily/switch", { key: it.key, on: next });
    dailyTasks.value = data.tasks || [];
  } catch (e) {
    it.on = prev;
    showErr(e.message);
  } finally {
    dailySwitching.value = "";
  }
}

async function saveDailyAt() {
  showErr("");
  dailyAtNote.value = "";
  const data = await api("/api/daily/schedule", { at: dailyAt.value });
  dailyAt.value = data.schedule || "";
  dailyAtReady = true;
  dailyAtNote.value = data.schedule ? "已设定每天 " + data.schedule : "已关掉定时";
}

async function runDaily(kind, extra) {
  showErr("");
  dailyNote.value = "";
  await api("/api/daily", Object.assign({ kind }, extra || {}));
  dailyNote.value = "已交给绑定的攻打号";
  await refreshDaily();
  await refreshAttacks();
}

function pickTab(key) {
  tab.value = key;
  if (key === "daily") refreshDaily().catch((e) => showErr(e.message, "daily"));
}

function openProfile() {
  tab.value = tab.value === "profile" && !toolsOn.value ? "attack" : "profile";
}

function openQq() {
  tab.value = tab.value === "qq" && !toolsOn.value ? "attack" : "qq";
}

const sideOn = computed(() => tab.value === "profile" || tab.value === "qq");
const toolsOn = computed(() => {
  const p = proc.value;
  if (!p || !p.online || p.need_login || p.phase === "login") return false;
  if (!p.keepalive && String(p.detail || "").startsWith("登录")) return false;
  return true;
});

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
  const wantQr = !!(proc.value && proc.value.qr);
  if (proc.value && (proc.value.qq || proc.value.qr)) bindWait.value = false;
  if (!wantQr) qrSrc.value = "";
  else if (proc.value.qr_at) {
    const next = "/api/attack-qr?t=" + encodeURIComponent(proc.value.qr_at);
    if (qrSrc.value !== next) qrSrc.value = next;
  } else if (!qrSrc.value) qrSrc.value = "/api/attack-qr?t=" + Date.now();
  if (wantQr) qrWait = 0;
}

let qrWait = 0;
function reloadQr() {
  if (!qrSrc.value || qrWait >= 12) return;
  qrWait += 1;
  setTimeout(() => {
    if (qrSrc.value) qrSrc.value = "/api/attack-qr?t=" + Date.now();
  }, 1000);
}

function pushLoginVisible(p) {
  if (p && p.need_login) return true;
  if (p && p.online && !p.keepalive && p.phase !== "login") return true;
  return !(p && p.online && p.phase !== "login");
}

function procText(p) {
  if (!p) return "没在跑";
  if (!p.online) return p.paused && p.detail ? p.detail : "没在跑";
  if (p.paused && p.phase !== "login") return p.detail || "已暂停";
  return p.detail || "空闲，等订单";
}

async function setAttackPause(on) {
  showErr("", "bar");
  const data = await api("/api/attack-pause", { on });
  if (proc.value) proc.value.paused = !!data.paused;
  await refreshAttacks();
}

async function enter() {
  showErr("");
  const path = mode.value === "register" && registerOpen.value ? "/api/register" : "/api/login";
  await api(path, {
    username: username.value.trim(),
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
    showErr(e.message);
  }
}

function cityLabel(c) {
  const same = cities.value.filter((x) => x.name === c.name).length;
  return same > 1 ? c.name + " " + c.id : c.name;
}

function hereText(p) {
  if (!p) return "";
  const bits = [];
  if (p.here) bits.push("目前在 " + p.here);
  if (p.morale != null) bits.push("士气 " + p.morale);
  if (p.power != null) bits.push("体力 " + p.power);
  return bits.join(" · ");
}

async function devEnter() {
  showErr("");
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
  pveStagesReady = false;
  dailyAtReady = false;
  qqTarget.value = user.qq_target || "";
  autoLock.value = !!user.auto_lock;
  holdMin.value = String(user.hold_min ?? 0);
  holdAll.value = !!user.hold_all;
  cardMax.value = String(user.card_max ?? 100);
  const picked = user.retreat_mode;
  retreatMode.value = picked === "off" || picked === "city" || picked === "hops" ? picked : "hops";
  retreatHops.value = String(user.retreat_hops ?? 3);
  retreatCity.value = String(user.retreat_city || 0);
  retreatFail.value = !!user.retreat_fail;
  lockCards.value = String(user.lock_cards ?? 3);
  clearMode.value = user.clear_mode === "range" ? "range" : "head";
  clearFrom.value = String(user.clear_from || 1);
  clearTo.value = String(user.clear_to || 5);
  clearWait.value = String(user.clear_wait ?? 0);
  clearScan.value = String(user.clear_scan ?? 0);
  const clearPicked = user.clear_retreat_mode;
  clearRetreatMode.value = clearPicked === "hops" || clearPicked === "city" || clearPicked === "off" ? clearPicked : "off";
  clearRetreatHops.value = String(user.clear_retreat_hops ?? 3);
  clearRetreatCity.value = String(user.clear_retreat_city || 0);
  clearRetreatFail.value = !!user.clear_retreat_fail;
  clearRows.value = (user.clear_priority || []).map((row) => ({
    uid: String(row.uid),
    rank: String(row.rank),
  }));
  modoCards.value = String(user.modo_cards ?? 0);
}

function pinRetreatCity() {
  const hit = cities.value.find((c) => c.name === "马奇诺");
  if (retreatMode.value === "hops" && retreatCity.value === "0" && hit) {
    retreatCity.value = String(hit.id);
  }
  if (clearRetreatMode.value === "hops" && clearRetreatCity.value === "0" && hit) {
    clearRetreatCity.value = String(hit.id);
  }
}

function addPriority() {
  showErr("");
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
  showErr("");
  clearNote.value = "";
  const data = await api("/api/clear-plan", {
    mode: clearMode.value,
    page_from: clearFrom.value,
    page_to: clearTo.value,
    wait_min: clearWait.value,
    scan_sec: clearScan.value,
    retreat_mode: clearRetreatMode.value,
    retreat_hops: clearRetreatHops.value,
    retreat_city: clearRetreatCity.value,
    retreat_fail: clearRetreatFail.value,
    priority: clearRows.value.map((row) => ({ uid: row.uid, rank: row.rank })),
  });
  if (data.user) takeUser(data.user);
  pinRetreatCity();
  clearNote.value = "已保存";
}

async function saveLockCards() {
  showErr("");
  lockNote.value = "";
  const data = await api("/api/lock-cards", { cards: lockCards.value });
  if (data.user) takeUser(data.user);
  lockNote.value = "已保存";
}

async function saveRetreat() {
  showErr("");
  retreatNote.value = "";
  const data = await api("/api/retreat", {
    mode: retreatMode.value,
    hops: retreatHops.value,
    city_id: retreatCity.value,
    on_fail: retreatFail.value,
  });
  if (data.user) takeUser(data.user);
  pinRetreatCity();
  retreatNote.value = "已保存";
}

async function saveAutoLock(ev) {
  const on = ev.target.checked;
  showErr("");
  try {
    const data = await api("/api/auto-lock", { on });
    autoLock.value = !!data.auto_lock;
    showErr(attackLoginError(data.login));
    await refreshAttacks();
  } catch (e) {
    ev.target.checked = autoLock.value;
    showErr(e.message);
  }
}

function lockKey(it) {
  return it.city_id + ":" + it.uid;
}

async function lockOne(it) {
  const key = lockKey(it);
  if (locking.value) return;
  locking.value = key;
  showErr("", "watch");
  lockHint.value = "";
  try {
    const data = await api("/api/watch-lock", { city_id: it.city_id, uid: it.uid });
    const where = it.city_name ? it.city_name + " " + it.city_id : String(it.city_id);
    lockHint.value = where + " 已排队索敌";
    showErr(attackLoginError(data.login), "watch");
    await refresh();
    await refreshAttacks();
  } finally {
    locking.value = "";
  }
}

async function addSub() {
  showErr("");
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
  showErr("");
  if (!moveCity.value) throw new Error("要选移动到哪座城");
  const data = await api("/api/attack-move", { city_id: moveCity.value });
  if (proc.value) proc.value.move_note = data.move_note || "";
  await refreshAttacks();
}

async function addModo() {
  showErr("", "daily");
  try {
    const data = await api("/api/modo", { cards: modoCards.value });
    if (data.modo_cards != null) modoCards.value = String(data.modo_cards);
    const extra = submitNote(data);
    notice.value = {
      ok: true,
      title: "已提交到国战助手",
      text: "刷摩多军团的订单和进度都在国战助手。" + extra,
    };
    pickTab("attack");
    await refreshAttacks();
  } catch (e) {
    showErr(e.message, "daily");
    notice.value = { ok: false, title: "提交没成功", text: e.message || "请求失败" };
  }
}

async function saveHold() {
  showErr("", "hold");
  holdNote.value = "";
  const data = await api("/api/attack-hold", { minutes: holdMin.value });
  if (data.hold_min != null) {
    holdMin.value = String(data.hold_min);
    if (me.value) me.value.hold_min = data.hold_min;
  }
  holdNote.value = "已保存";
}

async function saveHoldAll(on) {
  showErr("", "hold");
  holdNote.value = "";
  holdAllBusy.value = true;
  try {
    const data = await api("/api/attack-hold", { all: !!on });
    holdAll.value = !!data.hold_all;
    if (me.value) me.value.hold_all = holdAll.value;
    holdNote.value = "已保存";
  } catch (e) {
    showErr(e.message, "hold");
  } finally {
    holdAllBusy.value = false;
  }
}

async function addOrder() {
  showErr("");
  const city = cities.value.find((c) => String(c.id) === String(attackCity.value));
  const where = city ? cityLabel(city) : String(attackCity.value || "");
  const who = String(attackUid.value || "").trim() || "整座城";
  try {
    const data = await api("/api/attacks", {
      city_id: attackCity.value,
      uid: attackUid.value,
      cards: cardMax.value,
    });
    attackCity.value = "";
    attackUid.value = "";
    if (data.hold_min != null) holdMin.value = String(data.hold_min);
    if (data.card_max != null) cardMax.value = String(data.card_max);
    const extra = submitNote(data);
    showErr(extra);
    notice.value = {
      ok: true,
      title: "提交成功",
      text: "已提交 " + where + "，" + who + "。" + extra,
    };
  } catch (e) {
    showErr(e.message);
    notice.value = { ok: false, title: "提交没成功", text: e.message || "请求失败" };
    return;
  }
  try {
    await refreshAttacks();
  } catch (e) {
    showErr(e.message || err.value);
  }
}

function beatText(it) {
  if (!it || it.beats == null) return "—";
  const n = Number(it.beats);
  if (!Number.isFinite(n)) return "—";
  if (it.kind === "modo") return String(Math.floor(n / 4));
  return String(Math.trunc(n));
}

function orderCity(it) {
  if (!it) return "";
  if (it.kind === "modo") return "摩多军团";
  return it.city_name ? it.city_id + " " + it.city_name : String(it.city_id ?? "");
}

function orderUid(it) {
  if (it && it.kind === "modo") return "首都周边";
  if (it && it.name) return it.name;
  return (it && it.uid) || "整座城";
}

function formatClock(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
}

function holdClock() {
  if (!holdUntil.value) return "";
  const sec = Math.max(0, Math.round((holdUntil.value - clock.value) / 1000));
  return formatClock(sec);
}

function waitClock(it) {
  if (!it || it.status !== "wait" || !it.run_at) return "";
  const due = Date.parse(it.run_at);
  if (!Number.isFinite(due)) return "";
  const sec = Math.max(0, Math.round((due - clock.value) / 1000));
  return formatClock(sec);
}

const clearRetryText = computed(() => {
  const row = orders.value.find((it) => it.status === "wait" && it.run_at);
  if (!row) return "";
  const left = waitClock(row);
  return left ? "下一轮还有 " + left : "";
});

function holdCell(it) {
  if (it && it.status === "wait") return waitClock(it) || "—";
  const text = holdClock();
  if (!text || it.status === "pending" || it.status === "running") return "—";
  const finished = orders.value.find((row) => row.status !== "pending" && row.status !== "running" && row.status !== "wait");
  return finished && finished.id === it.id ? text : "—";
}

async function pushLogin() {
  showErr("", "bar");
  const data = await api("/api/attack-login", {});
  if (data.login === "qr" && !(proc.value && proc.value.qq)) bindWait.value = true;
  showErr(attackLoginError(data.login), "bar");
  await refreshAttacks();
}

function attackLoginError(login) {
  if (login === "no_account") return "服务器还没配置攻打号，二维码发不出去";
  if (login === "taken") return "这个攻打 QQ 已经绑定别的登录账号，不能接着用";
  if (login === "unbound") return "这个登录账号还没绑定攻打号";
  return "";
}

function submitNote(data) {
  let text = "";
  if (data && data.resumed) text += "攻打已继续。";
  if (data && data.scan === "page") text += "请扫页面上的二维码登录。";
  else if (data && data.scan === "away") text += "请扫码登录。";
  else if (data && data.scan === "mismatch") text += "请用绑定的 QQ 扫码登录。";
  else text += attackLoginError(data && data.login);
  return text;
}

function orderStatus(status) {
  return { pending: "排队", running: "正在打", blocked: "等通路", wait: "等再打", done: "已打完", failed: "没打成", ended: "已结束" }[status] || status;
}

function orderOpen(it) {
  return !!it && (it.status === "pending" || it.status === "running" || it.status === "blocked" || it.status === "wait");
}

async function cancelOrder(it) {
  showErr("");
  await api("/api/attacks/cancel", { id: it.id });
  await refreshAttacks();
}

async function cancelDaily(it) {
  showErr("", "daily");
  await api("/api/daily/cancel", { id: it.id });
  await refreshDaily();
}

async function savePush() {
  note.value = "";
  showErr("");
  await api("/api/push", { qq_target: qqTarget.value });
  note.value = "QQ 号已保存";
}

async function savePassword(current, password) {
  showErr("", "pwd");
  await api("/api/password", { current, password });
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
  bindWait.value = false;
  notice.value = null;
  showErr("");
  tab.value = "attack";
  dailyQq.value = "";
  dailyTasks.value = [];
  dailyJobs.value = [];
  pveStages.value = "";
  pveStagesReady = false;
  dailyAt.value = "";
  dailyAtNote.value = "";
  dailyAtReady = false;
  holdNote.value = "";
  dailyNote.value = "";
  lockHint.value = "";
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
    if (!me.value) return;
    refreshAttacks().catch(() => {});
    if (tab.value === "daily") refreshDaily().catch(() => {});
  }, 2000);
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
    <form v-if="!me" @submit.prevent="enter().catch((e) => showErr(e.message))">
      <label>用户名<input v-model.trim="username" autocomplete="username" required /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
      <label v-if="mode === 'register'">注册口令<input v-model="invite" autocomplete="off" /></label>
      <button type="submit">{{ mode === "register" && registerOpen ? "注册" : "登录" }}</button>
      <button v-if="registerOpen" type="button" class="ghost" @click="mode = mode === 'login' ? 'register' : 'login'">
        {{ mode === "login" ? "去注册" : "去登录" }}
      </button>
      <button v-if="devLogin" type="button" class="ghost" @click="devEnter().catch((e) => showErr(e.message))">
        测试进入
      </button>
    </form>
    <p v-if="!me && err && errAt === 'login'" class="err">{{ err }}</p>
    <template v-if="me">
      <AccountBar
        v-model:hold-min="holdMin"
        :me="me"
        :hold-note="holdNote"
        :err="err"
        :err-at="errAt"
        :save-hold="saveHold"
        :hold-all="holdAll"
        :hold-all-busy="holdAllBusy"
        :save-hold-all="saveHoldAll"
        :open-profile="openProfile"
        :profile-on="tab === 'profile'"
        :open-qq="openQq"
        :qq-on="tab === 'qq'"
        :logout="logout"
        :show-err="showErr"
      />
      <AttackStatus
        v-show="!sideOn"
        :proc="proc"
        :qr-src="qrSrc"
        :bind-wait="bindWait"
        :err="err"
        :err-at="errAt"
        :push-login-visible="pushLoginVisible"
        :proc-text="procText"
        :push-login="pushLogin"
        :set-attack-pause="setAttackPause"
        :reload-qr="reloadQr"
        :show-err="showErr"
      />
      <el-menu v-if="toolsOn" class="page-nav" mode="horizontal" :ellipsis="false" :default-active="sideOn ? '' : tab" :key="tab" aria-label="功能" @select="pickTab">
        <el-menu-item index="attack">国战助手</el-menu-item>
        <el-menu-item index="daily">日常任务</el-menu-item>
        <el-menu-item index="watch">监控敌人</el-menu-item>
      </el-menu>
      <ProfilePage
        v-show="tab === 'profile'"
        :me="me"
        :err="err"
        :err-at="errAt"
        :save-password="savePassword"
        :show-err="showErr"
      />
      <DailyPage
        v-show="toolsOn && tab === 'daily'"
        v-model:daily-at="dailyAt"
        v-model:pve-stages="pveStages"
        v-model:fund-building="fundBuilding"
        v-model:fund-times="fundTimes"
        v-model:modo-cards="modoCards"
        :daily-qq="dailyQq"
        :campaign-tasks="campaignTasks"
        :progress-tasks="progressTasks"
        :daily-jobs="dailyJobs"
        :daily-at-note="dailyAtNote"
        :daily-note="dailyNote"
        :daily-switching="dailySwitching"
        :fund-buildings="fundBuildings"
        :err="err"
        :err-at="errAt"
        :proc="proc"
        :run-daily="runDaily"
        :cancel-daily="cancelDaily"
        :save-daily-at="saveDailyAt"
        :save-daily-switch="saveDailySwitch"
        :add-modo="addModo"
        :show-err="showErr"
      />
      <QqPage
        v-show="tab === 'qq'"
        v-model:qq-target="qqTarget"
        :err="err"
        :err-at="errAt"
        :note="note"
        :save-push="savePush"
        :show-err="showErr"
      />
      <WatchPage
        v-show="toolsOn && tab === 'watch'"
        v-model:city-id="cityId"
        v-model:uid="uid"
        v-model:lock-cards="lockCards"
        v-model:retreat-mode="retreatMode"
        v-model:retreat-hops="retreatHops"
        v-model:retreat-city="retreatCity"
        v-model:retreat-fail="retreatFail"
        v-model:advanced="advanced"
        :me="me"
        :cities="cities"
        :auto-lock="autoLock"
        :err="err"
        :err-at="errAt"
        :lock-note="lockNote"
        :retreat-note="retreatNote"
        :lock-hint="lockHint"
        :items="items"
        :players="players"
        :locking="locking"
        :city-label="cityLabel"
        :add-sub="addSub"
        :save-auto-lock="saveAutoLock"
        :save-lock-cards="saveLockCards"
        :save-retreat="saveRetreat"
        :lock-one="lockOne"
        :remove-sub="removeSub"
        :lock-key="lockKey"
        :is-sub-open="isSubOpen"
        :on-sub-toggle="onSubToggle"
        :status-of="statusOf"
        :when-of="whenOf"
        :city-text="cityText"
        :show-err="showErr"
      />
      <AttackPage
        v-show="toolsOn && tab === 'attack'"
        v-model:attack-city="attackCity"
        v-model:attack-uid="attackUid"
        v-model:card-max="cardMax"
        v-model:attack-advanced="attackAdvanced"
        v-model:clear-mode="clearMode"
        v-model:clear-from="clearFrom"
        v-model:clear-to="clearTo"
        v-model:clear-wait="clearWait"
        v-model:clear-scan="clearScan"
        v-model:clear-retreat-mode="clearRetreatMode"
        v-model:clear-retreat-hops="clearRetreatHops"
        v-model:clear-retreat-city="clearRetreatCity"
        v-model:clear-retreat-fail="clearRetreatFail"
        v-model:prio-uid="prioUid"
        v-model:prio-rank="prioRank"
        v-model:move-city="moveCity"
        v-model:clear-rows="clearRows"
        :me="me"
        :proc="proc"
        :cities="cities"
        :orders="orders"
        :storms="storms"
        :city-counts="cityCounts"
        :err="err"
        :err-at="errAt"
        :clear-note="clearNote"
        :clear-retry-text="clearRetryText"
        :prio-shown="prioShown"
        :city-label="cityLabel"
        :here-text="hereText"
        :city-text="cityText"
        :add-order="addOrder"
        :save-clear-plan="saveClearPlan"
        :add-priority="addPriority"
        :move-to-city="moveToCity"
        :cancel-order="cancelOrder"
        :beat-text="beatText"
        :order-city="orderCity"
        :order-uid="orderUid"
        :order-status="orderStatus"
        :order-open="orderOpen"
        :hold-cell="holdCell"
        :show-err="showErr"
      />
    </template>

    <div v-if="notice" class="modal" @click.self="notice = null">
      <div class="modal-card" :class="{ bad: !notice.ok }" role="dialog" aria-modal="true" aria-labelledby="notice-title">
        <h2 id="notice-title">{{ notice.title }}</h2>
        <p>{{ notice.text }}</p>
        <button type="button" @click="notice = null">知道了</button>
      </div>
    </div>
  </main>
</template>

