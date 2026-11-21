<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { ElSwitch } from "element-plus";
import "element-plus/es/components/switch/style/css";
import bundledItems from "../items.json";
import bundledNew from "../items-new.json";

const guildLevel = defineModel("guildLevel", { type: String, required: true });
const fundBuilding = defineModel("fundBuilding", { type: String, required: true });
const fundTimes = defineModel("fundTimes", { type: String, required: true });
const wishTimes = defineModel("wishTimes", { type: String, required: true });
const soulTimes = defineModel("soulTimes", { type: String, required: true });
const trainCount = defineModel("trainCount", { type: String, required: true });
const skillLevel = defineModel("skillLevel", { type: String, required: true });
const stoneTimes = defineModel("stoneTimes", { type: String, required: true });

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
  runFundCycle: { type: Function, required: true },
  runGuildCamp: { type: Function, required: true },
  runStrike: { type: Function, required: true },
  runEgg: { type: Function, required: true },
  runWish: { type: Function, required: true },
  runGift: { type: Function, required: true },
  runDrop: { type: Function, required: true },
  runSoul: { type: Function, required: true },
  runBadge: { type: Function, required: true },
  runDonate: { type: Function, required: true },
  runTribute: { type: Function, required: true },
  runTrain: { type: Function, required: true },
  runTrainAll: { type: Function, required: true },
  runSkill: { type: Function, required: true },
  runStone: { type: Function, required: true },
  wargame: { type: Object, default: () => ({}) },
  runWargame: { type: Function, required: true },
  runWargameRetreat: { type: Function, required: true },
  cancelJob: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const itemRows = ref(bundledItems);
const newItems = ref(bundledNew.map(([id, name, desc]) => ({ id, name, desc })));
const itemQuery = ref("");
let itemLoad = 0;

const itemHits = computed(() => {
  const q = itemQuery.value.trim().toLowerCase();
  if (!q) return [];
  const out = [];
  for (const [id, name, desc] of itemRows.value) {
    if (String(id).includes(q) || name.toLowerCase().includes(q) || desc.toLowerCase().includes(q)) {
      out.push({ id, name, desc });
      if (out.length >= 40) break;
    }
  }
  return out;
});

async function loadLiveItems() {
  const serial = ++itemLoad;
  try {
    const resp = await fetch("/api/items", { credentials: "same-origin" });
    if (!resp.ok || serial !== itemLoad) return;
    const data = await resp.json();
    if (serial !== itemLoad) return;
    if (!data || !Array.isArray(data.items) || !data.items.length) return;
    itemRows.value = data.items;
    if (Array.isArray(data.new)) {
      newItems.value = data.new.map(([id, name, desc]) => ({ id, name, desc }));
    }
  } catch {
    // 接口没有数据时继续用打包进来的表。
  }
}

function onItemToggle(event) {
  const box = event.currentTarget;
  if (box && box.open) loadLiveItems();
}

onMounted(() => {
  loadLiveItems();
});

const guildTask = computed(() => props.tasks.find((it) => it.key === "公会战役") || null);
const campJob = computed(() => props.jobs.find((it) => it.kind === "guild_camp") || null);
const strikeJob = computed(() => props.jobs.find((it) => it.kind === "strike") || null);
const eggJob = computed(() => props.jobs.find((it) => it.kind === "egg") || null);
const wishJob = computed(() => props.jobs.find((it) => it.kind === "wish") || null);
const giftJob = computed(() => props.jobs.find((it) => it.kind === "gift") || null);
const dropJob = computed(() => props.jobs.find((it) => it.kind === "repair_drop") || null);
const soulJob = computed(() => props.jobs.find((it) => it.kind === "soul") || null);
const badgeJob = computed(() => props.jobs.find((it) => it.kind === "badge") || null);
const donateJob = computed(() => props.jobs.find((it) => it.kind === "donate") || null);
const tributeJob = computed(() => props.jobs.find((it) => it.kind === "tribute") || null);
const trainJob = computed(() => props.jobs.find((it) => it.kind === "train_book") || null);
const skillJob = computed(() => props.jobs.find((it) => it.kind === "skill_book") || null);
const stoneJob = computed(() => props.jobs.find((it) => it.kind === "stone") || null);
const fundJob = computed(() => props.jobs.find((it) => it.kind === "fund") || null);
const wgJob = computed(() => props.jobs.find((it) => it.kind === "wargame") || null);
const wgQuery = ref("");
const wgLevel = ref("0");
const wgPage = ref(1);
const wgPageText = ref("1");
const wgSize = 10;

