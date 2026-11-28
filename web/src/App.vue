<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMenu, ElMenuItem } from "element-plus";
import "element-plus/es/components/menu/style/css";
import "./base.css";
import AccountBar from "./pages/AccountBar.vue";
import AttackStatus from "./pages/AttackStatus.vue";
import AttackPage from "./pages/AttackPage.vue";
import DailyPage from "./pages/DailyPage.vue";
import ActivityPage from "./pages/ActivityPage.vue";
import BasePage from "./pages/BasePage.vue";
import WatchPage from "./pages/WatchPage.vue";
import QqPage from "./pages/QqPage.vue";
import ProfilePage from "./pages/ProfilePage.vue";
import MarkdownDoc from "./components/MarkdownDoc.vue";
import SquadPanel from "./components/SquadPanel.vue";
import HiddenMark from "./components/HiddenMark.vue";

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
const command = ref(null);
const commandBusy = ref(false);
const qrSrc = ref("");
const bindWait = ref(false);
const devLogin = ref(false);
const registerOpen = ref(false);
const loginPlatDefault = ref("空间");
const autoLock = ref(false);
const holdMin = ref("0");
const holdAll = ref(false);
const holdAllBusy = ref(false);
const holdNote = ref("");
const offAt = ref("");
const offNote = ref("");
const closeBusy = ref(false);
const cardMax = ref("100");
const retreatMode = ref("hops");
const retreatHops = ref("3");
const retreatCity = ref("0");
const retreatFail = ref(false);
const retreatNote = ref("");
const lockCards = ref("3");
const lockHint = ref("");
const locking = ref("");
const skipSwitching = ref("");
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
const awayMorale = ref("100");
const clearRows = ref([]);
const clearNote = ref("");
const prioUid = ref("");
const prioRank = ref("1");
const prioVip = ref(false);
const prioOfficer = ref(false);
const modoCards = ref("0");
const tab = ref("attack");
const dailyQq = ref("");
const dailyTasks = ref([]);
const campaignTasks = computed(() => dailyTasks.value.filter((it) => it.extra));
const progressTasks = computed(() => dailyTasks.value.filter((it) => !it.extra && it.key !== "公会战役"));
const dailyJobs = ref([]);
const activityJobs = ref([]);
const guildBoard = ref(null);
const guildCatalog = ref([]);
const guildAccept = ref([]);
const guildAcceptNote = ref("");
let guildAcceptReady = false;
const baseCount = ref("1");
const baseArmy = ref("");
const baseFactoryCount = ref("1");
const aaCount = ref("1");
const drinkCount = ref("1");
const admiralCount = ref("1");
const admiral10Count = ref("10");
const agentCount = ref("10");
const boxMidCount = ref("10");
const boxHighCount = ref("10");
const generalCount = ref("1");
const staffCount = ref("1");
const labPickCount = ref("1");
const autoCount = ref("1");
const lab50Count = ref("1");
const lab10Count = ref("1");
const plant50Count = ref("1");
const auto50Count = ref("1");
const spreadX1 = ref("");
const spreadY1 = ref("");
const spreadX2 = ref("");
const spreadY2 = ref("");
const emptyCards = () => ({
  general: null, staff: null, lab: null, plant: null, auto: null, drink: null, agent: null, recruit: null, explore: null,
  box_mid: null, box_high: null, box_mid_key: null, box_high_key: null,
});
const baseCards = ref(emptyCards());
const resourceCatalog = ref([]);
const cleanCatalog = ref([]);
const cleanItems = ref(null);
const resourceCards = ref(null);
const ask = ref("");
let askDone = null;
const guides = ref([]);
const guideErr = ref("");
const guideBusy = ref(false);
const guide = computed(() => guides.value[0] || null);
const guideImages = computed(() => {
  const raw = guide.value && guide.value.images;
  if (!Array.isArray(raw)) return [];
  const text = String((guide.value && guide.value.text) || "");
  return raw.filter((src) => {
    if (typeof src !== "string" || !src.startsWith("/") || src.startsWith("//") || src.includes("://")) return false;
    if (src.split("/").includes("..")) return false;
    const name = src.slice(src.lastIndexOf("/") + 1);
    return !name || !text.includes(name);
  });
});
const baseJobs = ref([]);
const floatAt = ref(null);
const floatFolded = ref(true);
const floatJobs = computed(() => {
  const seen = new Set();
  const base = [];
  for (const it of baseJobs.value) {
    if (seen.has(it.kind)) continue;
    seen.add(it.kind);
    base.push({ ...it, lane: "base" });
  }
  const rows = activityJobs.value.map((it) => ({ ...it, lane: "activity" })).concat(base);
  const rank = (it) => (it.status === "正在做" ? 0 : it.status === "排队" ? 1 : 2);
  return rows.sort((a, b) => rank(a) - rank(b) || b.id - a.id);
});
const floatStyle = computed(() => {
  const at = floatAt.value;
  if (!at) return {};
  return { left: at.x + "px", top: at.y + "px", right: "auto", bottom: "auto" };
});

function clampFloat(x, y, width) {
  const w = width || Math.min(360, window.innerWidth - 24);
  return {
    x: Math.max(8, Math.min(window.innerWidth - w - 8, x)),
    y: Math.max(8, Math.min(window.innerHeight - 36, y)),
  };
}

