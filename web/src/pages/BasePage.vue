<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import FoldPart from "../components/FoldPart.vue";

const count = defineModel("count", { type: String, required: true });
const army = defineModel("army", { type: String, required: true });
const factoryCount = defineModel("factoryCount", { type: String, required: true });
const aaCount = defineModel("aaCount", { type: String, required: true });
const drinkCount = defineModel("drinkCount", { type: String, required: true });
const admiralCount = defineModel("admiralCount", { type: String, required: true });
const admiral10Count = defineModel("admiral10Count", { type: String, required: true });
const agentCount = defineModel("agentCount", { type: String, required: true });
const boxMidCount = defineModel("boxMidCount", { type: String, required: true });
const boxHighCount = defineModel("boxHighCount", { type: String, required: true });
const generalCount = defineModel("generalCount", { type: String, required: true });
const staffCount = defineModel("staffCount", { type: String, required: true });
const labPickCount = defineModel("labPickCount", { type: String, required: true });
const autoCount = defineModel("autoCount", { type: String, required: true });
const lab50Count = defineModel("lab50Count", { type: String, required: true });
const auto50Count = defineModel("auto50Count", { type: String, required: true });

const props = defineProps({
  dailyQq: { type: String, default: "" },
  jobs: { type: Array, required: true },
  blast: { default: null },
  metal: { default: null },
  oil: { default: null },
  credit: { default: null },
  pop: { default: null },
  cards: { type: Object, default: () => ({}) },
  resourceCatalog: { type: Array, default: () => [] },
  resourceCards: { default: null },
  cleanCatalog: { type: Array, default: () => [] },
  cleanItems: { default: null },
  note: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  runBlast: { type: Function, required: true },
  runFactory: { type: Function, required: true },
  runCards: { type: Function, required: true },
  saveResourceCards: { type: Function, required: true },
  runClean: { type: Function, required: true },
  saveClean: { type: Function, required: true },
  runDraw: { type: Function, required: true },
  runBox: { type: Function, required: true },
  runPick: { type: Function, required: true },
  runLab50: { type: Function, required: true },
  runAuto50: { type: Function, required: true },
  cancelJob: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const picked = ref([]);
const cardNote = ref("");
const catalog = computed(() => props.resourceCatalog || []);
const cleanPicked = ref([]);
const cleanNote = ref("");
const cleanList = computed(() => props.cleanCatalog || []);
let appliedCards = null;
let appliedClean = null;

watch(
  [() => (props.resourceCards == null ? null : props.resourceCards.map((id) => Number(id)).join(",")),
    () => catalog.value.map((card) => card.id).join(",")],
  ([sig]) => {
    if (sig == null) {
      if (!picked.value.length) picked.value = catalog.value.map((card) => card.id);
      return;
    }
    if (sig === appliedCards) return;
    appliedCards = sig;
    picked.value = sig ? sig.split(",").map((id) => Number(id)) : [];
  },
  { immediate: true },
);

watch(
  [() => (props.cleanItems == null ? null : props.cleanItems.map((id) => Number(id)).join(",")),
    () => cleanList.value.map((item) => item.id).join(",")],
  ([sig]) => {
    if (sig == null) {
      if (!cleanPicked.value.length) cleanPicked.value = cleanList.value.map((item) => item.id);
      return;
    }
    if (sig === appliedClean) return;
    appliedClean = sig;
    cleanPicked.value = sig ? sig.split(",").map((id) => Number(id)) : [];
  },
  { immediate: true },
);

function cardOn(id) {
  return picked.value.includes(id);
}

function cleanOn(id) {
  return cleanPicked.value.includes(id);
}

function toggleClean(id, on) {
  cleanNote.value = "";
  const set = new Set(cleanPicked.value);
  if (on) set.add(id);
  else set.delete(id);
  cleanPicked.value = cleanList.value.filter((item) => set.has(item.id)).map((item) => item.id);
}

async function saveCleanItems() {
  cleanNote.value = "";
  if (!cleanPicked.value.length) throw new Error("至少选一种物品");
  const saved = await props.saveClean(cleanPicked.value.slice());
  if (Array.isArray(saved)) cleanPicked.value = saved.map((id) => Number(id));
  cleanNote.value = "已保存。清理时按这个名单使用。";
}

function toggleCard(id, on) {
  cardNote.value = "";
  const set = new Set(picked.value);
  if (on) set.add(id);
  else set.delete(id);
  picked.value = catalog.value.filter((card) => set.has(card.id)).map((card) => card.id);
}

async function saveCards() {
  cardNote.value = "";
  if (!picked.value.length) throw new Error("至少选一种资源卡");
  const saved = await props.saveResourceCards(picked.value.slice());
  if (Array.isArray(saved)) picked.value = saved.map((id) => Number(id));
  cardNote.value = "已保存。开卡按面额从小到大。";
}

const page = ref(null);
const recentScroller = ref(null);
const recentOpen = ref(true);
let recentObserver;
let recentSeen;

function fitRecent() {
  const root = page.value;
  if (!root) return;
  const dock = root.querySelector(".recent-dock");
  const scroller = recentScroller.value;
  if (scroller) scroller.style.maxHeight = "";
  const mobile = window.matchMedia("(max-width: 720px)").matches && getComputedStyle(root).display !== "none";
  if (!mobile || !dock) {
    root.style.paddingBottom = "";
    return;
  }
  const pad = `${dock.offsetHeight}px`;
  if (root.style.paddingBottom !== pad) root.style.paddingBottom = pad;
}

function bindRecent() {
  recentObserver?.disconnect();
  const dock = page.value?.querySelector(".recent-dock");
  if (dock && recentObserver) recentObserver.observe(dock);
  fitRecent();
}

onMounted(() => {
  recentObserver = new ResizeObserver(() => fitRecent());
  recentSeen = new IntersectionObserver(() => fitRecent());
  if (page.value) recentSeen.observe(page.value);
  bindRecent();
  window.addEventListener("resize", fitRecent);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", fitRecent);
  recentObserver?.disconnect();
  recentSeen?.disconnect();
});

watch(recentScroller, () => nextTick(bindRecent));
watch(() => props.jobs, () => nextTick(fitRecent));
watch(recentOpen, () => nextTick(fitRecent));

function resText(n) {
  if (n === null || n === undefined || n === "") return "还没读到";
  const num = Number(n);
  if (!Number.isFinite(num)) return "还没读到";
  return Math.trunc(num).toLocaleString("zh-CN");
}
</script>

<template>
  <section ref="page" class="base-page">
    <h2>基地战</h2>
    <p v-if="dailyQq">攻打 QQ {{ dailyQq }}</p>
    <p v-else class="muted">还没绑定攻打 QQ。按页面上方的步骤点登录并扫码。一个登录账号只绑一个攻打号，这里的制造用那个号做。</p>
    <p class="muted">由这个账号的攻打线程执行，不另开连接。兵营和战车工厂按剩余时间排下一次快速制造，免费线默认 600 秒。开资源卡和清理背包也在这条线程上做。</p>
    <h2>基地内资源</h2>
    <p>金属 {{ resText(metal) }} · 石油 {{ resText(oil) }}</p>
    <p>勋章 {{ resText(credit) }} · 人口 {{ resText(pop) }}</p>
    <p>高级将领卡 {{ resText(cards.general) }} · 参谋卡 {{ resText(cards.staff) }} · 实验卡 {{ resText(cards.lab) }} · 自动化探索卡 {{ resText(cards.auto) }} · 豪饮卡 {{ resText(cards.drink) }} · 紫色特工令 {{ resText(cards.agent) }}</p>
    <p>中级补给箱 {{ resText(cards.box_mid) }} · 中级钥匙 {{ resText(cards.box_mid_key) }} · 高级补给箱 {{ resText(cards.box_high) }} · 高级钥匙 {{ resText(cards.box_high_key) }}</p>
    <p>招募卡自选 {{ resText(cards.recruit) }} · 探索卡自选 {{ resText(cards.explore) }}</p>
    <p class="muted">进入这一页就读攻打号背包里的这几种卡。开卡、换自选包或抽卡之后，剩余张数会跟着背包更新。还没收到就显示还没读到。</p>
    <fieldset class="locked-area">
      <details class="campaign-box">
        <summary>使用物品</summary>
        <FoldPart title="使用背包物品">
          <form class="lock-row" @submit.prevent="runCards(picked.slice()).catch((e) => showErr(e.message))">
            <span class="switch">开资源卡</span>
            <button type="submit">开卡</button>
          </form>
          <div class="card-picks">
            <label v-for="card in catalog" :key="card.id" class="choice">
              <input type="checkbox" :checked="cardOn(card.id)" @change="toggleCard(card.id, $event.target.checked)" />
              {{ card.name }}
            </label>
          </div>
          <form class="lock-row" @submit.prevent="saveCards().catch((e) => showErr(e.message))">
            <button type="submit" class="ghost">保存</button>
            <span v-if="cardNote" class="muted">{{ cardNote }}</span>
          </form>
          <p class="muted">资源箱整叠打开，每张变成金属卡和石油卡各 1 张，然后再按面额从小到大开勾上的资源卡。一次只开 1 张资源卡。某种没有，或者这一张没开成，这种就停，换下一种。保存后下次进来还是这份。</p>
        </FoldPart>
        <FoldPart title="清理背包">
          <form class="lock-row" @submit.prevent="runClean(cleanPicked.slice()).catch((e) => showErr(e.message))">
            <span class="switch">清理勾选的物品</span>
            <button type="submit">清理</button>
          </form>
          <div class="card-picks">
            <label v-for="item in cleanList" :key="item.id" class="choice">
              <input type="checkbox" :checked="cleanOn(item.id)" @change="toggleClean(item.id, $event.target.checked)" />
              {{ item.name }}
            </label>
          </div>
          <form class="lock-row" @submit.prevent="saveCleanItems().catch((e) => showErr(e.message))">
            <button type="submit" class="ghost">保存</button>
            <span v-if="cleanNote" class="muted">{{ cleanNote }}</span>
          </form>
          <p class="muted">勾上的按背包数量整叠使用。战略自选宝箱平均分成 10064、10065、10066、10067，坦克研究宝箱平均分成 10088、10087，余数给前面的选项。先开宝箱，再使用开出来的军魂卡。背包里没有的跳过。保存后下次进来还是这份。</p>
        </FoldPart>
        <FoldPart title="抽卡">
          <form class="lock-row" @submit.prevent="runDraw('haoyin', drinkCount).catch((e) => showErr(e.message))">
            <span class="switch">英雄豪饮</span>
            <span class="card-have">豪饮卡 {{ resText(cards.drink) }}</span>
            <label>卡数量<input v-model="drinkCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">抽取</button>
          </form>
          <form class="lock-row" @submit.prevent="runDraw('admiral', admiralCount).catch((e) => showErr(e.message))">
            <span class="switch">高级将领单抽</span>
            <span class="card-have">高级将领卡 {{ resText(cards.general) }}</span>
            <label>卡数量<input v-model="admiralCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">抽取</button>
          </form>
          <form class="lock-row" @submit.prevent="runDraw('admiral10', admiral10Count).catch((e) => showErr(e.message))">
            <span class="switch">高级将领十连</span>
            <span class="card-have">高级将领卡 {{ resText(cards.general) }}</span>
            <label>卡数量<input v-model="admiral10Count" class="mins" inputmode="numeric" required /></label>
            <button type="submit">抽取</button>
          </form>
          <form class="lock-row" @submit.prevent="runDraw('agent10', agentCount).catch((e) => showErr(e.message))">
            <span class="switch">紫色特工令十连</span>
            <span class="card-have">紫色特工令 {{ resText(cards.agent) }}</span>
            <label>数量<input v-model="agentCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">抽取</button>
          </form>
          <p class="muted">数量是要消耗的卡。豪饮用物品 10061，一张抽一次。高级将领用物品 10020，单抽一张一次。紫色特工令用物品 10095，十连一次用 10 个。十连按数量除以 10 向下取整。不够就停，并写出剩余张数。回包里会单独写出这次得到的紫色特工令个数。1 到 99999，十连至少 10 张。点下去会先确认卡和张数。</p>
        </FoldPart>
        <FoldPart title="补给箱">
          <form class="lock-row" @submit.prevent="runBox('box_mid', boxMidCount).catch((e) => showErr(e.message))">
            <span class="switch">中级补给箱十连</span>
            <span class="card-have">箱子 {{ resText(cards.box_mid) }} · 钥匙 {{ resText(cards.box_mid_key) }}</span>
            <label>数量<input v-model="boxMidCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">开启</button>
          </form>
          <form class="lock-row" @submit.prevent="runBox('box_high', boxHighCount).catch((e) => showErr(e.message))">
            <span class="switch">高级补给箱十连</span>
            <span class="card-have">箱子 {{ resText(cards.box_high) }} · 钥匙 {{ resText(cards.box_high_key) }}</span>
            <label>数量<input v-model="boxHighCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">开启</button>
          </form>
          <p class="muted">数量是要开的箱子个数。中级箱 60002 配钥匙 10038，高级箱 60003 配钥匙 10039。这两样在装备栏，进这一页时读一次。开完之后数量会跟着变。一次各扣 10。箱子或钥匙不够 10 就停。25 个开 2 次、用 20 个箱子和 20 把钥匙。点下去会先确认。</p>
        </FoldPart>
        <FoldPart title="换卡">
          <form class="lock-row" @submit.prevent="runPick('pick_general', generalCount).catch((e) => showErr(e.message))">
            <span class="switch">换高级将领卡</span>
            <span class="card-have">招募卡自选 {{ resText(cards.recruit) }} · 高级将领卡 {{ resText(cards.general) }}</span>
            <label>数量<input v-model="generalCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">兑换</button>
          </form>
          <form class="lock-row" @submit.prevent="runPick('pick_staff', staffCount).catch((e) => showErr(e.message))">
            <span class="switch">换参谋卡</span>
            <span class="card-have">招募卡自选 {{ resText(cards.recruit) }} · 参谋卡 {{ resText(cards.staff) }}</span>
            <label>数量<input v-model="staffCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">兑换</button>
          </form>
          <form class="lock-row" @submit.prevent="runPick('pick_lab', labPickCount).catch((e) => showErr(e.message))">
            <span class="switch">换实验卡</span>
            <span class="card-have">探索卡自选 {{ resText(cards.explore) }} · 实验卡 {{ resText(cards.lab) }}</span>
            <label>数量<input v-model="labPickCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">兑换</button>
          </form>
          <form class="lock-row" @submit.prevent="runPick('pick_auto', autoCount).catch((e) => showErr(e.message))">
            <span class="switch">换自动化探索卡</span>
            <span class="card-have">探索卡自选 {{ resText(cards.explore) }} · 自动化探索卡 {{ resText(cards.auto) }}</span>
            <label>数量<input v-model="autoCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">兑换</button>
          </form>
          <p class="muted">招募自选包换成高级将领卡或参谋卡，探索自选包换成实验卡或自动化探索卡。数量是要换出的张数，也是消耗的包数。点下去会先确认。</p>
        </FoldPart>
        <FoldPart title="探索">
          <form class="lock-row" @submit.prevent="runLab50(lab50Count).catch((e) => showErr(e.message))">
            <span class="switch">实验室探索50连</span>
            <span class="card-have">实验卡 {{ resText(cards.lab) }}</span>
            <label>次数<input v-model="lab50Count" class="mins" inputmode="numeric" required /></label>
            <button type="submit">探索</button>
          </form>
          <form class="lock-row" @submit.prevent="runAuto50(auto50Count).catch((e) => showErr(e.message))">
            <span class="switch">自动化探索50连</span>
            <span class="card-have">自动化探索卡 {{ resText(cards.auto) }}</span>
            <label>次数<input v-model="auto50Count" class="mins" inputmode="numeric" required /></label>
            <button type="submit">探索</button>
          </form>
          <p v-if="err && errAt === 'base' && err.includes('数量不足')" class="err">{{ err }}</p>
          <p class="muted">实验室 50 连一次用 50 张实验卡，自动化探索 50 连一次用 50 张自动化探索卡，次数默认 1。点下去会先确认。背包不够这次要的张数时，提示数量不足，不发，也不扣勋章。</p>
        </FoldPart>
      </details>
      <details class="campaign-box">
        <summary>兵种制造</summary>
        <div class="item-part">
          <h3>兵营</h3>
          <p>当前爆破兵 {{ blast === null || blast === undefined ? "还没读到" : blast }}</p>
          <form class="lock-row" @submit.prevent="runBlast(count).catch((e) => showErr(e.message))">
            <span class="switch">爆破兵制造</span>
            <label>数量<input v-model="count" class="mins" inputmode="numeric" required /></label>
            <button type="submit">开始制造</button>
          </form>
          <p class="muted">按这个数量循环。兵营里已经在造爆破兵时，直接快速制造，不再开工。剩余时间算出 0 勋章才加速，credit 固定写 0。要花勋章就停在这一只，不发。大厅蓝钻大约要等剩余降到 600 秒。消耗金属和石油。数量 1 到 399。已经在造的那一只，加速回包里有新兵才算进数量。旧队列时间走完了不算。</p>
        </div>
        <div class="item-part">
          <h3>战车工厂</h3>
          <form class="lock-row" @submit.prevent="runFactory('12', aaCount).catch((e) => showErr(e.message))">
            <span class="switch">防空车制造</span>
            <label>数量<input v-model="aaCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">开始制造</button>
          </form>
          <form class="lock-row" @submit.prevent="runFactory(army, factoryCount).catch((e) => showErr(e.message))">
            <span class="switch">其他战车</span>
            <label>兵种<input v-model="army" class="mins" inputmode="numeric" required /></label>
            <label>数量<input v-model="factoryCount" class="mins" inputmode="numeric" required /></label>
            <button type="submit">开始制造</button>
          </form>
          <p class="muted">防空车是兵种 12，只在建筑类型 30 的战车工厂造。和兵营同一套：先生产，剩余时间算出 0 勋章才加速，credit 固定写 0。要花勋章就停在这一辆，不发。其他战车填兵种号，例如猛犸 45、T99 90。数量 1 到 399。</p>
        </div>
      </details>
    </fieldset>
    <p v-if="note" class="muted">{{ note }}</p>
    <p v-if="err && errAt === 'base'" class="err">{{ err }}</p>
    <div class="recent-dock" :class="{ collapsed: !recentOpen }">
      <div class="recent-head">
        <h2>最近执行</h2>
        <button type="button" class="ghost recent-toggle" @click="recentOpen = !recentOpen">{{ recentOpen ? "向下收起" : "向上展开" }}</button>
      </div>
      <p v-if="!jobs.length" class="muted">还没有执行记录。</p>
      <div v-else ref="recentScroller" class="wide recent-scroll">
        <table class="daily-jobs">
          <thead>
            <tr><th>项目</th><th>状态</th><th>说明</th><th>北京时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="it in jobs" :key="it.id">
              <td>{{ it.label }}</td>
              <td>{{ it.status }}<button v-if="it.status === '排队' || it.status === '正在做'" type="button" class="ghost" @click="cancelJob(it).catch((e) => showErr(e.message))">关停</button></td>
              <td class="reason">{{ it.detail || "—" }}</td>
              <td>{{ it.created_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
