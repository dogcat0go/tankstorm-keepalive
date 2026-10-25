<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import "./base.css";

const me = ref(null);
const username = ref("");
const password = ref("");
const err = ref("");
const cities = ref([]);
const members = ref([]);
const openMember = ref(0);
const openExpiry = ref(0);
const expiryDraft = ref({});
const expiryBusy = ref(0);
const expiryErr = ref({});
const expiryNote = ref({});
const openTier = ref(0);
const tierDraft = ref({});
const tierBusy = ref(0);
const tierErr = ref({});
const tierNote = ref({});
const TIERS = ["初级", "中级", "高级", "高级pro"];
const clearing = ref(0);
const stormNotes = ref({});
const stormErr = ref({});
const fighters = ref([]);
const scanGap = ref("300");
const quietStart = ref("");
const quietEnd = ref("");
const scanRanges = ref([]);
const scanNote = ref("");
const scanQuiet = ref(false);
const memberQ = ref("");
const memberRegion = ref("");
const memberOnline = ref("");
const memberTier = ref("");
const memberSort = ref("id");
const memberDir = ref("asc");
const fighterQ = ref("");
const fighterRegion = ref("");
const fighterOnline = ref("");
const fighterSort = ref("id");
const fighterDir = ref("asc");
const sheet = ref("members");
const showExpired = ref(false);
const memberPage = ref(1);
const fighterPage = ref(1);
const logQq = ref("");
const logDays = ref("3");
const logView = ref(null);
const logBusy = ref(false);
const logErr = ref("");
const PAGE = 20;
let fighterTimer = 0;
const TIER_RANK = { 初级: 1, 中级: 2, 高级: 3, 高级pro: 4 };

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

function cityLabel(c) {
  const same = cities.value.filter((x) => x.name === c.name).length;
  return same > 1 ? c.name + " " + c.id : c.name;
}

function procText(p) {
  if (!p) return "没在跑";
  if (!p.online) return p.detail || "没在跑";
  if (p.paused && p.phase !== "login") return p.detail || "已暂停";
  return p.detail || "空闲，等订单";
}

const fighterMap = computed(() => {
  const map = {};
  for (const f of fighters.value) map[f.user_id] = f;
  return map;
});

const memberMap = computed(() => {
  const map = {};
  for (const m of members.value) map[m.id] = m;
  return map;
});

const regionOptions = computed(() => {
  const set = new Set();
  for (const m of members.value) {
    if (m.region) set.add(Number(m.region));
  }
  for (const f of fighters.value) {
    if (f.region) set.add(Number(f.region));
  }
  return [...set].sort((a, b) => a - b);
});

function fighterOf(userId) {
  return fighterMap.value[userId] || null;
}

function regionOf(row) {
  if (row.region) return Number(row.region);
  const m = memberMap.value[row.user_id || row.id];
  return m && m.region ? Number(m.region) : 0;
}

function regionText(row) {
  const n = regionOf(row);
  return n ? n + "区" : "—";
}

function onlineOf(row) {
  if (row.online) return true;
  const f = fighterOf(row.id || row.user_id);
  return !!(f && f.online);
}

function sortHint(col, key, dir) {
  if (col !== key) return "";
  return dir === "asc" ? " ↑" : " ↓";
}

function toggleSort(which, col) {
  const sortRef = which === "m" ? memberSort : fighterSort;
  const dirRef = which === "m" ? memberDir : fighterDir;
  if (sortRef.value === col) {
    dirRef.value = dirRef.value === "asc" ? "desc" : "asc";
  } else {
    sortRef.value = col;
    dirRef.value = col === "online" ? "desc" : "asc";
  }
}

function hitQuery(q, ...parts) {
  const s = q.trim().toLowerCase();
  if (!s) return true;
  return parts.some((p) => String(p || "").toLowerCase().includes(s));
}

function hitRegion(sel, region) {
  if (!sel) return true;
  return String(Number(region) || 0) === sel;
}

function hitOnline(sel, on) {
  if (!sel) return true;
  return sel === "on" ? !!on : !on;
}

function beijingDay() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

function expiryDay(expiresAt) {
  const matched = String(expiresAt || "").trim().match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)/);
  if (!matched) return "";
  const y = Number(matched[1]);
  const mo = Number(matched[2]);
  const d = Number(matched[3]);
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) return "";
  return y + "-" + String(mo).padStart(2, "0") + "-" + String(d).padStart(2, "0");
}