function dragFloat(ev) {
  if (ev.button != null && ev.button !== 0) return;
  ev.preventDefault();
  const box = ev.currentTarget.parentElement;
  const rect = box.getBoundingClientRect();
  const dx = ev.clientX - rect.left;
  const dy = ev.clientY - rect.top;
  const sx = ev.clientX;
  const sy = ev.clientY;
  let moved = false;
  function move(e) {
    if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) < 6) return;
    moved = true;
    const at = clampFloat(e.clientX - dx, e.clientY - dy, rect.width);
    floatAt.value = at;
  }
  function up() {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    if (!moved && window.matchMedia("(max-width: 720px)").matches) {
      floatFolded.value = !floatFolded.value;
      return;
    }
    try {
      if (moved && floatAt.value) localStorage.setItem("job-float-at", JSON.stringify(floatAt.value));
    } catch (e) { /* 存不住就只留在这一页 */ }
  }
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
const baseNote = ref("");
const baseBlast = ref(null);
const baseMetal = ref(null);
const baseOil = ref(null);
const baseCredit = ref(null);
const basePop = ref(null);
const pveStages = ref("");
let pveStagesReady = false;
const guildLevel = ref("");
const guildLevelNote = ref("");
const guildThew = ref(null);
const guildScore = ref(null);
const strikeLeft = ref(null);
let guildLevelReady = false;
const fundBuilding = ref("");
const fundTimes = ref("1");
const wishTimes = ref("1");
const soulTimes = ref("1");
const trainCount = ref("1");
const skillLevel = ref("4");
const stoneTimes = ref("1");
const fundNote = ref("");
const eggNote = ref("");
const eggLeft = ref(null);
const wargame = ref({ token: null, sites: [], result: "", occupy: null, partial: false, empty: 0 });
const fundBuildings = [
  { id: "10132", name: "比萨斜塔" },
  { id: "10133", name: "埃菲尔铁塔" },
  { id: "10134", name: "大本钟" },
  { id: "10135", name: "女神像" },
  { id: "10137", name: "红场" },
  { id: "10138", name: "帝国大厦" },
  { id: "10144", name: "万磁陀螺" },
];
const dailyNote = ref("");
const dailyAt = ref("");
const dailyAtNote = ref("");
const modoAt = ref("");
const modoAtNote = ref("");
let modoAtReady = false;
const dailySwitching = ref("");
let dailyAtReady = false;
const moveCity = ref("");
const holdUntil = ref(0);
const clock = ref(Date.now());
let timer = 0;
let atkTimer = 0;
let clockTimer = 0;
let seatTimer = 0;
let seatBusy = false;

function confirmUse(text) {
  return new Promise((resolve) => {
    askDone = resolve;
    ask.value = text;
  });
}

function answerAsk(ok) {
  const done = askDone;
  ask.value = "";
  askDone = null;
  if (done) done(!!ok);
}

function cardNum(count) {
  const n = Number(String(count ?? "").trim());
  if (!Number.isInteger(n)) throw new Error("数量要是数字");
  return n;
}

function knownCards(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return Math.trunc(n);
}

function tellShortage(name, have, need) {
  const text = `${name}数量不足，只有 ${have.toLocaleString("zh-CN")} 张，这次要 ${need.toLocaleString("zh-CN")} 张。`;
  showErr(text, "base");
  notice.value = { ok: false, title: "数量不足", text };
}

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
  if (!me.value || !me.value.watch) {
    items.value = [];
    cityCounts.value = [];
    return;
  }
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
    activityJobs.value = [];
    guildBoard.value = null;
    guildCatalog.value = [];
    guildAccept.value = [];
    guildAcceptNote.value = "";
    guildAcceptReady = false;
    pveStages.value = "";
    pveStagesReady = false;
    dailyAt.value = "";
    dailyAtNote.value = "";
    dailyAtReady = false;
    modoAt.value = "";
    modoAtNote.value = "";
    modoAtReady = false;
    guildLevel.value = "";
    guildLevelNote.value = "";
    guildLevelReady = false;
    guildThew.value = null;
    guildScore.value = null;
    strikeLeft.value = null;
    eggLeft.value = null;
    wargame.value = { token: null, sites: [], result: "", occupy: null, partial: false, empty: 0 };
    fundNote.value = "";
    eggNote.value = "";
    return;
  }
  const data = await api("/api/daily");
  dailyQq.value = data.qq || "";
  dailyTasks.value = data.tasks || [];
  dailyJobs.value = data.jobs || [];
  activityJobs.value = data.activity_jobs || [];
  guildBoard.value = data.guild_tasks || null;
  guildCatalog.value = data.guild_catalog || [];
  if (!guildAcceptReady) {
    guildAccept.value = (data.guild_accept || []).slice();
    guildAcceptReady = true;
  }
  if (!pveStagesReady) {
    pveStages.value = data.stages || "";
    pveStagesReady = true;
  }
  if (!dailyAtReady) {
    dailyAt.value = data.schedule || "";
    dailyAtReady = true;
    dailyAtNote.value = data.schedule ? "已设定每天 " + data.schedule : "";
  }
  if (!modoAtReady) {
    modoAt.value = data.modo_at || "";
    modoAtReady = true;
    modoAtNote.value = data.modo_at ? "已设定每天 " + data.modo_at + "，刷完再领国家宝箱" : "";
  }
  if (!guildLevelReady) {
    guildLevel.value = data.guild_level == null ? "" : String(data.guild_level);
    guildLevelReady = true;
  }
  guildThew.value = data.guild_thew == null ? null : data.guild_thew;
  guildScore.value = data.guild_score == null ? null : data.guild_score;
  strikeLeft.value = data.strike_left == null ? null : data.strike_left;
  eggLeft.value = data.egg_left == null ? null : data.egg_left;
  wargame.value = data.wargame || { token: null, sites: [], result: "", occupy: null, partial: false, empty: 0 };
}

async function saveGuildAccept() {
  showErr("", "daily");
  guildAcceptNote.value = "";
  const data = await api("/api/daily/guild-accept", { ids: guildAccept.value });
  guildAccept.value = (data.guild_accept || []).slice();
  guildAcceptReady = true;
  guildAcceptNote.value = "已保存，下一轮按这份接受";
}

