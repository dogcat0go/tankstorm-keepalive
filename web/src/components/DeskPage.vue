<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import SquadPanel from "./SquadPanel.vue";

const view = ref({ cities: [], accounts: [], teams: [], countries: [], width: 320, height: 180 });
const err = ref("");
const lines = ref([]);
const busy = ref(false);
const hits = ref([]);
const hitTotal = ref(0);
const hitPage = ref(1);
const hitPages = ref(1);
const hitWho = ref("");
const hitResult = ref("");
const hitQ = ref("");
const teamId = ref(0);
const tool = ref("");
const armed = ref(false);
const asked = ref(0);
const cityId = ref(0);
const organizerId = ref("");
const countryId = ref("");
const newName = ref("国战编队");
const zoom = ref(1);
const camX = ref(0);
const camY = ref(0);
const mapDrag = ref(false);
const mapSvg = ref(null);
let timer = 0;
let dragMoved = false;
let dragAt = null;
let camSet = false;

const PALETTE = ["#c45c26", "#2f6f4e", "#3d5a80", "#8c3a4b", "#6b5b2a", "#3f6f8c", "#7a4e2d", "#4d5d3a", "#6a4c7a", "#8a5a2b", "#2c6e6a", "#7d3b3b", "#4a6741", "#5c4a32", "#345e7d", "#6e4b3a"];

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "请求失败");
  return data;
}

function placeCam() {
  const w = Number(view.value.width) || 0;
  const h = Number(view.value.height) || 0;
  if (!w || !h || camSet) return;
  camX.value = w / 2;
  camY.value = h / 2;
  camSet = true;
}

async function load() {
  const data = await api("/api/admin/desk");
  view.value = data;
  placeCam();
  if (!view.value.teams.some((t) => t.id === teamId.value)) teamId.value = view.value.teams[0]?.id || 0;
}

const mapFrame = computed(() => {
  const w = Number(view.value.width) || 1;
  const h = Number(view.value.height) || 1;
  const vw = w / zoom.value;
  const vh = h / zoom.value;
  return `${camX.value - vw / 2} ${camY.value - vh / 2} ${vw} ${vh}`;
});

function clampCam() {
  const w = Number(view.value.width) || 1;
  const h = Number(view.value.height) || 1;
  const vw = w / zoom.value;
  const vh = h / zoom.value;
  camX.value = vw >= w ? w / 2 : Math.min(w - vw / 2, Math.max(vw / 2, camX.value));
  camY.value = vh >= h ? h / 2 : Math.min(h - vh / 2, Math.max(vh / 2, camY.value));
}

function unitScale(svg) {
  const m = svg && svg.getScreenCTM && svg.getScreenCTM();
  if (!m || !m.a || !m.d) return { x: 1, y: 1 };
  return { x: 1 / m.a, y: 1 / m.d };
}

function onMapWheel(e) {
  const svg = mapSvg.value;
  if (!svg || !svg.getScreenCTM()) return;
  const pt = svg.createSVGPoint();
  pt.x = e.clientX;
  pt.y = e.clientY;
  const here = pt.matrixTransform(svg.getScreenCTM().inverse());
  const step = e.deltaMode === 1 ? 0.06 : 0.0016;
  const next = Math.min(8, Math.max(1, zoom.value * Math.exp(-e.deltaY * step)));
  if (next === zoom.value) return;
  const w = Number(view.value.width) || 1;
  const h = Number(view.value.height) || 1;
  const vw = w / zoom.value;
  const vh = h / zoom.value;
  const fx = (here.x - (camX.value - vw / 2)) / vw;
  const fy = (here.y - (camY.value - vh / 2)) / vh;
  const nw = w / next;
  const nh = h / next;
  camX.value = here.x - fx * nw + nw / 2;
  camY.value = here.y - fy * nh + nh / 2;
  zoom.value = next;
  clampCam();
}

function onMapDown(e) {
  if (e.button !== 0) return;
  dragMoved = false;
  dragAt = { x: e.clientX, y: e.clientY };
  window.addEventListener("pointermove", onMapMove);
  window.addEventListener("pointerup", onMapUp);
  window.addEventListener("pointercancel", onMapUp);
}