function accountExpired(expiresAt) {
  const text = String(expiresAt || "").trim();
  if (!text) return false;
  const day = expiryDay(text);
  if (!day) return true;
  return day < beijingDay();
}

function memberExpired(m) {
  return accountExpired(m && m.expires_at);
}

function fighterExpired(f) {
  return memberExpired(memberMap.value[f.user_id]);
}

function keepAccount(expired) {
  return showExpired.value || !expired;
}

function cmpVal(a, b, dir) {
  let n = 0;
  if (typeof a === "string" || typeof b === "string") n = String(a).localeCompare(String(b), "zh");
  else if (a < b) n = -1;
  else if (a > b) n = 1;
  return dir === "desc" ? -n : n;
}

function memberKey(m, col) {
  if (col === "username") return m.username || "";
  if (col === "tier") return TIER_RANK[m.tier] || 0;
  if (col === "expires") {
    const raw = (m.expires_at || "").trim();
    return expiryDay(raw) || raw || "9999-99-99";
  }
  if (col === "qq") return m.attack_qq || "";
  if (col === "region") return regionOf(m) || 9999;
  if (col === "online") return onlineOf(m) ? 1 : 0;
  return m.id;
}

function fighterKey(f, col) {
  if (col === "username") return f.username || "";
  if (col === "qq") return String(f.qq || "");
  if (col === "region") return regionOf(f) || 9999;
  if (col === "online") return f.online ? 1 : 0;
  if (col === "here") return f.here || "";
  return f.user_id;
}

function memberPasses(m) {
  return hitQuery(memberQ.value, m.username, m.attack_qq, m.qq_target)
    && hitRegion(memberRegion.value, m.region)
    && hitOnline(memberOnline.value, onlineOf(m))
    && (!memberTier.value || (m.tier || "初级") === memberTier.value);
}

function fighterPasses(f) {
  return hitQuery(fighterQ.value, f.username, f.qq)
    && hitRegion(fighterRegion.value, regionOf(f))
    && hitOnline(fighterOnline.value, f.online);
}

const shownMembers = computed(() => {
  const rows = members.value.filter((m) => memberPasses(m) && keepAccount(memberExpired(m)));
  const col = memberSort.value;
  const dir = memberDir.value;
  rows.sort((a, b) => cmpVal(memberKey(a, col), memberKey(b, col), dir) || a.id - b.id);
  return rows;
});

const shownFighters = computed(() => {
  const rows = fighters.value.filter((f) => fighterPasses(f) && keepAccount(fighterExpired(f)));
  const col = fighterSort.value;
  const dir = fighterDir.value;
  rows.sort((a, b) => cmpVal(fighterKey(a, col), fighterKey(b, col), dir) || a.user_id - b.user_id);
  return rows;
});

const hiddenExpiredMembers = computed(() => (
  showExpired.value ? 0 : members.value.filter((m) => memberPasses(m) && memberExpired(m)).length
));

const hiddenExpiredFighters = computed(() => (
  showExpired.value ? 0 : fighters.value.filter((f) => fighterPasses(f) && fighterExpired(f)).length
));

function pageOf(rows, page) {
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const cur = Math.min(Math.max(1, page), pages);
  const start = (cur - 1) * PAGE;
  return {
    page: cur,
    pages,
    rows: rows.slice(start, start + PAGE),
    from: rows.length ? start + 1 : 0,
    to: Math.min(start + PAGE, rows.length),
  };
}

const memberView = computed(() => pageOf(shownMembers.value, memberPage.value));
const fighterView = computed(() => pageOf(shownFighters.value, fighterPage.value));

watch([memberQ, memberRegion, memberOnline, memberTier, memberSort, memberDir, showExpired], () => {
  memberPage.value = 1;
});
watch([fighterQ, fighterRegion, fighterOnline, fighterSort, fighterDir, showExpired], () => {
  fighterPage.value = 1;
});

const memberFilterOn = computed(() =>
  !!(memberQ.value || memberRegion.value || memberOnline.value || memberTier.value));
const fighterFilterOn = computed(() =>
  !!(fighterQ.value || fighterRegion.value || fighterOnline.value));

function clearMemberFilters() {
  memberQ.value = "";
  memberRegion.value = "";
  memberOnline.value = "";
  memberTier.value = "";
  showExpired.value = false;
}

function clearFighterFilters() {
  fighterQ.value = "";
  fighterRegion.value = "";
  fighterOnline.value = "";
  showExpired.value = false;
}

