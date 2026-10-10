<script setup>
import { onMounted, onUnmounted, ref } from "vue";
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
let fighterTimer = 0;

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
      <p class="muted">看每个登录账号自己改过的配置。清空后回到默认，档位、有效期、管理和攻打 QQ 不动。</p>
      <div class="wide" v-if="members.length">
        <table class="members">
          <thead>
            <tr><th>登录账号</th><th>档</th><th>有效期</th><th>攻打 QQ</th><th>区服</th><th></th></tr>
          </thead>
          <tbody>
            <template v-for="m in members" :key="m.id">
              <tr>
                <td>{{ m.username }}<span v-if="m.admin" class="muted"> · 管理</span></td>
                <td>{{ m.tier || "初级" }}</td>
                <td>{{ m.expires_at || "不限期" }}</td>
                <td>{{ m.attack_qq || "—" }}</td>
                <td>{{ m.region ? m.region + "区" : "—" }}</td>
                <td>
                  <button type="button" class="ghost" @click="toggleMember(m.id)">{{ openMember === m.id ? "收起" : "配置" }}</button>
                  <button type="button" class="ghost" :disabled="clearing === m.id" @click="clearMember(m).catch((e) => (err = e.message))">清空</button>
                </td>
              </tr>
              <tr v-if="openMember === m.id">
                <td colspan="6" class="member-cfg">
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
      <h2>攻打 QQ</h2>
      <p class="muted">每 5 秒刷新。每个攻打 QQ 一条线程，票据在对应的 cookie 文件里。</p>
      <table>
        <thead>
          <tr><th>登录账号</th><th>攻打 QQ</th><th>状态</th><th>人在</th><th>cookie</th></tr>
        </thead>
        <tbody>
          <tr v-for="f in fighters" :key="f.user_id">
            <td>{{ f.username }}</td>
            <td>{{ f.qq }}</td>
            <td :class="f.online ? 'on' : 'off'">{{ procText(f) }}</td>
            <td>{{ f.here || "—" }}</td>
            <td><code>{{ f.cookie }}</code></td>
          </tr>
        </tbody>
      </table>
      <p v-if="!fighters.length" class="muted">还没有绑定攻打 QQ。</p>
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