function onMapMove(e) {
  if (!dragAt) return;
  const dx = e.clientX - dragAt.x;
  const dy = e.clientY - dragAt.y;
  if (!dragMoved) {
    if (dx * dx + dy * dy <= 9) return;
    dragMoved = true;
    mapDrag.value = true;
  }
  const u = unitScale(mapSvg.value);
  camX.value -= dx * u.x;
  camY.value -= dy * u.y;
  clampCam();
  dragAt = { x: e.clientX, y: e.clientY };
}

function onMapUp() {
  window.removeEventListener("pointermove", onMapMove);
  window.removeEventListener("pointerup", onMapUp);
  window.removeEventListener("pointercancel", onMapUp);
  dragAt = null;
  mapDrag.value = false;
}

const team = computed(() => view.value.teams.find((t) => t.id === teamId.value) || null);
const accounts = computed(() => view.value.accounts || []);
const byId = computed(() => {
  const map = {};
  for (const a of accounts.value) map[a.id] = a;
  return map;
});
const city = computed(() => (view.value.cities || []).find((c) => c.id === cityId.value) || null);
const organizer = computed(() => byId.value[Number(organizerId.value)] || null);

watch(organizer, (who) => {
  if (who && who.country) countryId.value = String(who.country);
});
watch([teamId, hitWho, hitResult, hitQ], () => {
  hitPage.value = 1;
});

const squad = computed(() => {
  const current = team.value;
  if (!current) return null;
  const who = byId.value[current.organizer_id];
  return {
    team_id: current.id,
    name: current.name || "国战编队",
    region: current.region,
    country_name: current.country_name || "",
    organizer: who && who.username ? who.username : "指挥者",
    self: false,
    state: "in",
    task: current.task || "idle",
    task_text: current.task_text || "未开始",
    roster: current.roster || [],
  };
});

const members = computed(() => {
  if (!team.value) return [];
  const ids = (team.value.roster || []).filter((m) => m.state === "in").map((m) => m.id);
  return ids.map((id) => byId.value[id]).filter(Boolean);
});

const supplyCap = computed(() => (team.value ? team.value.country_id * 1000 + 101 : 0));
const edges = computed(() => {
  const by = {};
  for (const c of view.value.cities || []) by[c.id] = c;
  const lines = [];
  for (const c of view.value.cities || []) {
    for (const n of c.near || []) {
      if (n > c.id && by[n]) lines.push({ id: c.id + "-" + n, x1: c.x, y1: c.y, x2: by[n].x, y2: by[n].y });
    }
  }
  return lines;
});

function canSupply(c) {
  const cap = supplyCap.value;
  return cap > 101 && (c.id === cap || (c.near || []).includes(cap));
}

function fill(id) {
  return PALETTE[Math.abs(Number(id) || 0) % PALETTE.length];
}

function hereOf(a) {
  return (view.value.cities || []).find((c) => c.id === a.here_id);
}

const looks = computed(() => {
  const rows = [];
  for (const a of members.value) {
    const look = a.look || {};
    if (look.city) rows.push({ who: a, look });
  }
  return rows;
});

const shownLook = computed(() => looks.value.find((row) => row.look.city === cityId.value) || null);
const enemies = computed(() => {
  const look = shownLook.value && shownLook.value.look;
  if (!look) return [];
  const mine = team.value ? Number(team.value.country_id) || 0 : 0;
  return (look.players || []).filter((p) => {
    const side = Number(p.country) || 0;
    return !mine || !side || side !== mine;
  });
});
const opening = computed(() => {
  if (!city.value || shownLook.value) return "";
  const id = String(city.value.id);
  for (const a of members.value) {
    const note = a.desk_note || "";
    if (note.includes("打开") && note.includes(id)) return note;
  }
  return "";
});