async function saveGuildLevel() {
  showErr("", tab.value === "daily" ? "daily" : "activity");
  guildLevelNote.value = "";
  const data = await api("/api/daily/guild-level", { level: guildLevel.value });
  guildLevel.value = data.guild_level == null ? "" : String(data.guild_level);
  guildLevelReady = true;
  guildLevelNote.value = guildLevel.value ? "已保存，下次进攻用 " + guildLevel.value + " 级" : "";
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

async function saveModoAt() {
  showErr("", "daily");
  modoAtNote.value = "";
  const data = await api("/api/daily/modo-schedule", { at: modoAt.value, cards: modoCards.value });
  modoAt.value = data.modo_at || "";
  modoAtReady = true;
  if (data.modo_cards != null) modoCards.value = String(data.modo_cards);
  modoAtNote.value = data.modo_at ? "已设定每天 " + data.modo_at + "，刷完再领国家宝箱" : "已关掉定时";
}

async function refreshBase() {
  if (!me.value) {
    baseJobs.value = [];
    baseBlast.value = null;
    baseMetal.value = null;
    baseOil.value = null;
    baseCredit.value = null;
    basePop.value = null;
    baseCards.value = emptyCards();
    resourceCatalog.value = [];
    cleanCatalog.value = [];
    cleanItems.value = null;
    resourceCards.value = null;
    return;
  }
  const data = await api("/api/base");
  dailyQq.value = data.qq || dailyQq.value;
  baseJobs.value = data.jobs || [];
  baseBlast.value = data.blast == null ? null : data.blast;
  baseMetal.value = data.metal == null ? null : data.metal;
  baseOil.value = data.oil == null ? null : data.oil;
  baseCredit.value = data.credit == null ? null : data.credit;
  basePop.value = data.pop == null ? null : data.pop;
  baseCards.value = data.cards || emptyCards();
  resourceCatalog.value = data.resource_catalog || [];
  resourceCards.value = data.resource_cards || null;
  cleanCatalog.value = data.clean_catalog || [];
  cleanItems.value = data.clean_items || null;
  applyEquipNote(data.equip_note || "", baseCards.value);
}

async function runBase(countOrBody) {
  showErr("", "base");
  baseNote.value = "";
  const body = countOrBody && typeof countOrBody === "object"
    ? countOrBody
    : { kind: "blast", count: countOrBody };
  if (body.kind === "cards") {
    return runCards(body.ids || resourceCards.value || []);
  }
  await api("/api/base", body);
  baseNote.value = "爆破兵已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runSpread(x1, y1, x2, y2) {
  showErr("", "base");
  baseNote.value = "";
  const ok = await confirmUse(`把全部爆破兵均匀布到 (${x1},${y1}) 和 (${x2},${y2}) 这块区域。`);
  if (!ok) return;
  await api("/api/base", { kind: "spread", x1, y1, x2, y2 });
  baseNote.value = "布开爆破兵已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function saveResourceCards(ids) {
  showErr("", "base");
  const data = await api("/api/base/resource-cards", { ids });
  resourceCards.value = data.resource_cards || ids;
  return resourceCards.value;
}

async function saveCleanItems(ids) {
  showErr("", "base");
  const data = await api("/api/base/clean-items", { ids });
  cleanItems.value = data.clean_items || ids;
  return cleanItems.value;
}

async function runClean(ids) {
  showErr("", "base");
  baseNote.value = "";
  const wanted = new Set(ids || []);
  const chosen = cleanCatalog.value.filter((item) => wanted.has(item.id));
  if (!chosen.length) throw new Error("至少选一种物品");
  const ok = await confirmUse(
    "清理：" + chosen.map((item) => item.name).join("、")
      + "。按背包里的数量整叠使用。自选宝箱按选项平均分。背包里没有的跳过。"
  );
  if (!ok) return;
  await api("/api/base", { kind: "clean", ids: chosen.map((item) => item.id) });
  cleanItems.value = chosen.map((item) => item.id);
  baseNote.value = "清理背包已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runCards(ids) {
  showErr("", "base");
  baseNote.value = "";
  const wanted = new Set(ids || []);
  const chosen = resourceCatalog.value.filter((card) => wanted.has(card.id));
  if (!chosen.length) throw new Error("至少选一种资源卡");
  const boxes = chosen.filter((card) => card.box);
  const cards = chosen.filter((card) => !card.box);
  const parts = [];
  if (boxes.length) parts.push("先整叠打开" + boxes.map((card) => card.name).join("、") + "，变成对应的资源卡");
  if (cards.length) parts.push("再按从小到大开" + cards.map((card) => card.name).join("、") + "，一次一张");
  const ok = await confirmUse(
    parts.join("。") + "。某种没有，或这一张没开成，这种就停，换下一种。"
  );
  if (!ok) return;
  await api("/api/base", { kind: "cards", ids: chosen.map((card) => card.id) });
  resourceCards.value = chosen.map((card) => card.id);
  baseNote.value = "开资源卡已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runFactory(army, count) {
  showErr("", "base");
  baseNote.value = "";
  await api("/api/base", { kind: "factory", army, count });
  baseNote.value = "战车工厂已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runAa(count) {
  showErr("");
  baseNote.value = "";
  await api("/api/base", { count, unit: "aa" });
  baseNote.value = "已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runBox(kind, count) {
  showErr("", "base");
  baseNote.value = "";
  const n = cardNum(count);
  const times = Math.floor(n / 10);
  if (times < 1) throw new Error("十连至少要 10 个");
  const name = kind === "box_high" ? "高级补给箱" : "中级补给箱";
  const keyName = kind === "box_high" ? "高级钥匙" : "中级钥匙";
  const ok = await confirmUse(`开启${name} ${times * 10} 个，同时用掉${keyName} ${times * 10} 把，十连 ${times} 次。`);
  if (!ok) return;
  await api("/api/base", { kind, count: n });
  baseNote.value = "已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runDraw(mode, count) {
  showErr("", "base");
  baseNote.value = "";
  const n = cardNum(count);
  const used = mode === "admiral10" || mode === "agent10" ? Math.floor(n / 10) * 10 : n;
  const times = mode === "admiral10" || mode === "agent10" ? Math.floor(n / 10) : n;
  if ((mode === "admiral10" || mode === "agent10") && times < 1) throw new Error("十连至少要 10 张卡");
  const name = mode === "haoyin" ? "豪饮卡" : mode === "agent10" ? "紫色特工令" : "高级将领卡";
  const extra = mode === "admiral10" || mode === "agent10" ? `，十连 ${times} 次` : "";
  const ok = await confirmUse(`使用${name} ${used} 张${extra}。`);
  if (!ok) return;
  await api("/api/base", { count, unit: mode });
  baseNote.value = "已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runPick(kind, count) {
  showErr("", "base");
  baseNote.value = "";
  const n = cardNum(count);
  const text = {
    pick_general: `使用招募卡自选包 ${n} 张，换成高级将领卡 ${n} 张。`,
    pick_staff: `使用招募卡自选包 ${n} 张，换成参谋卡 ${n} 张。`,
    pick_lab: `使用探索卡自选包 ${n} 张，换成实验卡 ${n} 张。`,
    pick_auto: `使用探索卡自选包 ${n} 张，换成自动化探索卡 ${n} 张。`,
  }[kind];
  if (!text) throw new Error("没有这项");
  const ok = await confirmUse(text);
  if (!ok) return;
  await api("/api/base", { kind, count: n });
  baseNote.value = "已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runAuto50(count) {
  showErr("", "base");
  baseNote.value = "";
  const n = cardNum(count);
  const need = n * 50;
  const have = knownCards(baseCards.value.auto);
  if (have !== null && have < need) {
    tellShortage("自动化探索卡", have, need);
    return;
  }
  const ok = await confirmUse(`使用自动化探索卡 ${need} 张，做 ${n} 次 50 连。`);
  if (!ok) return;
  await api("/api/base", { kind: "auto50", count: n });
  baseNote.value = "自动化探索已交给绑定的攻打号";
  await refreshBase();
  await refreshAttacks();
}

async function runExplore(kind, count, cardKey, cardName, batch, note) {
  showErr("", "base");
  baseNote.value = "";
  const n = cardNum(count);
  const need = n * batch;
  const have = knownCards(baseCards.value[cardKey]);
  if (have !== null && have < need) {
    tellShortage(cardName, have, need);
    return;
  }
  const ok = await confirmUse(`使用${cardName} ${need} 张，做 ${n} 次 ${batch} 连。`);
  if (!ok) return;
  await api("/api/base", { kind, count: n });
  baseNote.value = note;
  await refreshBase();
  await refreshAttacks();
}

function runLab50(count) {
  return runExplore("lab50", count, "lab", "实验室探索卡", 50, "实验室探索已交给绑定的攻打号");
}

function runLab10(count) {
  return runExplore("lab10", count, "lab", "实验室探索卡", 10, "实验室探索已交给绑定的攻打号");
}

function runPlant50(count) {
  return runExplore("plant50", count, "plant", "工厂探索卡", 50, "工厂探索已交给绑定的攻打号");
}

async function claimBlueprint() {
  showErr("", "attack");
  const ok = await confirmUse("领取国战图纸奖励。");
  if (!ok) return;
  await api("/api/daily", { kind: "blueprint" });
}

async function runGuildCamp() {
  showErr("", tab.value === "daily" ? "daily" : "activity");
  guildLevelNote.value = "";
  const level = String(guildLevel.value || "").trim();
  const ok = await confirmUse(`按体力进攻 ${level} 级据点，直到体力不够。`);
  if (!ok) return;
  await api("/api/daily", { kind: "guild_camp" });
  guildLevelNote.value = "已交给绑定的攻打号";
  await refreshDaily();
  await refreshAttacks();
}
async function runStrike() {
  showErr("", "activity");
  guildLevelNote.value = "";
  const ok = await confirmUse("按当前剩余次数执行精准打击，打到没有次数或被拒绝就停。");
  if (!ok) return;
  await api("/api/daily", { kind: "strike" });
  guildLevelNote.value = "精准打击已交给绑定的攻打号";
  await refreshDaily();
  await refreshAttacks();
}

async function runFund(building, times) {
  showErr("", "activity");
  fundNote.value = "";
  await api("/api/daily", { kind: "fund", building_id: building, times });
  fundNote.value = "已交给绑定的攻打号";
  await refreshDaily();
  await refreshAttacks();
}

async function runFundCycle() {
  showErr("", "activity");
  fundNote.value = "";
  await api("/api/daily", {
    kind: "fund",
    cycle: true,
    buildings: fundBuildings.map((b) => ({ id: b.id, name: b.name })),
  });
  fundNote.value = "已按列表顺序，一栋拨到被拒再换下一栋";
  await refreshDaily();
  await refreshAttacks();
}

async function runWish(times) {
  showErr("", "activity");
  const n = Number(String(times || "").trim());
  if (!Number.isInteger(n) || n < 1 || n > 999) throw new Error("次数要在 1 到 999");
  const ok = await confirmUse(`许愿树抽 ${n} 次。`);
  if (!ok) return;
  await api("/api/daily", { kind: "wish", times: n });
  await refreshDaily();
  await refreshAttacks();
}

async function runGift() {
  showErr("", "activity");
  const ok = await confirmUse("把背包里的献礼宝箱 30156 全部打开。");
  if (!ok) return;
  await api("/api/daily", { kind: "gift" });
  await refreshDaily();
  await refreshAttacks();
}

async function runSoul(times) {
  showErr("", "activity");
  const n = Number(String(times || "").trim());
  if (!Number.isInteger(n) || n < 1 || n > 999) throw new Error("次数要在 1 到 999");
  const ok = await confirmUse(`将魂兑换 ${n} 次。兑完会自己停。`);
  if (!ok) return;
  await api("/api/daily", { kind: "soul", times: n });
  await refreshDaily();
  await refreshAttacks();
}

async function runBadge() {
  showErr("", "activity");
  const ok = await confirmUse("把背包里的献礼徽章宝箱 30157 全部打开。");
  if (!ok) return;
  await api("/api/daily", { kind: "badge" });
  await refreshDaily();
  await refreshAttacks();
}

async function runTribute() {
  showErr("", "activity");
  const ok = await confirmUse("一键献礼：打开献礼宝箱，摧毁建筑修复卡，打开献礼徽章宝箱，捐献献礼徽章，再清理背包。");
  if (!ok) return;
  await api("/api/daily", { kind: "tribute" });
  await refreshDaily();
  await refreshAttacks();
}

async function runDonate() {
  showErr("", "activity");
  const ok = await confirmUse("捐献献礼徽章。");
  if (!ok) return;
  await api("/api/daily", { kind: "donate" });
  await refreshDaily();
  await refreshAttacks();
}

async function runTrain(count) {
  showErr("", "activity");
  const n = Number(String(count || "").trim());
  if (!Number.isInteger(n) || n < 1 || n > 999) throw new Error("本数要在 1 到 999");
  const ok = await confirmUse(`给正在训练的英雄用 ${n} 本训练册。`);
  if (!ok) return;
  await api("/api/daily", { kind: "train_book", count: n });
  await refreshDaily();
  await refreshAttacks();
}

async function runTrainAll() {
  showErr("", "activity");
  const ok = await confirmUse("把背包里的训练册全部用在正在训练的英雄上。一次最多 999 本，还有就接着用。");
  if (!ok) return;
  await api("/api/daily", { kind: "train_book", all: true });
  await refreshDaily();
  await refreshAttacks();
}

async function runStone(times) {
  showErr("", "activity");
  const n = Number(String(times || "").trim());
  if (!Number.isInteger(n) || n < 1 || n > 999) throw new Error("次数要在 1 到 999");
  const ok = await confirmUse(`购买 ${n} 个保护石。每个花费 2。`);
  if (!ok) return;
  await api("/api/daily", { kind: "stone", times: n });
  await refreshDaily();
  await refreshAttacks();
}

async function runSkill(level) {
  showErr("", "activity");
  const n = Number(String(level || "").trim());
  if (!Number.isInteger(n) || n < 1 || n > 10) throw new Error("等级要在 1 到 10");
  const ok = await confirmUse(`按背包数量的一半合成 ${n} 级参谋技能书。`);
  if (!ok) return;
  await api("/api/daily", { kind: "skill_book", level: n });
  await refreshDaily();
  await refreshAttacks();
}

async function runDrop() {
  showErr("", "activity");
  const ok = await confirmUse("把背包里的建筑修复卡 10127 全部摧毁，不修复建筑。");
  if (!ok) return;
  await api("/api/daily", { kind: "repair_drop" });
  await refreshDaily();
  await refreshAttacks();
}

async function runWargameRetreat(siteId) {
  showErr("", "activity");
  const id = Number(siteId) || 0;
  const where = id > 0 ? `场地 ${id}` : "当前占着的演习场";
  const ok = await confirmUse(`从${where}撤退。`);
  if (!ok) return;
  await api("/api/daily", { kind: "wargame", action: "retreat", site_id: id });
  await refreshDaily();
  await refreshAttacks();
}

async function runWargame(site) {
  showErr("", "activity");
  if (site && site.uid) {
    const power = Number(site.power) > 0
      ? Number(site.power).toLocaleString("zh-CN")
      : "未知";
    const lv = site.lv ? `${site.lv}级` : "";
    const ok = await confirmUse(`进攻 ${site.name || "这个玩家"}（战力 ${power}）的${lv}演习场。`);
    if (!ok) return;
    await api("/api/daily", {
      kind: "wargame",
      action: "attack",
      site_id: site.id,
      uid: site.uid,
      name: site.name || "",
      power: site.power || 0,
    });
  } else {
    await api("/api/daily", { kind: "wargame", action: "list" });
  }
  await refreshDaily();
  await refreshAttacks();
}

async function runEgg() {
  showErr("", "activity");
  eggNote.value = "";
  const ok = await confirmUse("把剩余彩蛋全部砸开，直到服务器上的数量变成 0。每个消耗 1 个物品 10015。");
  if (!ok) return;
  await api("/api/daily", { kind: "egg" });
  eggNote.value = "已交给绑定的攻打号";
  await refreshDaily();
  await refreshAttacks();
}

async function cancelBase(it) {
  showErr("", "base");
  await api("/api/daily/cancel", { id: it.id });
  await refreshBase();
}

async function runDaily(kind, extra) {
  showErr("");
  dailyNote.value = "";
  await api("/api/daily", Object.assign({ kind }, extra || {}));
  dailyNote.value = "已交给绑定的攻打号";
  await refreshDaily();
  await refreshAttacks();
}

let attackHereOn = false;

function askAttackHere() {
  api("/api/attack-here", {}).catch(() => {});
}

const EQUIP_NOTES = ["正在等攻打线程", "正在读装备栏", "这次没读到装备栏"];
let equipNoteAt = 0;

function boxesRead(cards) {
  if (!cards) return false;
  return ["box_mid", "box_high", "box_mid_key", "box_high_key"].every(
    (key) => cards[key] !== null && cards[key] !== undefined);
}

function applyEquipNote(text, cards) {
  const cur = baseNote.value;
  if (cur && !EQUIP_NOTES.includes(cur)) return;
  if (boxesRead(cards)) {
    if (EQUIP_NOTES.includes(cur)) baseNote.value = "";
    return;
  }
  if (text && text === cur) return;
  if (!text) {
    if (cur && Date.now() - equipNoteAt < 12000) return;
    if (cur) baseNote.value = "";
    return;
  }
  equipNoteAt = Date.now();
  baseNote.value = text;
}

async function askBaseEquip() {
  try {
    const data = await api("/api/base-equip", {});
    applyEquipNote(data.note || "", baseCards.value);
  } catch (e) {
    if (EQUIP_NOTES.includes(baseNote.value)) baseNote.value = "";
    showErr(e.message, "base");
  }
}

function markAttackHere() {
  const on = !!(toolsOn.value && tab.value === "attack");
  if (on && !attackHereOn) askAttackHere();
  attackHereOn = on;
}

function pickTab(key) {
  tab.value = key;
  if (key === "daily") {
    refreshDaily().catch((e) => showErr(e.message, "daily"));
    if (!me.value || !me.value.base_war) api("/api/camp-live", {}).catch((e) => showErr(e.message, "daily"));
  }
  if (key === "activity") {
    refreshDaily().catch((e) => showErr(e.message, "activity"));
    api("/api/camp-live", {}).catch((e) => showErr(e.message, "activity"));
  }
  if (key === "base") {
    refreshBase().catch((e) => showErr(e.message, "base"));
    askBaseEquip();
  }
  if (key === "attack") attackHereOn = false;
  markAttackHere();
}

function openProfile() {
  tab.value = tab.value === "profile" && !toolsOn.value ? "attack" : "profile";
  markAttackHere();
}

function openQq() {
  tab.value = tab.value === "qq" && !toolsOn.value ? "attack" : "qq";
  markAttackHere();
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
    command.value = null;
    attackHereOn = false;
    return;
  }
  const atk = await api("/api/attacks");
  orders.value = atk.items || [];
  storms.value = atk.storms || [];
  command.value = atk.command && atk.command.team_id ? atk.command : null;
  if (me.value) {
    const next = { ...me.value, desk_test: !!atk.desk_test };
    if (typeof atk.storm_day === "number") next.storm_day = atk.storm_day;
    me.value = next;
  }
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
  markAttackHere();
}

