<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import "./base.css";

const me = ref(null);
const username = ref("");
const password = ref("");
const err = ref("");
const cities = ref([]);
const members = ref([]);
const openMember = ref(0);
const clearing = ref(0);
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
let fighterTimer = 0;
const TIER_RANK = { 初级: 1, 中级: 2, 高级: 3 };

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
  if (!p.online) return p.paused && p.detail ? p.detail : "没在跑";
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

function procOf(row) {
  return row.user_id ? row : fighterOf(row.id);
}

function inGame(p) {
  if (!p || !p.online) return false;
  if (p.need_login || p.phase === "login") return false;
  const d = String(p.detail || "");
  if (d.startsWith("登录中") || d.startsWith("登录已失效")) return false;
  return true;
}

function statusKind(row) {
  const p = procOf(row);
  if (inGame(p)) return "on";
  if (p && p.online && (p.phase === "login" || String(p.detail || "").startsWith("登录中"))) return "login";
  return "off";
}

function statusText(row) {
  const k = statusKind(row);
  return k === "on" ? "在线" : k === "login" ? "登录中" : "没在跑";
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

function hitStatus(sel, row) {
  if (!sel) return true;
  return statusKind(row) === sel;
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
  if (col === "expires") return (m.expires_at || "").trim() || "9999-99-99";
  if (col === "qq") return m.attack_qq || "";
  if (col === "region") return regionOf(m) || 9999;
  if (col === "online") {
    const k = statusKind(m);
    return k === "on" ? 2 : k === "login" ? 1 : 0;
  }
  return m.id;
}

function fighterKey(f, col) {
  if (col === "username") return f.username || "";
  if (col === "qq") return String(f.qq || "");
  if (col === "region") return regionOf(f) || 9999;
  if (col === "online") {
    const k = statusKind(f);
    return k === "on" ? 2 : k === "login" ? 1 : 0;
  }
  if (col === "here") return f.here || "";
  return f.user_id;
}

const shownMembers = computed(() => {
  const rows = members.value.filter((m) =>
    hitQuery(memberQ.value, m.username, m.attack_qq, m.qq_target)
    && hitRegion(memberRegion.value, m.region)
    && hitStatus(memberOnline.value, m)
    && (!memberTier.value || (m.tier || "初级") === memberTier.value));
  const col = memberSort.value;
  const dir = memberDir.value;
  rows.sort((a, b) => cmpVal(memberKey(a, col), memberKey(b, col), dir) || a.id - b.id);
  return rows;
});

const shownFighters = computed(() => {
  const rows = fighters.value.filter((f) =>
    hitQuery(fighterQ.value, f.username, f.qq)
    && hitRegion(fighterRegion.value, regionOf(f))
    && hitStatus(fighterOnline.value, f));
  const col = fighterSort.value;
  const dir = fighterDir.value;
  rows.sort((a, b) => cmpVal(fighterKey(a, col), fighterKey(b, col), dir) || a.user_id - b.user_id);
  return rows;
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
}

function clearFighterFilters() {
  fighterQ.value = "";
  fighterRegion.value = "";
  fighterOnline.value = "";
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

async function loadMembers() {
  const data = await api("/api/admin/members");
  members.value = data.members || [];
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

async function clearMember(m) {
  if (clearing.value) return;
  if (!window.confirm("清空 " + m.username + " 的账号配置？档位、有效期、管理和攻打 QQ 会留下。")) return;
  err.value = "";
  clearing.value = m.id;
  try {
    const data = await api("/api/admin/members/clear", { user_id: m.id });
    members.value = data.members || [];
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
  fighters.value = [];
  scanRanges.value = [];
  clearMemberFilters();
  clearFighterFilters();
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
  <main>
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
      <h2>成员账号</h2>
      <p class="muted">看每个登录账号自己改过的配置。清空后回到默认，档位、有效期、管理和攻打 QQ 不动。点表头可以排序。</p>
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
            <option value="login">登录中</option>
            <option value="off">没在跑</option>
          </select>
        </label>
        <label>档
          <select v-model="memberTier">
            <option value="">全部</option>
            <option>初级</option>
            <option>中级</option>
            <option>高级</option>
          </select>
        </label>
        <button v-if="memberFilterOn" type="button" class="ghost" @click="clearMemberFilters">清除筛选</button>
      </form>
      <p class="muted" v-if="members.length">显示 {{ shownMembers.length }} / {{ members.length }} 个</p>
      <div class="wide" v-if="shownMembers.length">
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
            <template v-for="m in shownMembers" :key="m.id">
              <tr>
                <td>{{ m.username }}<span v-if="m.admin" class="muted"> · 管理</span></td>
                <td>{{ m.tier || "初级" }}</td>
                <td>{{ m.expires_at || "不限期" }}</td>
                <td>{{ m.attack_qq || "—" }}</td>
                <td>{{ regionText(m) }}</td>
                <td :class="statusKind(m) === 'on' ? 'on' : 'off'">{{ statusText(m) }}</td>
                <td>
                  <button type="button" class="ghost" @click="toggleMember(m.id)">{{ openMember === m.id ? "收起" : "配置" }}</button>
                  <button type="button" class="ghost" :disabled="clearing === m.id" @click="clearMember(m).catch((e) => (err = e.message))">清空</button>
                </td>
              </tr>
              <tr v-if="openMember === m.id">
                <td colspan="7" class="member-cfg">
                  <p>订阅 QQ：{{ m.qq_target || "—" }}</p>
                  <p>挂机：{{ m.hold_min || 0 }} 分钟<template v-if="m.hold_all"> · 全天候</template></p>
                  <p>定时关闭：{{ m.off_at || "未设" }}</p>
                  <p>自动锁敌：{{ m.auto_lock ? "开" : "关" }} · 锁敌恢复卡 {{ m.lock_cards }} 张/小时</p>
                  <p>恢复卡上限：{{ m.card_max }} · 摩多：{{ m.modo_cards || 0 }}</p>
                  <p>打完后退：{{ retreatText(m.retreat_mode, m.retreat_hops, m.retreat_city, m.retreat_fail) }}</p>
                  <p>清城扫页：{{ clearPages(m) }} · 空城再打 {{ m.clear_wait ? m.clear_wait + " 分钟" : "结束" }} · 扫页冷却 {{ m.clear_scan ? m.clear_scan + " 秒" : "每次出手" }}</p>
                  <p>清城后退：{{ retreatText(m.clear_retreat_mode, m.clear_retreat_hops, m.clear_retreat_city, m.clear_retreat_fail) }}</p>
                  <p>优先 UID：{{ prioText(m) }}</p>
                  <p>日常定时：{{ m.daily_at || "未设" }}</p>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <p v-if="!members.length" class="muted">还没有登录账号。</p>
      <p v-else-if="!shownMembers.length" class="muted">没有符合筛选的账号。</p>
      <h2>攻打 QQ</h2>
      <p class="muted">每 5 秒刷新。每个攻打 QQ 一条线程，票据在对应的 cookie 文件里。点表头可以排序。</p>
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
            <option value="login">登录中</option>
            <option value="off">没在跑</option>
          </select>
        </label>
        <button v-if="fighterFilterOn" type="button" class="ghost" @click="clearFighterFilters">清除筛选</button>
      </form>
      <p class="muted" v-if="fighters.length">显示 {{ shownFighters.length }} / {{ fighters.length }} 个</p>
      <div class="wide" v-if="shownFighters.length">
        <table>
          <thead>
            <tr>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'username' }" title="点击排序" @click="toggleSort('f', 'username')">登录账号{{ sortHint('username', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'qq' }" title="点击排序" @click="toggleSort('f', 'qq')">攻打 QQ{{ sortHint('qq', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'region' }" title="点击排序" @click="toggleSort('f', 'region')">区服{{ sortHint('region', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'online' }" title="点击排序" @click="toggleSort('f', 'online')">状态{{ sortHint('online', fighterSort, fighterDir) }}</button></th>
              <th><button type="button" class="sort" :class="{ on: fighterSort === 'here' }" title="点击排序" @click="toggleSort('f', 'here')">人在{{ sortHint('here', fighterSort, fighterDir) }}</button></th>
              <th>cookie</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in shownFighters" :key="f.user_id">
              <td>{{ f.username }}</td>
              <td>{{ f.qq }}</td>
              <td>{{ regionText(f) }}</td>
              <td :class="inGame(f) ? 'on' : 'off'">{{ procText(f) }}</td>
              <td>{{ f.here || "—" }}</td>
              <td><code>{{ f.cookie }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!fighters.length" class="muted">还没有绑定攻打 QQ。</p>
      <p v-else-if="!shownFighters.length" class="muted">没有符合筛选的攻打 QQ。</p>
      <h2>扫描安排</h2>
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
    </template>
    <p class="err">{{ err }}</p>
  </main>
</template>