async function loadHits() {
  if (!teamId.value) {
    hits.value = [];
    hitTotal.value = 0;
    hitPages.value = 1;
    return;
  }
  const q = new URLSearchParams({
    team_id: String(teamId.value),
    page: String(hitPage.value),
    who: hitWho.value.trim(),
    result: hitResult.value,
    q: hitQ.value.trim(),
  });
  const data = await api("/api/admin/desk/hits?" + q.toString());
  hits.value = data.items || [];
  hitTotal.value = data.total || 0;
  hitPage.value = data.page || 1;
  hitPages.value = data.pages || 1;
}

async function refresh() {
  try {
    await load();
    await loadHits();
    err.value = "";
  } catch (e) {
    err.value = e.message;
  }
}

async function send(action, extra) {
  busy.value = true;
  err.value = "";
  try {
    const data = await api("/api/admin/desk", { action, ...extra });
    view.value = data;
    if (data.lines) lines.value = data.lines;
  } catch (e) {
    err.value = e.message;
  } finally {
    busy.value = false;
  }
}

async function makeTeam() {
  await send("team", {
    organizer_id: Number(organizerId.value),
    country_id: Number(countryId.value),
    name: newName.value,
  });
  teamId.value = view.value.teams[view.value.teams.length - 1]?.id || teamId.value;
}

async function renameSquad(name) {
  await send("rename", { team_id: team.value.id, name });
  if (err.value) throw new Error(err.value);
}

async function inviteMember(username) {
  await send("invite", { team_id: team.value.id, username });
  if (err.value) throw new Error(err.value);
}

async function kickMember(member) {
  await send("kick", { team_id: team.value.id, user_id: member.id });
  if (err.value) throw new Error(err.value);
}

async function setTask(state) {
  await send("task", { team_id: team.value.id, state });
  if (err.value) throw new Error(err.value);
}

function runOn(c, op) {
  if (!team.value || busy.value) return;
  if (team.value.task !== "run") {
    err.value = team.value.task === "pause" ? "编队任务暂停着，先继续" : "先开始编队任务";
    return;
  }
  if (op === "attack") {
    const owner = c.owner ? (view.value.countries.find((x) => x.id === c.owner)?.name || c.owner) : "归属未知";
    if (!window.confirm(`攻打 ${c.name}（${owner}）？队里在线的号会一起走。敌城有人就打，空城会占领。一次最多走进 8 座城。`)) return;
  }
  if (op === "morale") {
    if (!window.confirm("用体力补士气？人要在本国首都或相邻城，体力要大于 70。")) return;
    send("command", { team_id: team.value.id, op: "morale", city_id: 0 });
    return;
  }
  if (op === "open") asked.value = c.id;
  send("command", { team_id: team.value.id, op, city_id: c.id });
}

function pick(c) {
  if (dragMoved) return;
  cityId.value = c.id;
  if (!armed.value || !tool.value) return;
  armed.value = false;
  runOn(c, tool.value);
}

function useTool(op) {
  tool.value = op;
  if (op === "morale") {
    armed.value = false;
    runOn(city.value, op);
    return;
  }
  if (!city.value) {
    armed.value = true;
    err.value = "先点一座城";
    return;
  }
  armed.value = false;
  runOn(city.value, op);
}

onMounted(() => {
  refresh();
  timer = setInterval(refresh, 3000);
});
onUnmounted(() => {
  clearInterval(timer);
  onMapUp();
});
</script>