async function loadCommandSeats() {
  if (!me.value || !command.value || !command.value.self || seatBusy) return;
  seatBusy = true;
  try {
    const data = await api("/api/desk/seats");
    const rows = data.roster || [];
    const by = new Map(rows.map((r) => [r.id, r]));
    for (const m of command.value.roster || []) {
      const next = by.get(m.id) || by.get(m.user_id);
      if (!next) continue;
      const here = next.here || "";
      const hereId = next.here_id || 0;
      if (m.here !== here) m.here = here;
      if (m.here_id !== hereId) m.here_id = hereId;
      if (m.cards !== next.cards) m.cards = next.cards;
    }
  } catch (e) {
    /* 城市刷新失败时留着上一次的位置 */
  } finally {
    seatBusy = false;
  }
}

async function saveCommandAccept(on) {
  commandBusy.value = true;
  try {
    const data = await api("/api/desk/accept", { on: !!on });
    command.value = data.command && data.command.team_id ? data.command : null;
  } finally {
    commandBusy.value = false;
  }
}

async function inviteMember(username) {
  const name = String(username || "").trim();
  if (!name) throw new Error("要填对方的账号");
  commandBusy.value = true;
  try {
    const data = await api("/api/desk/invite", { username: name });
    command.value = data.command && data.command.team_id ? data.command : null;
  } finally {
    commandBusy.value = false;
  }
}