const wgBoard = computed(() => props.wargame || {});
const wgBattle = computed(() => wgBoard.value.battle || null);
const wgLanes = computed(() => {
  const rows = wgBattle.value && wgBattle.value.lanes;
  return Array.isArray(rows) ? rows : [];
});

function wgGap(n) {
  const value = Number(n);
  if (!Number.isFinite(value) || value < 4) return "";
  return String(Math.trunc(value));
}

function wgHasSec(lane) {
  const value = Number(lane && lane.seconds);
  return Number.isFinite(value) && value >= 0;
}

function wgLv(n) {
  const value = Number(n);
  if (!Number.isFinite(value) || value < 1) return "";
  return `${Math.trunc(value)}级`;
}

function wgLaneOn(lane) {
  return wgHasSec(lane) || !!wgLv(lane && lane.selfLv) || !!wgLv(lane && lane.enemyLv)
    || !!wgGap(lane && lane.self) || !!wgGap(lane && lane.enemy);
}

function wgSec(seconds) {
  const n = Number(seconds) || 0;
  if (n < 60) return `${n}秒`;
  const minute = Math.floor(n / 60);
  const second = n % 60;
  return second ? `${minute}分${second}秒` : `${minute}分`;
}

function wgWan(n) {
  const value = Number(n) || 0;
  if (value < 10000) return String(value);
  const text = (value / 10000).toFixed(1).replace(/\.0$/, "");
  return `${text}万`;
}

function wgTanks(side) {
  if (!side) return "—";
  return `${side.tanks}/${side.total}`;
}

function wgHp(side) {
  if (!side) return "—";
  const max = Number(side.maxHp) || 0;
  const pct = max > 0 ? Math.round((Number(side.hp) || 0) * 100 / max) : 0;
  return `${wgWan(side.hp)}/${wgWan(side.maxHp)}（${pct}%）`;
}
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
  return wgSites.value.filter((row) => {
    if (lv && Number(row.lv) !== lv) return false;
    if (!q) return true;
    return String(row.name || "").toLowerCase().includes(q)
      || String(row.country || "").toLowerCase().includes(q);
  });
});
const wgPages = computed(() => Math.max(1, Math.ceil(wgShown.value.length / wgSize)));
const wgSlice = computed(() => {
  const page = Math.min(Math.max(1, wgPage.value), wgPages.value);
  const start = (page - 1) * wgSize;
  return wgShown.value.slice(start, start + wgSize);
});
const wgPageNo = computed(() => Math.min(Math.max(1, wgPage.value), wgPages.value));

watch([wgQuery, wgLevel], () => {
  wgPage.value = 1;
});

watch(wgPageNo, (n) => {
  wgPageText.value = String(n);
});