<template>
  <h2>指挥测试</h2>
  <p class="muted">编队名称默认是国战编队，可以改。创建后填写对方账号发出邀请。对方接受前是等待加入，接受后是已加入；退出或被踢出是已退出。名单上另显示在线或离线。指挥者可以踢人。管理在这个测试页。成员要在自己的页面看到编队，需要在账号配置里勾上「参与指挥测试」。编队任务有开始、暂停和结束。结束之后队员还在，下次可以直接再开始。进行中或暂停时，已加入的号自己的攻打和日常先停，退出或任务结束后再继续。下面是这一次任务的现场战报，最近 50 条，可以按人和结果筛。重新开始、结束、重新点攻打，或者有人加入、退出、被踢，这些记录就清掉。打不过仍记在原来的战败库。有人被打回去时，没在战败库里的队员会先打这个敌人。先点一座城，再点移动、攻打或打开城市。按住地图拖动，滚轮缩放。指挥者自己会做。已加入的号才会跟着做。移动只走路。补士气只在本国首都和相邻城，体力要大于 70。攻打会走进敌方城，有人就打，空城会占领，一次最多 8 座城。打开城市在旁边列出敌方，并写出城里一共有多少人。只看前 5 页，不打。</p>
  <p v-if="err" class="err">{{ err }}</p>
  <div class="desk-manage">
    <form class="lock-row" @submit.prevent="makeTeam()">
      <label>指挥者
        <select v-model="organizerId" required>
          <option value="" disabled>选一个已绑定攻打 QQ 的号</option>
          <option v-for="a in accounts" :key="a.id" :value="String(a.id)">
            {{ a.username }} · {{ a.region ? a.region + "区" : "区号未记" }} · {{ a.country_name || "国家未确认" }}
          </option>
        </select>
      </label>
      <label>国家
        <select v-model="countryId" required>
          <option value="" disabled>选国家</option>
          <option v-for="c in view.countries" :key="c.id" :value="String(c.id)">{{ c.name }}</option>
        </select>
      </label>
      <label>编队名称
        <input v-model="newName" maxlength="16" placeholder="国战编队" />
      </label>
      <button type="submit" :disabled="busy">创建编队</button>
    </form>
    <div class="lock-row">
      <label>当前编队
        <select v-model.number="teamId">
          <option :value="0">还没有编队</option>
          <option v-for="t in view.teams" :key="t.id" :value="t.id">
            {{ t.name || "国战编队" }} · {{ t.region }}区 {{ t.country_name || t.country_id }} · {{ byId[t.organizer_id]?.username || "指挥者" }}
          </option>
        </select>
      </label>
      <button v-if="team" type="button" class="ghost" :disabled="busy" @click="send('drop', { team_id: team.id })">解散</button>
    </div>
    <SquadPanel
      v-if="squad"
      lead
      :squad="squad"
      :busy="busy"
      :rename-squad="renameSquad"
      :invite-member="inviteMember"
      :kick-member="kickMember"
      :set-task="setTask"
    />
  </div>
  <div class="desk">
    <div
      class="desk-map"
      :class="{ 'is-drag': mapDrag }"
      @wheel.prevent="onMapWheel"
      @pointerdown="onMapDown">
      <svg v-if="view.cities.length" ref="mapSvg" :viewBox="mapFrame" role="img" aria-label="城市地图">
        <line v-for="e in edges" :key="e.id" :x1="e.x1" :y1="e.y1" :x2="e.x2" :y2="e.y2" />
        <g v-for="c in view.cities" :key="c.id">
          <circle
            :cx="c.x" :cy="c.y" :r="c.capital || c.id === 9101 ? 9 : 5.5"
            :fill="fill(c.owner)"
            :stroke="c.id === cityId ? '#1a1a1a' : (canSupply(c) ? '#2f6f4e' : '#fff')"
            :stroke-width="c.id === cityId ? 2.4 : 1"
            @click="pick(c)">
            <title>{{ c.name }} {{ c.id }}</title>
          </circle>
          <text :x="c.lx || c.x + 8" :y="c.ly || c.y + 3" text-anchor="middle">{{ c.name }}</text>
        </g>
        <g v-for="a in members" :key="'here' + a.id">
          <circle v-if="hereOf(a)" :cx="hereOf(a).x" :cy="hereOf(a).y - 9" r="3" fill="#fff" stroke="#1a1a1a" />
        </g>
      </svg>
      <p v-else class="muted">城市目录还没装上。</p>
    </div>
    <div class="desk-side">
      <div v-if="team" class="desk-tools" role="group" aria-label="操作">
        <button type="button" :class="{ ghost: tool !== 'move' }" @click="useTool('move')">移动</button>
        <button type="button" :class="{ ghost: tool !== 'attack' }" @click="useTool('attack')">攻打</button>
        <button type="button" :class="{ ghost: tool !== 'open' }" @click="useTool('open')">打开城市</button>
        <button type="button" class="ghost" :disabled="busy" @click="useTool('morale')">补士气</button>
      </div>
      <p v-if="city" class="muted">选中 {{ city.name }} {{ city.id }}<template v-if="canSupply(city)">，在这里可以用体力补士气</template></p>
      <ul v-if="lines.length" class="desk-lines">
        <li v-for="(line, i) in lines" :key="i">{{ line }}</li>
      </ul>
      <div v-if="city" class="desk-look">
        <h3>打开 {{ city.name }}</h3>
        <p v-if="opening" class="muted">{{ opening }}</p>
        <template v-else-if="shownLook">
          <p class="muted">城里共 {{ shownLook.look.count == null ? "人数未知" : shownLook.look.count + " 人" }}<template v-if="shownLook.look.owner_name"> · 归属 {{ shownLook.look.owner_name }}</template> · 敌方 {{ enemies.length }} 人</p>
          <p class="muted">{{ shownLook.look.note }}</p>
          <table v-if="enemies.length">
            <thead>
              <tr><th>名字</th><th>UID</th><th>等级</th><th>士气</th><th>战力</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="p in enemies" :key="p.uid">
                <td>{{ p.name || "—" }}</td>
                <td>{{ p.uid }}</td>
                <td>{{ p.lvl }}</td>
                <td>{{ p.morale }}</td>
                <td>{{ p.power }}</td>
                <td>{{ p.vip ? "VIP" : "" }}<template v-if="p.officer"> 官职{{ p.officer }}</template></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">前 5 页没有敌方。</p>
        </template>
        <p v-else-if="asked === city.id" class="muted">正在打开，敌方名单和总人数马上出现在这里。</p>
        <p v-else class="muted">点「打开城市」看这座城里的敌方，并写出总人数。只看前 5 页。</p>
      </div>
    </div>
  </div>
  <section v-if="team" class="desk-hits" aria-label="攻打状态">
    <div class="squad-head">
      <h3 class="squad-title">攻打状态</h3>
      <p class="muted squad-meta">最近 50 条，{{ hitTotal }} 条在当前筛选里</p>
    </div>
    <form class="lock-row" @submit.prevent="hitPage = 1; loadHits().catch((e) => (err = e.message))">
      <label>队员
        <input v-model="hitWho" placeholder="账号" />
      </label>
      <label>结果
        <select v-model="hitResult">
          <option value="">全部</option>
          <option value="win">打过</option>
          <option value="lose">打不过</option>
          <option value="repel">击退</option>
          <option value="down">被打</option>
        </select>
      </label>
      <label>关键字
        <input v-model="hitQ" placeholder="敌人、城市" />
      </label>
      <button type="submit" class="ghost">筛选</button>
    </form>
    <p v-if="!hits.length" class="muted">还没有攻打状态。编队打起来之后，新的战报会出现在这里。</p>
    <ul v-else class="hit-list">
      <li v-for="row in hits" :key="row.id" :class="'hit-' + row.result">
        <span class="hit-at">{{ row.at }}</span>
        <span class="who">{{ row.username }}</span>
        <span class="hit-result">{{ row.result_text }}</span>
        <span>{{ row.enemy_name || row.enemy_uid || "敌人" }}</span>
        <span v-if="row.city_name || row.city_id" class="muted">{{ row.city_name || row.city_id }}</span>
        <span v-if="row.pressing" class="hit-press">正在打队员</span>
        <span v-if="row.detail" class="muted hit-detail">{{ row.detail }}</span>
      </li>
    </ul>
    <p v-if="hitPages > 1" class="muted pager">
      <button type="button" class="ghost" :disabled="hitPage <= 1" @click="hitPage -= 1; loadHits().catch((e) => (err = e.message))">上一页</button>
      第 {{ hitPage }} / {{ hitPages }} 页
      <button type="button" class="ghost" :disabled="hitPage >= hitPages" @click="hitPage += 1; loadHits().catch((e) => (err = e.message))">下一页</button>
    </p>
  </section>
</template>