async function kickMember(member) {
  if (!member || !member.user_id) return;
  commandBusy.value = true;
  try {
    const data = await api("/api/desk/kick", { user_id: member.user_id });
    command.value = data.command && data.command.team_id ? data.command : null;
  } finally {
    commandBusy.value = false;
  }
}

async function renameSquad(name) {
  commandBusy.value = true;
  try {
    const data = await api("/api/desk/name", { name });
    command.value = data.command && data.command.team_id ? data.command : null;
  } finally {
    commandBusy.value = false;
  }
}

async function setSquadTask(state) {
  commandBusy.value = true;
  try {
    const data = await api("/api/desk/task", { state });
    command.value = data.command && data.command.team_id ? data.command : null;
  } finally {
    commandBusy.value = false;
  }
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
  if (p.shutdown) return p.detail || "已定时关闭";
  if (!p.online) return p.detail || "没在跑";
  if (p.paused && p.phase !== "login") return p.detail || "已暂停";
  return p.detail || "空闲，等订单";
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
  if (me.value && me.value.base_war && toolsOn.value) {
    refreshDaily().catch(() => {});
    refreshBase().catch(() => {});
  }
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
  if (me.value && me.value.base_war && toolsOn.value) {
    refreshDaily().catch(() => {});
    refreshBase().catch(() => {});
  }
}

