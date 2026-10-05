<script setup>
import { ElSwitch } from "element-plus";
import "element-plus/es/components/switch/style/css";

const dailyAt = defineModel("dailyAt", { type: String, required: true });
const pveStages = defineModel("pveStages", { type: String, required: true });
const fundBuilding = defineModel("fundBuilding", { type: String, required: true });
const fundTimes = defineModel("fundTimes", { type: String, required: true });
const modoCards = defineModel("modoCards", { type: String, required: true });

defineProps({
  dailyQq: { type: String, default: "" },
  campaignTasks: { type: Array, required: true },
  progressTasks: { type: Array, required: true },
  dailyJobs: { type: Array, required: true },
  dailyAtNote: { type: String, default: "" },
  dailyNote: { type: String, default: "" },
  dailySwitching: { type: String, default: "" },
  fundBuildings: { type: Array, required: true },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  proc: { type: Object, default: null },
  runDaily: { type: Function, required: true },
  saveDailyAt: { type: Function, required: true },
  saveDailySwitch: { type: Function, required: true },
  addModo: { type: Function, required: true },
  showErr: { type: Function, required: true },
});
</script>

<template>
  <section>
    <h2>日常任务</h2>
    <p v-if="dailyQq">攻打 QQ {{ dailyQq }}</p>
    <p v-else class="muted">还没绑定攻打 QQ。先在导航栏上方扫码。一个登录账号只绑一个攻打号，这里的每一项都用那个号做。</p>
    <p class="muted">由这个账号的攻打线程执行，不另开连接。正在打的那一单会先打完，然后做这项。后面的攻打单排在它后面。</p>
    <form class="lock-row" @submit.prevent="runDaily('daily', { stages: pveStages }).catch((e) => showErr(e.message))">
      <span class="switch">每日任务</span>
      <button type="submit" :disabled="!dailyQq">跑一轮</button>
    </form>
    <form class="lock-row" @submit.prevent="saveDailyAt().catch((e) => showErr(e.message))">
      <span class="switch">定时执行</span>
      <label>北京时间<input v-model="dailyAt" type="time" class="clock" /></label>
      <button type="submit">保存</button>
      <span class="muted">{{ dailyAtNote }}</span>
    </form>
    <p class="muted">按下面今日进度里打开的项做。开关记在这个登录账号上。今日次数记在这个攻打号上，换一个号单独算。填了时间就每天到点再跑一轮，留空不定时。</p>
    <details class="campaign-box">
      <summary>征战世界</summary>
      <form class="campaign-line" @submit.prevent="runDaily('pve', { stages: pveStages }).catch((e) => showErr(e.message))">
        <span class="campaign-ops">
          <input v-model="pveStages" aria-label="关卡" placeholder="终点，例如 150" />
          <button type="submit" :disabled="!dailyQq">开始征战</button>
        </span>
      </form>
      <div v-for="it in campaignTasks" :key="it.key" class="campaign-line">
        <span class="switch">{{ it.name }}</span>
        <span class="campaign-ops">
          <el-switch
            class="daily-switch"
            size="large"
            inline-prompt
            active-text="开"
            inactive-text="关"
            :model-value="it.on"
            :loading="dailySwitching === it.key"
            @change="saveDailySwitch(it, $event)"
          />
          <span class="muted">今日 {{ it.done }}/{{ it.max }}</span>
        </span>
      </div>
      <p class="muted">征战和命令行 --pve 同一套。填一个终点，就从当前关打到这一关。还没到就接着打，不重开。已经到了或超过，免费重开一次，再从第 1 关打到终点；打到了还有免费次数，再重开一次，再从第 1 关打。这一轮没打过就停，不再重开。一天最多 2 次。留空只打当前这一关。第三次、第4次排在后面；还在往终点打的这一轮先不做。第三次重开之后要从第 1 关打到终点，打完才发第4次；第4次也是重开后再打到终点。只重开没打完不算完成。第4次扣 100 勋章。第三次是付费重征。默认关。</p>
    </details>
    <form class="lock-row" @submit.prevent="runDaily('fund', { building_id: fundBuilding, times: fundTimes }).catch((e) => showErr(e.message))">
      <span class="switch">成就拨款</span>
      <label>建筑
        <select v-model="fundBuilding" class="fund-building" required>
          <option value="" disabled>选择建筑</option>
          <option v-for="b in fundBuildings" :key="b.id" :value="b.id">{{ b.name }}</option>
        </select>
      </label>
      <label>次数<input v-model="fundTimes" class="mins" inputmode="numeric" required /></label>
      <button type="submit" :disabled="!dailyQq">拨款</button>
    </form>
    <p class="muted">每次拨款前各开 4 张 1000 万金属卡和石油卡。次数 1 到 999。</p>
    <form class="lock-row" @submit.prevent="addModo().catch((e) => showErr(e.message))">
      <span class="switch">刷摩多军团</span>
      <label class="choice">恢复卡<input v-model="modoCards" class="mins" inputmode="numeric" required /></label>
      <button type="submit" :disabled="!dailyQq || !(proc && proc.keepalive)">提交</button>
    </form>
    <p class="muted">按攻打号的国家，去首都旁边两座摩多军团。先走进那座城，召唤支援兵，再打。这两座共用这么多张恢复卡，先打的那座最多用一半。0 表示不用卡，行动力不够就停。提交后跳到国战助手，订单和进度都在那里。击退数量按 4 次扫荡算 1 个。要这个号已经挂机保活。</p>
    <p v-if="dailyNote" class="muted">{{ dailyNote }}</p>
    <p v-if="err && errAt === 'daily'" class="err">{{ err }}</p>
    <h2>最近执行</h2>
    <p v-if="!dailyJobs.length" class="muted">还没有执行记录。</p>
    <div v-else class="wide">
      <table class="daily-jobs">
        <thead>
          <tr><th>项目</th><th>状态</th><th>说明</th><th>北京时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in dailyJobs" :key="it.id">
            <td>{{ it.label }}</td>
            <td>{{ it.status }}</td>
            <td class="reason">{{ it.detail || "—" }}</td>
            <td>{{ it.created_at || "—" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <h2>今日进度</h2>
    <p class="muted">点开关就保存。跑一轮时按这里的开和关做。</p>
    <p v-if="!progressTasks.length" class="muted">还没有任务表。</p>
    <div v-else class="wide">
      <table>
        <thead>
          <tr><th>任务</th><th>开关</th><th>今日</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in progressTasks" :key="it.key">
            <td>{{ it.name }}</td>
            <td>
              <el-switch
                class="daily-switch"
                size="large"
                inline-prompt
                active-text="开"
                inactive-text="关"
                :model-value="it.on"
                :loading="dailySwitching === it.key"
                @change="saveDailySwitch(it, $event)"
              />
            </td>
            <td>{{ it.done }}/{{ it.max }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
