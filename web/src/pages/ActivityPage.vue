<script setup>
import { computed, ref, watch } from "vue";
import { ElSwitch } from "element-plus";
import "element-plus/es/components/switch/style/css";
import itemRows from "../items.json";

const guildLevel = defineModel("guildLevel", { type: String, required: true });
const fundBuilding = defineModel("fundBuilding", { type: String, required: true });
const fundTimes = defineModel("fundTimes", { type: String, required: true });

const props = defineProps({
  dailyQq: { type: String, default: "" },
  tasks: { type: Array, required: true },
  jobs: { type: Array, default: () => [] },
  dailySwitching: { type: String, default: "" },
  note: { type: String, default: "" },
  thew: { default: null },
  score: { default: null },
  strikeLeft: { default: null },
  eggLeft: { default: null },
  fundBuildings: { type: Array, required: true },
  fundNote: { type: String, default: "" },
  eggNote: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  saveLevel: { type: Function, required: true },
  saveDailySwitch: { type: Function, required: true },
  runFund: { type: Function, required: true },
  runGuildCamp: { type: Function, required: true },
  runStrike: { type: Function, required: true },
  runEgg: { type: Function, required: true },
  wargame: { type: Object, default: () => ({}) },
  runWargame: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const itemQuery = ref("");
const itemHits = computed(() => {
  const q = itemQuery.value.trim().toLowerCase();
  if (!q) return [];
  const out = [];
  for (const [id, name, desc] of itemRows) {
    if (String(id).includes(q) || name.toLowerCase().includes(q) || desc.toLowerCase().includes(q)) {
      out.push({ id, name, desc });
      if (out.length >= 40) break;
    }
  }
  return out;
});

const guildTask = computed(() => props.tasks.find((it) => it.key === "公会战役") || null);
const campJob = computed(() => props.jobs.find((it) => it.kind === "guild_camp") || null);
const strikeJob = computed(() => props.jobs.find((it) => it.kind === "strike") || null);
const eggJob = computed(() => props.jobs.find((it) => it.kind === "egg") || null);
const wgJob = computed(() => props.jobs.find((it) => it.kind === "wargame") || null);
const wgQuery = ref("");
const wgLevel = ref("0");
const wgOrder = ref("asc");
const wgPage = ref(1);
const wgSize = 20;

const wgBoard = computed(() => props.wargame || {});
const wgSites = computed(() => (Array.isArray(wgBoard.value.sites) ? wgBoard.value.sites : []));
const wgLevels = computed(() => {
  const set = new Set();
  for (const row of wgSites.value) {
    if (row.lv) set.add(row.lv);
  }
  return [...set].sort((a, b) => a - b);
});
const wgShown = computed(() => {
  const q = wgQuery.value.trim().toLowerCase();
  const lv = Number(wgLevel.value) || 0;
  const rows = wgSites.value.filter((row) => {
    if (lv && Number(row.lv) !== lv) return false;
    if (!q) return true;
    return String(row.name || "").toLowerCase().includes(q)
      || String(row.country || "").toLowerCase().includes(q);
  });
  const sign = wgOrder.value === "desc" ? -1 : 1;
  return rows.slice().sort((a, b) => {
    const ap = Number(a.power) > 0 ? Number(a.power) : Number.MAX_SAFE_INTEGER;
    const bp = Number(b.power) > 0 ? Number(b.power) : Number.MAX_SAFE_INTEGER;
    return (ap - bp) * sign || (Number(a.lv) || 0) - (Number(b.lv) || 0) || (Number(a.id) || 0) - (Number(b.id) || 0);
  });
});
const wgPages = computed(() => Math.max(1, Math.ceil(wgShown.value.length / wgSize)));
const wgSlice = computed(() => {
  const page = Math.min(Math.max(1, wgPage.value), wgPages.value);
  const start = (page - 1) * wgSize;
  return wgShown.value.slice(start, start + wgSize);
});
const wgPageNo = computed(() => Math.min(Math.max(1, wgPage.value), wgPages.value));

watch([wgQuery, wgLevel, wgOrder], () => {
  wgPage.value = 1;
});

function liveText(n) {
  if (n === null || n === undefined || n === "") return "还没读到";
  const num = Number(n);
  if (!Number.isFinite(num)) return "还没读到";
  return Math.trunc(num).toLocaleString("zh-CN");
}

function powerText(n) {
  const num = Number(n);
  if (!Number.isFinite(num) || num <= 0) return "—";
  return Math.trunc(num).toLocaleString("zh-CN");
}

function wgState(row) {
  if (row.mine) return "自己";
  if (row.truce) return "停战";
  if (row.home) return "本国";
  return "可进攻";
}

function canHit(row) {
  return !!(row && row.uid && !row.mine && !row.truce);
}

function occupyText(occ) {
  if (!occ || !occ.id) return "";
  const lv = occ.lv ? `${occ.lv}级` : "";
  let text = `自己占着${lv}场地 ${occ.id}`;
  const end = Number(occ.end);
  if (Number.isFinite(end) && end > 0) {
    const mins = Math.max(0, Math.round((end * 1000 - Date.now()) / 60000));
    text += `，大约还有 ${mins} 分钟`;
  }
  return text;
}
</script>

<template>
  <section>
    <h2>活动助手</h2>
    <p v-if="dailyQq">攻打 QQ {{ dailyQq }}</p>
    <p v-else class="muted">还没绑定攻打 QQ。按页面上方的步骤点登录并扫码。一个登录账号只绑一个攻打号，这里的活动用那个号做。</p>
    <p class="muted">由这个账号的攻打线程执行，不另开连接。开关和日常任务是同一个。</p>
    <details class="campaign-box">
      <summary>公会战役</summary>
      <div v-if="guildTask" class="campaign-line">
        <span class="switch">{{ guildTask.name }}</span>
        <span class="campaign-ops">
          <el-switch
            class="daily-switch"
            size="large"
            inline-prompt
            active-text="开"
            inactive-text="关"
            :model-value="guildTask.on"
            :loading="dailySwitching === guildTask.key"
            :disabled="guildTask.locked"
            @change="saveDailySwitch(guildTask, $event)"
          />
          <span v-if="guildTask.locked" class="muted">功能尚在恢复中...</span>
        </span>
      </div>
      <form class="lock-row" @submit.prevent="saveLevel().catch((e) => showErr(e.message))">
        <span class="switch">攻打等级</span>
        <label>等级<input v-model="guildLevel" class="mins" inputmode="numeric" required /></label>
        <button type="submit">保存</button>
        <button type="button" @click="runGuildCamp().catch((e) => showErr(e.message))">进攻</button>
      </form>
      <p v-if="note" class="muted">{{ note }}</p>
      <p v-if="campJob">{{ campJob.status }} {{ campJob.detail }}</p>
      <p>体力 {{ liveText(thew) }} · 当前积分 {{ liveText(score) }}</p>
      <p class="muted">进入这个页面会读一次体力和当前积分。只在周五 10:00 到周六 22:00（本机时间）进攻。点进攻就按体力打这一轮，不用等整轮日常。同一等级一直打，直到体力不够、体力不再下降或被拒绝。不领奖。等级记在这个登录账号上，没改过就用配置里的等级。</p>
    </details>
    <details class="campaign-box" open>
      <summary>军事演习</summary>
      <form class="lock-row" @submit.prevent="runWargame().catch((e) => showErr(e.message))">
        <span class="switch">军事演习</span>
        <button type="submit">刷新场地</button>
        <label>等级
          <select v-model="wgLevel">
            <option value="0">全部</option>
            <option v-for="lv in wgLevels" :key="lv" :value="String(lv)">{{ lv }}级</option>
          </select>
        </label>
        <label>排序
          <select v-model="wgOrder">
            <option value="asc">战力从低到高</option>
            <option value="desc">战力从高到低</option>
          </select>
        </label>
        <label>查找<input v-model="wgQuery" placeholder="名字或国家" /></label>
      </form>
      <p v-if="wgJob">{{ wgJob.status }} {{ wgJob.detail }}</p>
      <p v-if="wgBoard.result">{{ wgBoard.result }}</p>
      <p>剩余次数 {{ liveText(wgBoard.token) }}<span v-if="occupyText(wgBoard.occupy)"> · {{ occupyText(wgBoard.occupy) }}</span></p>
      <p class="muted">点刷新拉战争学院的演习场，只列有人占的，写出名字和战力。选一个人进攻，打完把结果写在上面。战报里能分出双方血量时，写出我方击伤对方、对方击伤我方的百分比。用的还是军事演习原来的查询和进攻。次数用完不发，停战和自己占的场不能点。本国的场可以点，服务器不让打会把原因返回来。</p>
      <p v-if="!wgSites.length" class="muted">还没有场地。点刷新场地。</p>
      <p v-else-if="!wgShown.length" class="muted">这个筛选下没有人</p>
      <div v-else class="wide">
        <table class="wg">
          <thead>
            <tr><th>等级</th><th>玩家</th><th>战力</th><th>国家</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="row in wgSlice" :key="row.id + ':' + row.uid">
              <td>{{ row.lv ? row.lv + "级" : "—" }}</td>
              <td>{{ row.name }}</td>
              <td>{{ powerText(row.power) }}</td>
              <td>{{ row.country || "—" }}</td>
              <td>{{ wgState(row) }}</td>
              <td>
                <button type="button" :disabled="!canHit(row)" @click="runWargame(row).catch((e) => showErr(e.message))">进攻</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="wgPages > 1" class="pager">
        <button type="button" class="ghost" :disabled="wgPageNo <= 1" @click="wgPage = wgPageNo - 1">上一页</button>
        {{ wgPageNo }} / {{ wgPages }}，共 {{ wgShown.length }} 人
        <button type="button" class="ghost" :disabled="wgPageNo >= wgPages" @click="wgPage = wgPageNo + 1">下一页</button>
      </p>
    </details>
    <details class="campaign-box">
      <summary>精准打击</summary>
      <form class="lock-row" @submit.prevent="runStrike().catch((e) => showErr(e.message))">
        <span class="switch">精准打击</span>
        <button type="submit">打击</button>
      </form>
      <p v-if="strikeJob">{{ strikeJob.status }} {{ strikeJob.detail }}</p>
      <p>剩余次数 {{ liveText(strikeLeft) }}</p>
      <p class="muted">进入这个页面会读一次剩余次数。点下去会一直打，直到服务器上的剩余次数变成 0。次数没有下降不算打中。这次打开回包里没有活动起止时间。不领圈数奖励。</p>
    </details>
    <details class="campaign-box">
      <summary>成就拨款</summary>
      <form class="lock-row" @submit.prevent="runFund(fundBuilding, fundTimes).catch((e) => showErr(e.message))">
        <span class="switch">成就拨款</span>
        <label>建筑
          <select v-model="fundBuilding" class="fund-building" required>
            <option value="" disabled>选择建筑</option>
            <option v-for="b in fundBuildings" :key="b.id" :value="b.id">{{ b.name }}</option>
          </select>
        </label>
        <label>次数<input v-model="fundTimes" class="mins" inputmode="numeric" required /></label>
        <button type="submit">拨款</button>
      </form>
      <p v-if="fundNote" class="muted">{{ fundNote }}</p>
      <p class="muted">每次拨款前各开 4 张 1000 万金属卡和石油卡。次数 1 到 999。</p>
    </details>
    <details class="campaign-box">
      <summary>欢乐砸彩蛋</summary>
      <form class="lock-row" @submit.prevent="runEgg().catch((e) => showErr(e.message))">
        <span class="switch">欢乐砸彩蛋</span>
        <button type="submit">全部砸开</button>
      </form>
      <p v-if="eggJob">{{ eggJob.status }} {{ eggJob.detail }}</p>
      <p v-if="eggNote" class="muted">{{ eggNote }}</p>
      <p>还可以开 {{ liveText(eggLeft) }} 个</p>
      <p class="muted">进入这个页面会读一次剩余数量。点下去会一直砸，直到剩余变成 0。每砸一个消耗 1 个物品 10015，得到 1 张 200 万石油卡。</p>
    </details>
    <details class="campaign-box">
      <summary>道具表</summary>
      <form class="lock-row" @submit.prevent>
        <label>查找<input v-model="itemQuery" placeholder="编号或名称" /></label>
      </form>
      <p class="muted">共 {{ itemRows.length }} 条。输入编号或名称后列出，一次最多 40 条。自选包后面写着可以开出的道具。</p>
      <p v-if="itemQuery.trim() && !itemHits.length" class="muted">没有这个道具</p>
      <p v-for="it in itemHits" :key="it.id"><b>{{ it.id }}</b> {{ it.name }}<span class="muted"> {{ it.desc }}</span></p>
    </details>
    <p v-if="err && errAt === 'activity'" class="err">{{ err }}</p>
  </section>
</template>