async function loadCities() {
  const data = await api("/api/cities");
  cities.value = data.items || [];
}

async function loadScan() {
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

async function refreshFighters() {
  const data = await api("/api/admin/fighters");
  fighters.value = data.fighters || [];
}

function orderStatus(status) {
  return {
    pending: "排队",
    running: "正在打",
    blocked: "等通路",
    wait: "等再打",
    done: "已打完",
    failed: "没打成",
    ended: "已结束",
  }[status] || status || "—";
}

function orderWho(it) {
  if (!it || it.label === "清城") return "清城";
  const name = it.name ? it.name + " " : "";
  return (name + (it.uid || "")).trim() || "—";
}

function orderWhere(it) {
  if (!it) return "—";
  if (it.kind === "modo") return "摩多";
  if (it.city_name) return it.city_name;
  return it.city_id ? String(it.city_id) : "—";
}

function beatText(it) {
  if (!it || it.beats == null || it.beats === "") return "—";
  return String(it.beats);
}

function accountLine(accounts) {
  const names = (accounts || []).map((item) => item.username).filter(Boolean);
  return names.length ? names.join("、") : "—";
}

function clearWaitLine(accounts) {
  return (accounts || []).map((item) => {
    const wait = item.clear_wait ? item.clear_wait + " 分钟后再打" : "空了就结束";
    return (item.username || "这个账号") + " " + wait;
  }).join("，");
}

async function loadQqLog() {
  if (!logQq.value) return;
  logBusy.value = true;
  logErr.value = "";
  try {
    logView.value = await api(
      "/api/admin/qq-log?qq=" + encodeURIComponent(logQq.value)
      + "&days=" + encodeURIComponent(logDays.value));
  } catch (e) {
    logErr.value = e.message;
  } finally {
    logBusy.value = false;
  }
}

function openQqLog(row) {
  logQq.value = String((row && row.qq) || "");
  logView.value = null;
  logErr.value = "";
  loadQqLog();
}

function closeQqLog() {
  logQq.value = "";
  logView.value = null;
  logErr.value = "";
}

function pickLogDays(days) {
  logDays.value = days;
  loadQqLog();
}

function paintMembers(rows) {
  members.value = (rows || []).map((m) => ({
    ...m,
    storm_cap_text: m.storm_cap == null ? "" : String(m.storm_cap),
  }));
}

async function loadMembers() {
  const data = await api("/api/admin/members");
  paintMembers(data.members);
}

async function saveStormCap(m) {
  err.value = "";
  stormErr.value = { ...stormErr.value, [m.id]: "" };
  try {
    const data = await api("/api/admin/members/storm-cap", {
      user_id: m.id,
      cap: String(m.storm_cap_text ?? "").trim(),
    });
    paintMembers(data.members);
    const saved = members.value.find((row) => row.id === m.id);
    stormNotes.value = {
      ...stormNotes.value,
      [m.id]: saved && Number(saved.storm_cap) > 0
        ? "已保存，每天上限 " + saved.storm_cap + " 次"
        : "已保存，不拒绝",
    };
  } catch (e) {
    const message = e.message || "请求失败";
    err.value = message;
    stormErr.value = { ...stormErr.value, [m.id]: message };
    m.storm_cap_text = m.storm_cap == null ? "" : String(m.storm_cap);
  }
}

function cityName(id) {
  const hit = cities.value.find((c) => String(c.id) === String(id));
  return hit ? cityLabel(hit) : "";
}

function retreatText(mode, hops, city, fail) {
  let text = "不后退";
  if (mode === "hops") text = "后退 " + hops + " 座，朝向 " + (cityName(city) || "马奇诺");
  else if (mode === "city") text = "退到 " + (cityName(city) || city || "—");
  if ((mode === "hops" || mode === "city") && fail) text += "，没打成也退";
  return text;
}

function clearPages(m) {
  return m.clear_mode === "range" ? "第 " + m.clear_from + " 到 " + m.clear_to + " 页" : "前5页";
}

function prioText(m) {
  const rows = m.clear_priority || [];
  if (!rows.length) return "无";
  return rows.map((row) => row.uid + "（" + row.rank + "）").join("、");
}

function toggleMember(id) {
  openMember.value = openMember.value === id ? 0 : id;
}

function toggleTier(m) {
  if (openTier.value === m.id) {
    openTier.value = 0;
    return;
  }
  openTier.value = m.id;
  tierDraft.value = { ...tierDraft.value, [m.id]: m.tier || "初级" };
}

async function saveTier(m) {
  if (tierBusy.value) return;
  const tier = String(tierDraft.value[m.id] || "").trim();
  tierErr.value = { ...tierErr.value, [m.id]: "" };
  if (!TIERS.includes(tier)) {
    tierErr.value = { ...tierErr.value, [m.id]: "请选一个会员等级" };
    return;
  }
  err.value = "";
  tierBusy.value = m.id;
  try {
    const data = await api("/api/admin/members/tier", { user_id: m.id, tier });
    paintMembers(data.members);
    tierNote.value = { ...tierNote.value, [m.id]: "会员等级已改成 " + tier };
  } catch (e) {
    const message = e.message || "请求失败";
    err.value = message;
    tierErr.value = { ...tierErr.value, [m.id]: message };
  } finally {
    tierBusy.value = 0;
  }
}

function toggleExpiry(m) {
  if (openExpiry.value === m.id) {
    openExpiry.value = 0;
    return;
  }
  openExpiry.value = m.id;
  const day = expiryDay(m.expires_at);
  if (!expiryDraft.value[m.id] && day) {
    expiryDraft.value = { ...expiryDraft.value, [m.id]: day };
  }
}

async function saveExpiry(m, action) {
  if (expiryBusy.value) return;
  if (action === "expire") {
    if (!window.confirm("把 " + m.username + " 设为已过期？有效期会改成北京时间的昨天。未勾选「显示过期」时，这个账号会从列表里消失。")) return;
  }
  if (action === "month" && !String(m.expires_at || "").trim()) {
    if (!window.confirm(m.username + " 现在不限期。延长一个月会从今天起算，改成一个月后到期。")) return;
  }
  err.value = "";
  expiryErr.value = { ...expiryErr.value, [m.id]: "" };
  const body = { user_id: m.id, action };
  if (action === "custom") {
    body.expires_at = String(expiryDraft.value[m.id] || "").trim();
    if (!body.expires_at) {
      expiryErr.value = { ...expiryErr.value, [m.id]: "请选一个日期" };
      return;
    }
  }
  expiryBusy.value = m.id;
  try {
    const data = await api("/api/admin/members/expiry", body);
    paintMembers(data.members);
    const saved = (data.members || []).find((row) => row.id === m.id);
    const day = saved && saved.expires_at ? String(saved.expires_at).slice(0, 10) : "";
    expiryNote.value = {
      ...expiryNote.value,
      [m.id]: action === "expire" ? "已设为过期" : "有效期已改到 " + (day || "不限期"),
    };
    if (day) expiryDraft.value = { ...expiryDraft.value, [m.id]: day };
  } catch (e) {
    const message = e.message || "请求失败";
    err.value = message;
    expiryErr.value = { ...expiryErr.value, [m.id]: message };
  } finally {
    expiryBusy.value = 0;
  }
}

async function clearMember(m) {
  if (clearing.value) return;
  if (!window.confirm("清空 " + m.username + " 的账号配置？档位、有效期、管理、攻打 QQ 和超级强攻拒绝上限会留下。")) return;
  err.value = "";
  clearing.value = m.id;
  try {
    const data = await api("/api/admin/members/clear", { user_id: m.id });
    paintMembers(data.members);
  } finally {
    clearing.value = 0;
  }
}

async function loadAdmin() {
  const jobs = await Promise.allSettled([loadCities(), loadMembers(), loadScan(), refreshFighters()]);
  const failed = jobs.find((job) => job.status === "rejected");
  if (failed) err.value = failed.reason.message || "有一块没加载出来";
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
  await loadAdmin();
}

async function logout() {
  await api("/api/logout", {});
  me.value = null;
  members.value = [];
  openMember.value = 0;
  openExpiry.value = 0;
  openTier.value = 0;
  fighters.value = [];
  scanRanges.value = [];
  clearMemberFilters();
  clearFighterFilters();
  sheet.value = "members";
  memberPage.value = 1;
  fighterPage.value = 1;
}

onMounted(async () => {
  await loadMe();
  fighterTimer = setInterval(() => {
    if (me.value && me.value.admin) refreshFighters().catch(() => {});
  }, 5000);
});
onUnmounted(() => {
  clearInterval(fighterTimer);
});
</script>

<template>
  <main :class="{ 'admin-log': !!logQq }">
    <h1>管理</h1>
    <p class="lead" v-if="!me">
      管理员登录后，在这里查看成员配置、安排扫描，并查看每个攻打 QQ 的状态。
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
        <a class="ghost" href="/pwd-lab">密码登录测试</a>
        <a class="ghost" href="/">回到订阅</a>
        <button type="button" class="ghost" @click="logout">退出</button>
      </p>
      <div class="sheet-switch" role="tablist" aria-label="账号表">
        <button type="button" role="tab" :aria-selected="sheet === 'members'" :class="{ ghost: sheet !== 'members' }" @click="sheet = 'members'">成员账号</button>
        <button type="button" role="tab" :aria-selected="sheet === 'fighters'" :class="{ ghost: sheet !== 'fighters' }" @click="sheet = 'fighters'">攻打 QQ</button>
      </div>
      <template v-if="sheet === 'members'">
      <p class="muted">看每个登录账号自己改过的配置。清空后回到默认，档位、有效期、管理、攻打 QQ 和超级强攻拒绝上限不动。点「会员等级」可以改成初级、中级、高级或高级pro。点「有效期」可以设为过期、延长一个月，或改成指定日期。过期账号默认不显示，有效期当天仍然有效，没填有效期的一直显示。每页 20 个，点表头可以排序。</p>
      <form class="filters" @submit.prevent>
        <label>搜索<input v-model="memberQ" placeholder="账号或 QQ" /></label>
        <label>区服
          <select v-model="memberRegion">
            <option value="">全部</option>
            <option value="0">未识别</option>
            <option v-for="r in regionOptions" :key="'mr' + r" :value="String(r)">{{ r }}区</option>
          </select>
        </label>
        <label>状态
          <select v-model="memberOnline">
            <option value="">全部</option>
            <option value="on">在线</option>
            <option value="off">没在跑</option>
          </select>
        </label>
        <label>档
          <select v-model="memberTier">
            <option value="">全部</option>
            <option v-for="t in TIERS" :key="'ft' + t">{{ t }}</option>
          </select>
        </label>
        <label class="choice" title="有效期按北京时间。到期当天仍显示，没填有效期的一直显示。">
          <input type="checkbox" v-model="showExpired" />
          显示过期
        </label>
        <button v-if="memberFilterOn" type="button" class="ghost" @click="clearMemberFilters">清除筛选</button>
      </form>
      <p class="muted pager" v-if="members.length">
        显示 {{ memberView.from }}–{{ memberView.to }} / {{ shownMembers.length }} 个<template v-if="shownMembers.length !== members.length">，共 {{ members.length }} 个</template><template v-if="hiddenExpiredMembers">，已隐藏 {{ hiddenExpiredMembers }} 个过期</template>
        <template v-if="memberView.pages > 1">
          <button type="button" class="ghost" :disabled="memberView.page <= 1" @click="memberPage = memberView.page - 1">上一页</button>
          第 {{ memberView.page }} / {{ memberView.pages }} 页
          <button type="button" class="ghost" :disabled="memberView.page >= memberView.pages" @click="memberPage = memberView.page + 1">下一页</button>
        </template>
      </p>
      <div class="wide" v-if="memberView.rows.length">
        <table class="members">
          <thead>
            <tr>
              <th><button type="button" class="sort" :class="{ on: memberSort === 'username' }" title="点击排序" @click="toggleSort('m', 'username')">登录账号{{ sortHint('username', memberSort, memberDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: memberSort === 'tier' }" title="点击排序" @click="toggleSort('m', 'tier')">档{{ sortHint('tier', memberSort, memberDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: memberSort === 'expires' }" title="点击排序" @click="toggleSort('m', 'expires')">有效期{{ sortHint('expires', memberSort, memberDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: memberSort === 'qq' }" title="点击排序" @click="toggleSort('m', 'qq')">攻打 QQ{{ sortHint('qq', memberSort, memberDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: memberSort === 'region' }" title="点击排序" @click="toggleSort('m', 'region')">区服{{ sortHint('region', memberSort, memberDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: memberSort === 'online' }" title="点击排序" @click="toggleSort('m', 'online')">状态{{ sortHint('online', memberSort, memberDir) }}</button></th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="m in memberView.rows" :key="m.id">
              <tr>
                <td>{{ m.username }}<span v-if="m.admin" class="muted"> · 管理</span></td>
                <td>{{ m.tier || "初级" }}</td>
                <td>{{ m.expires_at || "不限期" }}<span v-if="memberExpired(m)" class="off"> 已过期</span></td>
                <td>{{ m.attack_qq || "—" }}</td>
                <td>{{ regionText(m) }}</td>
                <td :class="onlineOf(m) ? 'on' : 'off'">{{ onlineOf(m) ? "在线" : "没在跑" }}</td>
                <td class="member-ops">
                  <button type="button" class="ghost" @click="toggleMember(m.id)">{{ openMember === m.id ? "收起" : "配置" }}</button>
                  <button type="button" class="ghost" :disabled="clearing === m.id" @click="clearMember(m).catch((e) => (err = e.message))">清空</button>
                  <button type="button" class="ghost" :class="{ here: openTier === m.id }" :aria-expanded="openTier === m.id" @click="toggleTier(m)">{{ openTier === m.id ? "收起" : "会员等级" }}</button>
                  <button type="button" class="ghost" :class="{ here: openExpiry === m.id }" :aria-expanded="openExpiry === m.id" @click="toggleExpiry(m)">{{ openExpiry === m.id ? "收起" : "有效期" }}</button>
                </td>
              </tr>
              <tr v-if="openTier === m.id">
                <td colspan="7" class="member-cfg member-expiry">
                  <p>当前：{{ m.tier || "初级" }}。中级起可以远程扫码攻打。高级再加上自动索敌、自动锁敌和清城高级配置。高级pro再加上活动助手和基地战。</p>
                  <form class="expiry-actions" @submit.prevent="saveTier(m)">
                    <label>会员等级
                      <select v-model="tierDraft[m.id]">
                        <option v-for="t in TIERS" :key="m.id + t" :value="t">{{ t }}</option>
                      </select>
                    </label>
                    <button type="submit" :disabled="tierBusy === m.id">保存</button>
                  </form>
                  <p v-if="tierErr[m.id]" class="err">{{ tierErr[m.id] }}</p>
                  <p v-else-if="tierNote[m.id]" class="muted">{{ tierNote[m.id] }}</p>
                </td>
              </tr>
              <tr v-if="openExpiry === m.id">
                <td colspan="7" class="member-cfg member-expiry">
                  <p>当前：{{ m.expires_at || "不限期" }}<span v-if="memberExpired(m)">，已过期</span>。按北京时间，到期当天仍然有效。</p>
                  <div class="expiry-actions">
                    <button type="button" :disabled="expiryBusy === m.id" @click="saveExpiry(m, 'expire').catch((e) => (err = e.message))">设为过期</button>
                    <button type="button" :disabled="expiryBusy === m.id" @click="saveExpiry(m, 'month').catch((e) => (err = e.message))">延长一个月</button>
                    <form @submit.prevent="saveExpiry(m, 'custom').catch((e) => (err = e.message))">
                      <label>自定义
                        <input type="date" v-model="expiryDraft[m.id]" />
                      </label>
                      <button type="submit" :disabled="expiryBusy === m.id">保存</button>
                    </form>
                  </div>
                  <p v-if="expiryErr[m.id]" class="err">{{ expiryErr[m.id] }}</p>
                  <p v-else-if="expiryNote[m.id]" class="muted">{{ expiryNote[m.id] }}</p>
                </td>
              </tr>
              <tr v-if="openMember === m.id">
                <td colspan="7" class="member-cfg">
                  <p>订阅 QQ：{{ m.qq_target || "—" }}</p>
                  <p>挂机：{{ m.hold_min || 0 }} 分钟<template v-if="m.hold_all"> · 全天候</template></p>
                  <p>定时关闭：{{ m.off_at || "未设" }}</p>
                  <p>自动锁敌：{{ m.auto_lock ? "开" : "关" }} · 锁敌恢复卡 {{ m.lock_cards }} 张/小时</p>
                  <p>恢复卡上限：{{ m.card_max }} · 摩多：{{ m.modo_cards || 0 }}</p>
                  <p>城外补士气：{{ m.away_morale ? "士气低于 " + m.away_morale : "不补" }}</p>
                  <p>打完后退：{{ retreatText(m.retreat_mode, m.retreat_hops, m.retreat_city, m.retreat_fail) }}</p>
                  <p>清城扫页：{{ clearPages(m) }} · 空城再打 {{ m.clear_wait ? m.clear_wait + " 分钟" : "结束" }} · 扫页冷却 {{ m.clear_scan ? m.clear_scan + " 秒" : "每次出手" }}</p>
                  <p>清城后退：{{ retreatText(m.clear_retreat_mode, m.clear_retreat_hops, m.clear_retreat_city, m.clear_retreat_fail) }}</p>
                  <p>优先 UID：{{ prioText(m) }}</p>
                  <p>日常定时：{{ m.daily_at || "未设" }}</p>
                  <p>刷摩多定时：{{ m.modo_at || "未设" }} · 恢复卡 {{ m.modo_cards || 0 }}</p>
                  <p>
                    超级强攻：今日已拒绝 {{ m.storm_used || 0 }} 次
                    <template v-if="!(Number(m.storm_cap) > 0)">· 不拒绝</template>
                    <template v-else>· 上限 {{ m.storm_cap }} 次<template v-if="(m.storm_used || 0) >= m.storm_cap">，已停</template></template>
                  </p>
                  <form class="storm-cap" @submit.prevent="saveStormCap(m)">
                    <label>每日上限
                      <input v-model="m.storm_cap_text" inputmode="numeric" placeholder="不拒绝" />
                    </label>
                    <button type="submit">保存</button>
                    <span v-if="stormErr[m.id]" class="err">{{ stormErr[m.id] }}</span>
                    <span v-else class="muted">{{ stormNotes[m.id] }}</span>
                  </form>
                  <p class="muted">登录账号默认不拒绝。留空或填 0 都不自动拒绝；填 0 时，这个登录账号绑定的攻打 QQ 一律不拒绝。只有这里填了大于 0 的次数才会拒绝，按北京时间每天 0 点重新计算。成功发出拒绝才算一次，达到上限后这个登录账号今天不再自动拒绝。</p>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <p v-if="!members.length" class="muted">还没有登录账号。</p>
      <p v-else-if="!shownMembers.length" class="muted">{{ hiddenExpiredMembers && !memberFilterOn ? "过期账号已隐藏。" : "没有符合筛选的账号。" }}</p>
      </template>
      <template v-else-if="logQq">
        <div class="qq-log-head">
          <button type="button" class="ghost" @click="closeQqLog">返回攻打 QQ</button>
          <h2>攻打 QQ {{ logQq }}</h2>
          <button type="button" class="ghost" :disabled="logBusy" @click="loadQqLog">{{ logBusy ? "正在读" : "刷新" }}</button>
        </div>
        <div class="qq-days" role="tablist" aria-label="日志范围">
          <button type="button" class="ghost" :class="{ here: logDays === '1' }" @click="pickLogDays('1')">今天</button>
          <button type="button" class="ghost" :class="{ here: logDays === '3' }" @click="pickLogDays('3')">近 3 天</button>
          <button type="button" class="ghost" :class="{ here: logDays === '7' }" @click="pickLogDays('7')">近 7 天</button>
        </div>
        <p v-if="logErr" class="err">{{ logErr }}</p>
        <p v-else-if="logBusy && !logView" class="muted">正在读日志。</p>
        <template v-if="logView">
          <p>登录账号：{{ accountLine(logView.accounts) }}</p>
          <p class="muted">留空 UID 的是清城。同一条过了分钟再排队，就是空城再打。打完、空了、还有打不过的人，都按这个分钟再来。城里没人时人留在原地。人已经在要退到的城里就不再移动。当前说明写在订单上，每一次再打写在运行日志里。空城再打：{{ clearWaitLine(logView.accounts) }}。</p>
          <h3>提交的攻打</h3>
          <div class="wide qq-orders" v-if="logView.orders.length">
            <table>
              <thead>
                <tr>
                  <th>提交</th>
                  <th>登录账号</th>
                  <th>类型</th>
                  <th>城市</th>
                  <th>对象</th>
                  <th>状态</th>
                  <th>打中</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="it in logView.orders" :key="it.id">
                  <td>{{ it.created_at || "—" }}</td>
                  <td>{{ it.username || "—" }}</td>
                  <td>{{ it.label }}</td>
                  <td>{{ orderWhere(it) }}</td>
                  <td>{{ orderWho(it) }}</td>
                  <td>{{ orderStatus(it.status) }}<template v-if="it.run_at"> · {{ it.run_at }}</template></td>
                  <td>{{ beatText(it) }}</td>
                  <td class="reason">{{ it.reason || "—" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="muted">还没有提交过攻打。</p>
          <h3>运行日志</h3>
          <p v-if="logView.lines_cut" class="muted">行数比较多，这里只留最近的一部分。</p>
          <pre v-if="logView.lines.length" class="qq-log">{{ logView.lines.join("\n") }}</pre>
          <p v-else class="muted">这几天的运行日志里没有对得上的行。</p>
          <h3>任务日志</h3>
          <p v-if="!(logView.tasks || []).length" class="muted">没有按任务分开的日志。</p>
          <details v-for="task in logView.tasks" :key="task.name" class="qq-task-file" open>
            <summary>{{ task.name }}<span v-if="task.cut" class="muted"> · 只留末尾</span></summary>
            <pre v-if="task.lines.length" class="qq-task">{{ task.lines.join("\n") }}</pre>
            <p v-else class="muted">这个任务这几天没有新行。</p>
          </details>
        </template>
      </template>
      <template v-else>
      <p class="muted">每 5 秒刷新。每个攻打 QQ 一条线程，票据在对应的 cookie 文件里。登录账号过期后，这条默认也不显示。每页 20 个，点表头可以排序。点「日志」看这个号提交的攻打和对应日志。</p>
      <form class="filters" @submit.prevent>
        <label>搜索<input v-model="fighterQ" placeholder="账号或 QQ" /></label>
        <label>区服
          <select v-model="fighterRegion">
            <option value="">全部</option>
            <option value="0">未识别</option>
            <option v-for="r in regionOptions" :key="'fr' + r" :value="String(r)">{{ r }}区</option>
          </select>
        </label>
        <label>状态
          <select v-model="fighterOnline">
            <option value="">全部</option>
            <option value="on">在线</option>
            <option value="off">没在跑</option>
          </select>
        </label>
        <label class="choice" title="跟成员账号用同一个开关。登录账号过期后，这条攻打 QQ 默认不显示。">
          <input type="checkbox" v-model="showExpired" />
          显示过期
        </label>
        <button v-if="fighterFilterOn" type="button" class="ghost" @click="clearFighterFilters">清除筛选</button>
      </form>
      <p class="muted pager" v-if="fighters.length">
        显示 {{ fighterView.from }}–{{ fighterView.to }} / {{ shownFighters.length }} 个<template v-if="shownFighters.length !== fighters.length">，共 {{ fighters.length }} 个</template><template v-if="hiddenExpiredFighters">，已隐藏 {{ hiddenExpiredFighters }} 个过期</template>
        <template v-if="fighterView.pages > 1">
          <button type="button" class="ghost" :disabled="fighterView.page <= 1" @click="fighterPage = fighterView.page - 1">上一页</button>
          第 {{ fighterView.page }} / {{ fighterView.pages }} 页
          <button type="button" class="ghost" :disabled="fighterView.page >= fighterView.pages" @click="fighterPage = fighterView.page + 1">下一页</button>
        </template>
      </p>
      <div class="wide" v-if="fighterView.rows.length">
        <table class="fighters">
          <thead>
            <tr>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'username' }" title="点击排序" @click="toggleSort('f', 'username')">登录账号{{ sortHint('username', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'qq' }" title="点击排序" @click="toggleSort('f', 'qq')">攻打 QQ{{ sortHint('qq', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'region' }" title="点击排序" @click="toggleSort('f', 'region')">区服{{ sortHint('region', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'online' }" title="点击排序" @click="toggleSort('f', 'online')">状态{{ sortHint('online', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'here' }" title="点击排序" @click="toggleSort('f', 'here')">人在{{ sortHint('here', fighterSort, fighterDir) }}</button></th>
              <th>cookie</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in fighterView.rows" :key="f.user_id">
              <td>{{ f.username }}<span v-if="fighterExpired(f)" class="off"> · 已过期</span></td>
              <td>{{ f.qq }}</td>
              <td>{{ regionText(f) }}</td>
              <td :class="f.online ? 'on' : 'off'">{{ procText(f) }}</td>
              <td>{{ f.here || "—" }}</td>
              <td><code>{{ f.cookie }}</code></td>
              <td><button type="button" class="ghost" @click="openQqLog(f)">日志</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!fighters.length" class="muted">还没有绑定攻打 QQ。</p>
      <p v-else-if="!shownFighters.length" class="muted">{{ hiddenExpiredFighters && !fighterFilterOn ? "过期账号的攻打 QQ 已隐藏。" : "没有符合筛选的攻打 QQ。" }}</p>
      </template>
      <h2 v-if="sheet === 'members' || !logQq">扫描安排</h2>
      <form v-if="sheet === 'members' || !logQq" class="stack scan-form" @submit.prevent="saveScan().catch((e) => (err = e.message))">
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
    </template>
    <p class="err">{{ err }}</p>
  </main>
</template>
