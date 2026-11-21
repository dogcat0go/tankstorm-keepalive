<script setup>
import { computed } from "vue";

const props = defineProps({
  battle: { type: Object, default: null },
});

const lanes = computed(() => {
  const rows = props.battle && props.battle.lanes;
  return Array.isArray(rows) ? rows : [];
});

const sides = [
  { key: "self", label: "我方" },
  { key: "enemy", label: "对方" },
];
const generalCols = [
  ["id", "编号"], ["lv", "等级"], ["train", "训练"],
  ["atk", "攻击"], ["def", "防御"], ["hp", "生命"], ["crt", "暴击"], ["score", "评分"],
  ["eqpAtk", "装备攻击"], ["eqpHp", "装备生命"],
  ["buffAtk", "增益攻击"], ["buffHp", "增益生命"],
  ["cut", "相性减"], ["add", "相性加"],
];
const adviserCols = [
  ["id", "编号"], ["star", "星级"], ["hit", "命中"], ["avoid", "闪避"], ["crt", "暴击"],
];

function gap(n) {
  const value = Number(n);
  if (!Number.isFinite(value) || value < 4) return "";
  return String(Math.trunc(value));
}

function hasSec(lane) {
  const value = Number(lane && lane.seconds);
  return Number.isFinite(value) && value >= 0;
}

function grade(n) {
  const value = Number(n);
  if (!Number.isFinite(value) || value < 0 || value > 30) return "";
  return `${Math.trunc(value)}阶`;
}

function num(person, key) {
  if (!person || person[key] === undefined || person[key] === null) return "";
  const value = Number(person[key]);
  if (!Number.isFinite(value) || value < 0) return "";
  return Math.trunc(value).toLocaleString("zh-CN");
}

function laneDeep(lane) {
  return !!(lane && (lane.general || lane.adviser));
}

function laneOn(lane) {
  return hasSec(lane) || !!grade(lane && lane.selfGrade) || !!grade(lane && lane.enemyGrade)
    || !!gap(lane && lane.self) || !!gap(lane && lane.enemy);
}

function sec(seconds) {
  const n = Number(seconds) || 0;
  if (n < 60) return `${n}秒`;
  const minute = Math.floor(n / 60);
  const second = n % 60;
  return second ? `${minute}分${second}秒` : `${minute}分`;
}

function wan(n) {
  const value = Number(n) || 0;
  if (value < 10000) return String(value);
  const text = (value / 10000).toFixed(1).replace(/\.0$/, "");
  return `${text}万`;
}

function tanks(side) {
  if (!side) return "—";
  return `${side.tanks}/${side.total}`;
}

function hp(side) {
  if (!side) return "—";
  const max = Number(side.maxHp) || 0;
  const pct = max > 0 ? Math.round((Number(side.hp) || 0) * 100 / max) : 0;
  return `${wan(side.hp)}/${wan(side.maxHp)}（${pct}%）`;
}

const showLanes = computed(() => lanes.value.some((lane) => laneOn(lane)));
const showDeep = computed(() => lanes.value.some((lane) => laneDeep(lane)));
</script>

<template>
  <div v-if="battle && battle.phase1" class="wg-result">
    <p>第一阶段 {{ sec(battle.phase1.seconds) }}<span v-if="!battle.climax"> · 未进入决战</span></p>
    <table>
      <thead>
        <tr><th></th><th>剩余坦克</th><th>整体血量</th></tr>
      </thead>
      <tbody>
        <tr><td>我方</td><td>{{ tanks(battle.phase1.self) }}</td><td>{{ hp(battle.phase1.self) }}</td></tr>
        <tr><td>对方</td><td>{{ tanks(battle.phase1.enemy) }}</td><td>{{ hp(battle.phase1.enemy) }}</td></tr>
      </tbody>
    </table>
    <template v-if="battle.climax">
      <p>决战 {{ sec(battle.climax.seconds) }}</p>
      <table>
        <thead>
          <tr><th></th><th>剩余坦克</th><th>最终血量</th></tr>
        </thead>
        <tbody>
          <tr><td>我方</td><td>{{ tanks(battle.climax.self) }}</td><td>{{ hp(battle.climax.self) }}</td></tr>
          <tr><td>对方</td><td>{{ tanks(battle.climax.enemy) }}</td><td>{{ hp(battle.climax.enemy) }}</td></tr>
        </tbody>
      </table>
    </template>
    <table v-if="showLanes">
      <thead>
        <tr><th>路</th><th>战斗时间</th><th>我方火炮品阶</th><th>敌方火炮品阶</th><th>我方火炮发射间隔</th><th>敌方火炮发射间隔</th></tr>
      </thead>
      <tbody>
        <template v-for="(lane, i) in lanes" :key="i">
          <tr v-if="laneOn(lane)">
            <td>第 {{ i + 1 }} 路</td>
            <td>{{ hasSec(lane) ? sec(lane.seconds) : "" }}</td>
            <td>{{ grade(lane && lane.selfGrade) }}</td>
            <td>{{ grade(lane && lane.enemyGrade) }}</td>
            <td>{{ gap(lane && lane.self) }}</td>
            <td>{{ gap(lane && lane.enemy) }}</td>
          </tr>
        </template>
      </tbody>
    </table>
    <details v-if="showDeep" class="campaign-box">
      <summary>深度解析</summary>
      <template v-for="(lane, i) in lanes" :key="'deep' + i">
        <div v-if="laneDeep(lane)">
          <p>第 {{ i + 1 }} 路</p>
          <div v-if="lane.general" class="wide">
            <table>
              <thead>
                <tr>
                  <th></th>
                  <th v-for="col in generalCols" :key="col[0]">{{ col[1] }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="side in sides" :key="side.key">
                  <td>{{ side.label }}将领</td>
                  <td v-for="col in generalCols" :key="col[0]">{{ num(lane.general[side.key], col[0]) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="lane.adviser" class="wide">
            <table>
              <thead>
                <tr>
                  <th></th>
                  <th v-for="col in adviserCols" :key="col[0]">{{ col[1] }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="side in sides" :key="side.key">
                  <td>{{ side.label }}参谋</td>
                  <td v-for="col in adviserCols" :key="col[0]">{{ num(lane.adviser[side.key], col[0]) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </details>
  </div>
</template>