function takeUser(user) {
  me.value = user;
  if (!user.base_war && (tab.value === "base" || tab.value === "activity")) tab.value = "attack";
  if (!user.watch && tab.value === "watch") tab.value = "attack";
  pveStagesReady = false;
  guildAcceptReady = false;
  guildAcceptNote.value = "";
  guildLevelReady = false;
  dailyAtReady = false;
  modoAtReady = false;
  qqTarget.value = user.qq_target || "";
  autoLock.value = !!user.auto_lock;
  holdMin.value = String(user.hold_min ?? 0);
  holdAll.value = !!user.hold_all;
  offAt.value = user.off_at ? String(user.off_at).replace(" ", "T") : "";
  offNote.value = "";
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
  awayMorale.value = String(user.away_morale ?? 100);
  prioVip.value = !!user.clear_prio_vip;
  prioOfficer.value = !!user.clear_prio_officer;
  clearRows.value = (user.clear_priority || []).map((row) => ({
    uid: String(row.uid),
    rank: String(row.rank),
  }));
  modoCards.value = String(user.modo_cards ?? 0);
  guides.value = Array.isArray(user.guides) ? user.guides : [];
  guideErr.value = "";
}

async function ackGuide() {
  const cur = guide.value;
  if (!cur || guideBusy.value) return;
  guideErr.value = "";
  guideBusy.value = true;
  try {
    const data = await api("/api/guide/seen", { id: cur.id });
    guides.value = Array.isArray(data.guides) ? data.guides : [];
  } catch (e) {
    guideErr.value = e.message || "没记下";
  } finally {
    guideBusy.value = false;
  }
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
    away_morale: awayMorale.value,
    prio_vip: prioVip.value,
    prio_officer: prioOfficer.value,
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

async function saveSkip(it, on) {
  const prev = !!it.skip;
  const next = !!on;
  const key = lockKey(it);
  if (next === prev || skipSwitching.value) return;
  it.skip = next;
  skipSwitching.value = key;
  showErr("", "watch");
  try {
    const data = await api("/api/subs/skip", { city_id: it.city_id, uid: it.uid, on: next });
    it.skip = !!data.skip;
    showErr(attackLoginError(data.login), "watch");
  } catch (e) {
    it.skip = prev;
    showErr(e.message, "watch");
  } finally {
    skipSwitching.value = "";
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

async function closeAttack() {
  if (!window.confirm("关闭这个攻打号？和定时关闭到点一样，不再执行任务。更新之后也不会再拉起。要再上线得点登录。")) return;
  showErr("", "bar");
  offNote.value = "";
  closeBusy.value = true;
  try {
    const data = await api("/api/attack-off", { now: true });
    offAt.value = data.off_at ? String(data.off_at).replace(" ", "T") : "";
    if (me.value) me.value.off_at = data.off_at || "";
    offNote.value = data.due ? "已到点，账号下线" : (data.off_at ? "到点后下线" : "已取消");
    await refreshAttacks();
  } catch (e) {
    showErr(e.message, "bar");
  } finally {
    closeBusy.value = false;
  }
}

async function saveOffAt() {
  showErr("", "bar");
  offNote.value = "";
  const data = await api("/api/attack-off", { at: offAt.value });
  offAt.value = data.off_at ? String(data.off_at).replace(" ", "T") : "";
  if (me.value) me.value.off_at = data.off_at || "";
  offNote.value = data.due ? "已到点，账号下线" : (data.off_at ? "到点后下线" : "已取消");
  await refreshAttacks();
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

async function pushLogin(plat) {
  showErr("", "bar");
  const data = await api("/api/attack-login", { platform: plat || "空间", way: "qr" });
  if (data.off_at != null) {
    offAt.value = data.off_at ? String(data.off_at).replace(" ", "T") : "";
    if (me.value) me.value.off_at = data.off_at || "";
    offNote.value = data.off_at ? "到点后下线" : "已取消";
  }
  if (data.login === "qr" && !(proc.value && proc.value.qq)) bindWait.value = true;
  showErr(attackLoginError(data.login), "bar");
  await refreshAttacks();
}

function attackLoginError(login) {
  if (login === "no_account") return "服务器还没配置攻打号，二维码发不出去";
  if (login === "taken") return "这个攻打 QQ 已经绑定别的登录账号，不能接着用";
  if (login === "unbound") return "这个登录账号还没绑定攻打号";
  if (login === "off") return "已定时关闭，不再执行任务";
  if (login === "timeout") return "登录超过 10 分钟，已停止，请再点一次登录";
  if (login === "no_napcat") return "这个攻打号没有 NapCat 地址";
  if (login === "napcat_fail") return "NapCat 没有交出这张票";
  if (login === "napcat_busy") return "已经有一次取票在进行";
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

async function loadFailLibrary() {
  return await api("/api/atk-fail");
}

async function loadWargameHistory() {
  return await api("/api/wargame/history");
}

async function loadRegions() {
  return await api("/api/regions");
}

async function saveRegion(region) {
  showErr("", "region");
  const data = await api("/api/region", { region });
  if (me.value) me.value = { ...me.value, region: Number(data.region) || 0 };
  return data;
}

async function resetFailLibrary(uid) {
  showErr("", "fail");
  const body = {};
  if (uid) body.uid = String(uid);
  return await api("/api/atk-fail/reset", body);
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
  guides.value = [];
  guideErr.value = "";
  showErr("");
  tab.value = "attack";
  dailyQq.value = "";
  dailyTasks.value = [];
  dailyJobs.value = [];
  activityJobs.value = [];
  guildBoard.value = null;
  guildCatalog.value = [];
  guildAccept.value = [];
  guildAcceptNote.value = "";
  guildAcceptReady = false;
  pveStages.value = "";
  pveStagesReady = false;
  dailyAt.value = "";
  dailyAtNote.value = "";
  dailyAtReady = false;
  modoAt.value = "";
  modoAtNote.value = "";
  modoAtReady = false;
  guildLevel.value = "";
  guildLevelNote.value = "";
  guildLevelReady = false;
  holdNote.value = "";
  offAt.value = "";
  offNote.value = "";
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

const citySubs = computed(() => {
  const order = [];
  const map = {};
  for (const it of items.value) {
    let group = map[it.city_id];
    if (!group) {
      group = { city_id: it.city_id, city_name: it.city_name || "", present: false, people: [] };
      map[it.city_id] = group;
      order.push(group);
    }
    if (!group.city_name && it.city_name) group.city_name = it.city_name;
    if (it.present) group.present = true;
    group.people.push(it);
  }
  return order;
});

function isSubOpen(key) {
  const saved = subOpen.value[key];
  return saved == null ? true : saved;
}

function onSubToggle(key, ev) {
  subOpen.value = { ...subOpen.value, [key]: ev.target.open };
}

onMounted(async () => {
  try {
    const at = JSON.parse(localStorage.getItem("job-float-at") || "null");
    if (at && Number.isFinite(at.x) && Number.isFinite(at.y)) floatAt.value = clampFloat(at.x, at.y);
  } catch (e) { /* 没有存过位置 */ }
  const meta = await fetch("/api/meta").then((r) => r.json()).catch(() => ({}));
  devLogin.value = !!meta.dev_login;
  registerOpen.value = !!meta.register;
  loginPlatDefault.value = meta.login_platform === "大厅" ? "大厅" : "空间";
  await loadMe();
  timer = setInterval(() => {
    if (me.value) refresh().catch(() => {});
  }, 4000);
  atkTimer = setInterval(() => {
    if (!me.value) return;
    refreshAttacks().catch(() => {});
    if (me.value.base_war && toolsOn.value) {
      refreshDaily().catch(() => {});
      refreshBase().catch(() => {});
    } else if (tab.value === "daily") refreshDaily().catch(() => {});
  }, 2000);
  clockTimer = setInterval(() => {
    clock.value = Date.now();
  }, 1000);
  seatTimer = setInterval(() => { loadCommandSeats().catch(() => {}); }, 1000);
});
onUnmounted(() => {
  clearInterval(timer);
  clearInterval(atkTimer);
  clearInterval(clockTimer);
  clearInterval(seatTimer);
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
      <Teleport to="body">
        <HiddenMark :text="me.username" />
      </Teleport>
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
        :login-plat-default="loginPlatDefault"
        :close-attack="closeAttack"
        :close-busy="closeBusy"
        v-model:off-at="offAt"
        :save-off-at="saveOffAt"
        :off-note="offNote"
        :reload-qr="reloadQr"
        :show-err="showErr"
      />
      <SquadPanel
        v-if="me.desk_test"
        v-show="!sideOn"
        :squad="command"
        :lead="!!(command && command.self)"
        :idle="!command"
        :busy="commandBusy"
        :rename-squad="renameSquad"
        :invite-member="inviteMember"
        :kick-member="kickMember"
        :set-task="setSquadTask"
        :answer="saveCommandAccept"
        :show-err="showErr"
      />
      <div v-if="toolsOn && me.base_war && floatJobs.length && (tab === 'base' || tab === 'activity')" class="job-float" :class="{ 'is-collapsed': floatFolded }" :style="floatStyle">
        <header @pointerdown="dragFloat">任务状态</header>
        <div class="job-float-body">
          <p v-for="it in floatJobs" :key="it.id">{{ it.lane === "base" ? "基地战" : "活动" }} {{ it.label }} {{ it.status }} {{ it.detail }}<button v-if="it.status === '排队' || it.status === '正在做'" type="button" class="ghost" @click="(it.lane === 'base' ? cancelBase(it) : cancelDaily(it)).catch((e) => showErr(e.message))">关停</button></p>
        </div>
      </div>
      <el-menu v-if="toolsOn" class="page-nav" mode="horizontal" :ellipsis="false" :default-active="sideOn ? '' : tab" :key="tab" aria-label="功能" @select="pickTab">
        <el-menu-item index="attack">国战助手</el-menu-item>
        <el-menu-item index="daily">日常任务</el-menu-item>
        <el-menu-item v-if="me && me.base_war" index="base">基地战助手</el-menu-item>
        <el-menu-item v-if="me && me.base_war" index="activity">活动助手</el-menu-item>
        <el-menu-item v-if="me && me.watch" index="watch">监控敌人</el-menu-item>
      </el-menu>
      <ProfilePage
        v-show="tab === 'profile'"
        :me="me"
        :err="err"
        :err-at="errAt"
        :save-password="savePassword"
        :load-fail-library="loadFailLibrary"
        :reset-fail-library="resetFailLibrary"
        :load-regions="loadRegions"
        :save-region="saveRegion"
        :load-wargame-history="loadWargameHistory"
        :show-err="showErr"
      />
      <ActivityPage
        v-show="toolsOn && me && me.base_war && tab === 'activity'"
        v-model:guild-level="guildLevel"
        v-model:fund-building="fundBuilding"
        v-model:fund-times="fundTimes"
        v-model:wish-times="wishTimes"
        v-model:soul-times="soulTimes"
        v-model:train-count="trainCount"
        v-model:skill-level="skillLevel"
        v-model:stone-times="stoneTimes"
        :daily-qq="dailyQq"
        :tasks="dailyTasks"
        :jobs="activityJobs"
        :cancel-job="cancelDaily"
        :run-guild-camp="runGuildCamp"
        :daily-switching="dailySwitching"
        :note="guildLevelNote"
        :thew="guildThew"
        :score="guildScore"
        :strike-left="strikeLeft"
        :egg-left="eggLeft"
        :run-strike="runStrike"
        :fund-buildings="fundBuildings"
        :fund-note="fundNote"
        :egg-note="eggNote"
        :wargame="wargame"
        :run-wargame="runWargame"
        :run-wargame-retreat="runWargameRetreat"
        :err="err"
        :err-at="errAt"
        :save-level="saveGuildLevel"
        :save-daily-switch="saveDailySwitch"
        :run-fund="runFund"
        :run-fund-cycle="runFundCycle"
        :run-egg="runEgg"
        :run-wish="runWish"
        :run-gift="runGift"
        :run-drop="runDrop"
        :run-soul="runSoul"
        :run-badge="runBadge"
        :run-donate="runDonate"
        :run-tribute="runTribute"
        :run-train="runTrain"
        :run-train-all="runTrainAll"
        :run-skill="runSkill"
        :run-stone="runStone"
        :show-err="showErr"
      />
      <DailyPage
        v-show="toolsOn && tab === 'daily'"
        v-model:daily-at="dailyAt"
        v-model:pve-stages="pveStages"
        v-model:modo-cards="modoCards"
        :daily-qq="dailyQq"
        :campaign-tasks="campaignTasks"
        :progress-tasks="progressTasks"
        :daily-jobs="dailyJobs"
        :guild-board="guildBoard"
        :guild-catalog="guildCatalog"
        v-model:guild-accept="guildAccept"
        :guild-accept-note="guildAcceptNote"
        :save-guild-accept="saveGuildAccept"
        v-model:modo-at="modoAt"
        :daily-at-note="dailyAtNote"
        :modo-at-note="modoAtNote"
        :daily-note="dailyNote"
        :daily-switching="dailySwitching"
        :err="err"
        :err-at="errAt"
        :proc="proc"
        :run-daily="runDaily"
        :cancel-daily="cancelDaily"
        :save-daily-at="saveDailyAt"
        :save-modo-at="saveModoAt"
        :save-daily-switch="saveDailySwitch"
        :add-modo="addModo"
        :show-err="showErr"
        :show-camp="!(me && me.base_war)"
        :camp-task="dailyTasks.find((it) => it.key === '公会战役') || null"
        :camp-job="activityJobs.find((it) => it.kind === 'guild_camp') || null"
        v-model:guild-level="guildLevel"
        :guild-level-note="guildLevelNote"
        :thew="guildThew"
        :score="guildScore"
        :save-guild-level="saveGuildLevel"
        :run-guild-camp="runGuildCamp"
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
        v-if="me && me.watch"
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
        :city-subs="citySubs"
        :locking="locking"
        :city-label="cityLabel"
        :add-sub="addSub"
        :save-auto-lock="saveAutoLock"
        :save-lock-cards="saveLockCards"
        :save-retreat="saveRetreat"
        :lock-one="lockOne"
        :save-skip="saveSkip"
        :skip-switching="skipSwitching"
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
        v-model:away-morale="awayMorale"
        v-model:prio-uid="prioUid"
        v-model:prio-rank="prioRank"
        v-model:prio-vip="prioVip"
        v-model:prio-officer="prioOfficer"
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
        :claim-blueprint="claimBlueprint"
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
      <BasePage
        v-show="toolsOn && me && me.base_war && tab === 'base'"
        v-model:count="baseCount"
        v-model:army="baseArmy"
        v-model:factory-count="baseFactoryCount"
        v-model:aa-count="aaCount"
        v-model:drink-count="drinkCount"
        v-model:admiral-count="admiralCount"
        v-model:admiral10-count="admiral10Count"
        v-model:agent-count="agentCount"
        v-model:box-mid-count="boxMidCount"
        v-model:box-high-count="boxHighCount"
        v-model:general-count="generalCount"
        v-model:staff-count="staffCount"
        v-model:lab-pick-count="labPickCount"
        v-model:auto-count="autoCount"
        v-model:lab50-count="lab50Count"
        v-model:lab10-count="lab10Count"
        v-model:plant50-count="plant50Count"
        v-model:auto50-count="auto50Count"
        v-model:x1="spreadX1"
        v-model:y1="spreadY1"
        v-model:x2="spreadX2"
        v-model:y2="spreadY2"
        :daily-qq="dailyQq"
        :jobs="baseJobs"
        :blast="baseBlast"
        :metal="baseMetal"
        :oil="baseOil"
        :credit="baseCredit"
        :pop="basePop"
        :cards="baseCards"
        :resource-catalog="resourceCatalog"
        :resource-cards="resourceCards"
        :clean-catalog="cleanCatalog"
        :clean-items="cleanItems"
        :note="baseNote"
        :err="err"
        :err-at="errAt"
        :run-blast="runBase"
        :run-spread="runSpread"
        :run-factory="runFactory"
        :run-cards="runCards"
        :save-resource-cards="saveResourceCards"
        :run-clean="runClean"
        :save-clean="saveCleanItems"
        :run-draw="runDraw"
        :run-box="runBox"
        :run-pick="runPick"
        :run-lab50="runLab50"
        :run-lab10="runLab10"
        :run-plant50="runPlant50"
        :run-auto50="runAuto50"
        :cancel-job="cancelBase"
        :show-err="showErr"
      />
    </template>

    <div v-if="ask" class="modal">
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="ask-title">
        <h2 id="ask-title">确认使用</h2>
        <p>{{ ask }}</p>
        <div class="modal-actions">
          <button type="button" @click="answerAsk(true)">确认</button>
          <button type="button" class="ghost" @click="answerAsk(false)">取消</button>
        </div>
      </div>
    </div>
    <div v-if="notice" class="modal" @click.self="notice = null">
      <div class="modal-card" :class="{ bad: !notice.ok }" role="dialog" aria-modal="true" aria-labelledby="notice-title">
        <h2 id="notice-title">{{ notice.title }}</h2>
        <p>{{ notice.text }}</p>
        <button type="button" @click="notice = null">知道了</button>
      </div>
    </div>
    <div v-if="me && guide" class="modal">
      <div class="modal-card guide" role="dialog" aria-modal="true" aria-labelledby="guide-title">
        <h2 id="guide-title">{{ guide.title }}</h2>
        <MarkdownDoc :model-value="guide.text || ''" readonly />
        <img
          v-for="(src, i) in guideImages"
          :key="src + i"
          class="guide-shot"
          :src="src"
          :alt="guide.title"
        />
        <p v-if="guideErr" class="err">{{ guideErr }}</p>
        <button type="button" :disabled="guideBusy" @click="ackGuide()">确认</button>
      </div>
    </div>
  </main>
</template>