function jumpWgPage() {
  const n = Number(String(wgPageText.value).trim());
  if (!Number.isFinite(n)) {
    wgPageText.value = String(wgPageNo.value);
    return;
  }
  wgPage.value = Math.trunc(n);
  wgPageText.value = String(wgPageNo.value);
}

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
  if (!row || row.empty || !row.uid) return "空场";
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
    <details class="campaign-box" open>
      <summary>一键献礼</summary>
      <form class="lock-row" @submit.prevent="runTribute().catch((e) => showErr(e.message))">
        <span class="switch">一键献礼</span>
        <button type="submit">开始</button>
      </form>
      <p v-if="tributeJob">{{ tributeJob.status }} {{ tributeJob.detail }}<button v-if="tributeJob.status === '排队' || tributeJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(tributeJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">先打开献礼宝箱，再摧毁建筑修复卡，然后打开献礼徽章宝箱，捐献献礼徽章，最后按已保存的清理背包名单再清一次。没保存过就用默认名单。</p>
    </details>
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
          <span class="muted">今日 {{ guildTask.done }}/{{ guildTask.max }}</span>
        </span>
      </div>
      <form class="lock-row" @submit.prevent="saveLevel().catch((e) => showErr(e.message))">
        <span class="switch">攻打等级</span>
        <label>等级<input v-model="guildLevel" class="mins" inputmode="numeric" required /></label>
        <button type="submit">保存</button>
        <button type="button" @click="runGuildCamp().catch((e) => showErr(e.message))">进攻</button>
      </form>
      <p v-if="note" class="muted">{{ note }}</p>
      <p v-if="campJob">{{ campJob.status }} {{ campJob.detail }}<button v-if="campJob.status === '排队' || campJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(campJob).catch((e) => showErr(e.message))">关停</button></p>
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
        <label>查找<input v-model="wgQuery" placeholder="名字或国家" /></label>
      </form>
      <p v-if="wgJob && (wgJob.status === '排队' || wgJob.status === '正在做')">{{ wgJob.status }} {{ wgJob.detail }}<button type="button" class="ghost" @click="cancelJob(wgJob).catch((e) => showErr(e.message))">关停</button></p>
      <p v-if="wgBoard.result">{{ wgBoard.result }}</p>
      <div v-if="wgBattle" class="wg-result">
        <p>第一阶段 {{ wgSec(wgBattle.phase1.seconds) }}<span v-if="!wgBattle.climax"> · 未进入决战</span></p>
        <table>
          <thead>
            <tr><th></th><th>剩余坦克</th><th>整体血量</th></tr>
          </thead>
          <tbody>
            <tr><td>我方</td><td>{{ wgTanks(wgBattle.phase1.self) }}</td><td>{{ wgHp(wgBattle.phase1.self) }}</td></tr>
            <tr><td>对方</td><td>{{ wgTanks(wgBattle.phase1.enemy) }}</td><td>{{ wgHp(wgBattle.phase1.enemy) }}</td></tr>
          </tbody>
        </table>
        <template v-if="wgBattle.climax">
          <p>决战 {{ wgSec(wgBattle.climax.seconds) }}</p>
          <table>
            <thead>
              <tr><th></th><th>剩余坦克</th><th>最终血量</th></tr>
            </thead>
            <tbody>
              <tr><td>我方</td><td>{{ wgTanks(wgBattle.climax.self) }}</td><td>{{ wgHp(wgBattle.climax.self) }}</td></tr>
              <tr><td>对方</td><td>{{ wgTanks(wgBattle.climax.enemy) }}</td><td>{{ wgHp(wgBattle.climax.enemy) }}</td></tr>
            </tbody>
          </table>
        </template>
        <table v-if="wgLanes.some((lane) => wgLaneOn(lane))">
          <thead>
            <tr><th>路</th><th>战斗时间</th><th>我方火炮</th><th>敌方火炮</th><th>我方火炮发射间隔</th><th>敌方火炮发射间隔</th></tr>
          </thead>
          <tbody>
            <template v-for="(lane, i) in wgLanes" :key="i">
              <tr v-if="wgLaneOn(lane)">
                <td>第 {{ i + 1 }} 路</td>
                <td>{{ wgHasSec(lane) ? wgSec(lane.seconds) : "" }}</td>
                <td>{{ wgLv(lane && lane.selfLv) }}</td>
                <td>{{ wgLv(lane && lane.enemyLv) }}</td>
                <td>{{ wgGap(lane && lane.self) }}</td>
                <td>{{ wgGap(lane && lane.enemy) }}</td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <p>剩余次数 {{ liveText(wgBoard.token) }}<span v-if="occupyText(wgBoard.occupy)"> · {{ occupyText(wgBoard.occupy) }}</span>
        <button v-if="wgBoard.occupy && wgBoard.occupy.id" type="button" class="ghost" @click="runWargameRetreat(wgBoard.occupy.id).catch((e) => showErr(e.message))">撤退</button>
      </p>
      <p class="muted">点刷新拉战争学院的演习场，顺序和游戏里一样，一页 10 个。等级选全部、查找留空时，页码和战争学院对得上。空场留在原来的位置。写出占领者的名字和战力。选一个人进攻，打完把结果写在上面。战报按双方写出第一阶段的剩余坦克、整体血量和耗时，并按路写出第一阶段各自的战斗时间；时间轴中间断开且当时两边都还有坦克，就再写出决战耗时和最终血量。火炮等级和发射间隔按路写在下面，没读到的一方留空。用的还是军事演习原来的查询和进攻。次数用完不发，停战和自己占的场不能点。本国的场可以点，服务器不让打会把原因返回来。</p>
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
              <td>{{ row.empty || !row.uid ? "空场" : row.name }}</td>
              <td>{{ row.empty || !row.uid ? "—" : powerText(row.power) }}</td>
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
        <label>页码<input v-model="wgPageText" class="mins" inputmode="numeric" @change="jumpWgPage" /></label>
        / {{ wgPages }}
        <button type="button" class="ghost" :disabled="wgPageNo >= wgPages" @click="wgPage = wgPageNo + 1">下一页</button>
      </p>
    </details>
    <details class="campaign-box">
      <summary>精准打击</summary>
      <form class="lock-row" @submit.prevent="runStrike().catch((e) => showErr(e.message))">
        <span class="switch">精准打击</span>
        <button type="submit">打击</button>
      </form>
      <p v-if="strikeJob">{{ strikeJob.status }} {{ strikeJob.detail }}<button v-if="strikeJob.status === '排队' || strikeJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(strikeJob).catch((e) => showErr(e.message))">关停</button></p>
      <p>剩余次数 {{ liveText(strikeLeft) }}</p>
      <p class="muted">进入这个页面会读一次剩余次数。点下去会一直打，直到服务器上的剩余次数变成 0。没出奖励也继续打。次数没有下降不算打中。这次打开回包里没有活动起止时间。不领圈数奖励。</p>
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
        <button type="button" @click="runFundCycle().catch((e) => showErr(e.message))">一键循环拨款</button>
      </form>
      <p v-if="fundJob">{{ fundJob.status }} {{ fundJob.detail }}<button v-if="fundJob.status === '排队' || fundJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(fundJob).catch((e) => showErr(e.message))">关停</button></p>
      <p v-if="fundNote" class="muted">{{ fundNote }}</p>
      <p class="muted">每次拨款前先打开背包里的资源包，包括 1000 万资源包。1000 万资源包打开后是 1000 万金属卡和 1000 万石油卡各 1 张，再和其他面额的资源卡一起，从小到大按仓库一次能吃下的张数批量用掉。某种没有，或这一批没开成，就减少张数再试，减到 1 张还不行，这种就停，换下一种。次数 1 到 999。一键循环按上面的顺序，一栋一直拨到被拒，再拨下一栋，不回头。</p>
    </details>
    <details class="campaign-box">
      <summary>欢乐砸彩蛋</summary>
      <form class="lock-row" @submit.prevent="runEgg().catch((e) => showErr(e.message))">
        <span class="switch">欢乐砸彩蛋</span>
        <button type="submit">全部砸开</button>
      </form>
      <p v-if="eggJob">{{ eggJob.status }} {{ eggJob.detail }}<button v-if="eggJob.status === '排队' || eggJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(eggJob).catch((e) => showErr(e.message))">关停</button></p>
      <p v-if="eggNote" class="muted">{{ eggNote }}</p>
      <p>还可以开 {{ liveText(eggLeft) }} 个</p>
      <p class="muted">进入这个页面会读一次剩余数量。点下去会一直砸，直到剩余变成 0。每砸一个消耗 1 个物品 10015，得到 1 张 200 万石油卡。</p>
    </details>
    <details class="campaign-box">
      <summary>许愿树</summary>
      <form class="lock-row" @submit.prevent="runWish(wishTimes).catch((e) => showErr(e.message))">
        <span class="switch">许愿树抽奖</span>
        <label>次数<input v-model="wishTimes" class="mins" inputmode="numeric" required /></label>
        <button type="submit">抽奖</button>
      </form>
      <p v-if="wishJob">{{ wishJob.status }} {{ wishJob.detail }}<button v-if="wishJob.status === '排队' || wishJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(wishJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">先开一次面板，再按填写的次数抽。次数 1 到 999。</p>
    </details>
    <details class="campaign-box">
      <summary>献礼宝箱</summary>
      <form class="lock-row" @submit.prevent="runGift().catch((e) => showErr(e.message))">
        <span class="switch">开启献礼宝箱</span>
        <button type="submit">全部打开</button>
      </form>
      <p v-if="giftJob">{{ giftJob.status }} {{ giftJob.detail }}<button v-if="giftJob.status === '排队' || giftJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(giftJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">把背包里的 30156 献礼宝箱打开。物品槽不写进任务，执行时按清理背包从当前背包读取。一次最多 999 个，还有就接着开。</p>
    </details>
    <details class="campaign-box">
      <summary>建筑修复卡</summary>
      <form class="lock-row" @submit.prevent="runDrop().catch((e) => showErr(e.message))">
        <span class="switch">摧毁建筑修复卡</span>
        <button type="submit">全部摧毁</button>
      </form>
      <p v-if="dropJob">{{ dropJob.status }} {{ dropJob.detail }}<button v-if="dropJob.status === '排队' || dropJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(dropJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">把背包里的 10127 建筑修复卡摧毁，不修复建筑。物品槽不写进任务，执行时按清理背包从当前背包读取。一次 99 张，直到没有。</p>
    </details>
    <details class="campaign-box">
      <summary>将魂兑换</summary>
      <form class="lock-row" @submit.prevent="runSoul(soulTimes).catch((e) => showErr(e.message))">
        <span class="switch">将魂兑换</span>
        <label>次数<input v-model="soulTimes" class="mins" inputmode="numeric" required /></label>
        <button type="submit">兑换</button>
      </form>
      <p v-if="soulJob">{{ soulJob.status }} {{ soulJob.detail }}<button v-if="soulJob.status === '排队' || soulJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(soulJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">每次发 type=-1、free=1、credit=900。按填写的次数兑，没有回包或被拒就当已经兑完。次数 1 到 999。</p>
    </details>
    <details class="campaign-box">
      <summary>献礼徽章宝箱</summary>
      <form class="lock-row" @submit.prevent="runBadge().catch((e) => showErr(e.message))">
        <span class="switch">开启献礼徽章宝箱</span>
        <button type="submit">全部打开</button>
      </form>
      <p v-if="badgeJob">{{ badgeJob.status }} {{ badgeJob.detail }}<button v-if="badgeJob.status === '排队' || badgeJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(badgeJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">把背包里的 30157 献礼徽章宝箱打开。物品槽不写进任务，执行时按清理背包从当前背包读取。一次最多 999 个。</p>
    </details>
    <details class="campaign-box">
      <summary>捐献献礼徽章</summary>
      <form class="lock-row" @submit.prevent="runDonate().catch((e) => showErr(e.message))">
        <span class="switch">捐献献礼徽章</span>
        <button type="submit">捐献</button>
      </form>
      <p v-if="donateJob">{{ donateJob.status }} {{ donateJob.detail }}<button v-if="donateJob.status === '排队' || donateJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(donateJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">直接发 type=2、activeType=1，不先开面板。</p>
    </details>
    <details class="campaign-box">
      <summary>英雄训练册</summary>
      <form class="lock-row" @submit.prevent="runTrain(trainCount).catch((e) => showErr(e.message))">
        <span class="switch">英雄训练册</span>
        <label>本数<input v-model="trainCount" class="mins" inputmode="numeric" required /></label>
        <button type="submit">使用</button>
        <button type="button" @click="runTrainAll().catch((e) => showErr(e.message))">抽光</button>
      </form>
      <p v-if="trainJob">{{ trainJob.status }} {{ trainJob.detail }}<button v-if="trainJob.status === '排队' || trainJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(trainJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">给一个正在训练的英雄用填写的本数，1 到 999。抽光会把背包里的训练册用完，一次最多 999 本。没有正在训练的英雄不发。</p>
    </details>
    <details class="campaign-box">
      <summary>参谋技能书</summary>
      <form class="lock-row" @submit.prevent="runSkill(skillLevel).catch((e) => showErr(e.message))">
        <span class="switch">合成参谋技能书</span>
        <label>等级<input v-model="skillLevel" class="mins" inputmode="numeric" required /></label>
        <button type="submit">合成</button>
      </form>
      <p v-if="skillJob">{{ skillJob.status }} {{ skillJob.detail }}<button v-if="skillJob.status === '排队' || skillJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(skillJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">按背包里这一级的一半合成。等级 1 到 10，1 到 7 级是 10143 起，8 到 10 级是 10245 起。不足两本不发。</p>
    </details>
    <details class="campaign-box">
      <summary>购买保护石</summary>
      <form class="lock-row" @submit.prevent="runStone(stoneTimes).catch((e) => showErr(e.message))">
        <span class="switch">购买保护石</span>
        <label>次数<input v-model="stoneTimes" class="mins" inputmode="numeric" required /></label>
        <button type="submit">购买</button>
      </form>
      <p v-if="stoneJob">{{ stoneJob.status }} {{ stoneJob.detail }}<button v-if="stoneJob.status === '排队' || stoneJob.status === '正在做'" type="button" class="ghost" @click="cancelJob(stoneJob).catch((e) => showErr(e.message))">关停</button></p>
      <p class="muted">按填写的次数一次买完。商店 1112，每个花费 2，得到物品 50009。次数 1 到 999。这次抓包里勋章和功勋没有变化。</p>
    </details>
    <details class="campaign-box" @toggle="onItemToggle">
      <summary>道具表</summary>
      <form class="lock-row" @submit.prevent>
        <label>查找<input v-model="itemQuery" placeholder="编号或名称" /></label>
      </form>
      <p class="muted">共 {{ itemRows.length }} 条。输入编号或名称后列出，一次最多 40 条。自选包后面写着可以开出的道具。</p>
      <p v-if="itemQuery.trim() && !itemHits.length" class="muted">没有这个道具</p>
      <p v-for="it in itemHits" :key="it.id"><b>{{ it.id }}</b> {{ it.name }}<span class="muted"> {{ it.desc }}</span></p>
      <div v-if="newItems.length" class="item-part">
        <h3>新出的道具</h3>
        <p class="muted">新出 {{ newItems.length }} 个。</p>
        <p v-for="it in newItems" :key="'new-' + it.id"><b>{{ it.id }}</b> {{ it.name }}<span class="muted"> {{ it.desc }}</span></p>
      </div>
    </details>
    <p v-if="err && errAt === 'activity'" class="err">{{ err }}</p>
  </section>
</template>
