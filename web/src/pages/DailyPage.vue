<script setup>
import { computed } from "vue";
import { ElSwitch } from "element-plus";
import "element-plus/es/components/switch/style/css";

const dailyAt = defineModel("dailyAt", { type: String, required: true });
const pveStages = defineModel("pveStages", { type: String, required: true });
const modoCards = defineModel("modoCards", { type: String, required: true });
const modoAt = defineModel("modoAt", { type: String, required: true });

const props = defineProps({
  dailyQq: { type: String, default: "" },
  campaignTasks: { type: Array, required: true },
  progressTasks: { type: Array, required: true },
  dailyJobs: { type: Array, required: true },
  guildBoard: { type: Object, default: null },
  dailyAtNote: { type: String, default: "" },
  modoAtNote: { type: String, default: "" },
  dailyNote: { type: String, default: "" },
  dailySwitching: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  proc: { type: Object, default: null },
  runDaily: { type: Function, required: true },
  cancelDaily: { type: Function, required: true },
  saveDailyAt: { type: Function, required: true },
  saveModoAt: { type: Function, required: true },
  saveDailySwitch: { type: Function, required: true },
  addModo: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

const guildClaimable = computed(() =>
  (props.guildBoard?.tasks || []).filter((it) => it.state === "可领奖").length);
</script>

<template>
  <section>
    <h2>日常任务</h2>
    <p v-if="dailyQq">攻打 QQ {{ dailyQq }}</p>
    <p v-else class="muted">还没绑定攻打 QQ。按页面上方的步骤点登录并扫码。一个登录账号只绑一个攻打号，这里的每一项都用那个号做。</p>
    <p class="muted">由这个账号的攻打线程执行，不另开连接。正在打的那一单会先打完，然后做这项。后面的攻打单排在它后面。</p>
    <form class="lock-row" @submit.prevent="runDaily('daily', { stages: pveStages }).catch((e) => showErr(e.message))">
      <span class="switch">每日任务</span>
      <button type="submit">跑一轮</button>
    </form>
    <form class="lock-row" @submit.prevent="saveDailyAt().catch((e) => showErr(e.message))">
      <span class="switch">定时执行</span>
      <label>北京时间<input v-model="dailyAt" type="time" class="clock" /></label>
      <button type="submit">保存</button>
      <span class="muted">{{ dailyAtNote }}</span>
    </form>
    <p class="muted">按下面今日进度里打开的项做。开关记在这个登录账号上。今日次数记在这个攻打号上，换一个号单独算。填了时间就每天到点再跑一轮，留空不定时。标着恢复中的项不会执行。</p>
    <details class="campaign-box">
      <summary>征战世界</summary>
      <form class="campaign-line" @submit.prevent="runDaily('pve', { stages: pveStages }).catch((e) => showErr(e.message))">
        <span class="campaign-ops">
          <input v-model="pveStages" aria-label="关卡" placeholder="终点，例如 150" />
          <button type="submit">开始征战</button>
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
            :disabled="it.locked"
            @change="saveDailySwitch(it, $event)"
          />
          <span class="muted">今日 {{ it.done }}/{{ it.max }}</span>
        </span>
      </div>
      <p class="muted">征战和命令行 --pve 同一套。填一个终点，就从当前关打到这一关。还没到就接着打，不重开。已经到了或超过，免费重开一次，再从第 1 关打到终点；打到了还有免费次数，再重开一次，再从第 1 关打。这一轮没打过就停，不再重开。一天最多 2 次。留空只打当前这一关。第三次、第4次排在每日领奖之后，将领授勋和加强授勋再排在这两次后面；还在往终点打的这一轮先不做。第三次重开之后要从第 1 关打到终点，打完才发第4次；第4次也是重开后再打到终点。只重开没打完不算完成。第4次扣 100 勋章。第三次是付费重征。默认关。</p>
    </details>
    <form class="lock-row" @submit.prevent="addModo().catch((e) => showErr(e.message))">
      <span class="switch">刷摩多军团</span>
      <label class="choice">恢复卡<input v-model="modoCards" class="mins" inputmode="numeric" required /></label>
      <button type="submit">提交</button>
    </form>
    <form class="lock-row" @submit.prevent="saveModoAt().catch((e) => showErr(e.message))">
      <span class="switch">定时刷摩多</span>
      <label>北京时间<input v-model="modoAt" type="time" class="clock" /></label>
      <button type="submit">保存</button>
      <span class="muted">{{ modoAtNote }}</span>
    </form>
    <p class="muted">按攻打号的国家，去离自己最近的一座摩多军团。先走进那座城，召唤支援兵，再打。召唤失败时用功勋补一轮支援兵，再召唤一次；紧接着还失败就停。恢复卡都在这一座用完。0 表示不用卡，行动力不够就停。提交后跳到国战助手，订单和进度都在那里。击退数量按 4 次扫荡算 1 个。要这个号已经挂机保活。填了时间就每天到点按这里的恢复卡数量再刷一轮，刷完再领一次国家宝箱。正在打的那一单会先打完。留空不定时。</p>
    <p v-if="dailyNote" class="muted">{{ dailyNote }}</p>
    <p v-if="err && errAt === 'daily'" class="err">{{ err }}</p>
    <details class="guild-fold">
      <summary>
        公会任务
        <span class="muted">功能尚在完善</span>
        <span v-if="!guildBoard" class="muted">还没读到</span>
        <span v-else class="muted">今日参与 {{ guildBoard.finish }}/{{ guildBoard.cap }} · {{ guildClaimable ? "身上 " + guildClaimable + " 条可以领" : "现在不能领奖" }}</span>
      </summary>
      <p v-if="!guildBoard" class="muted">还没读到公会任务</p>
      <template v-else>
        <p class="muted">今日参与是游戏里的参与次数。参加一次加一，身上这些任务已经算进去了，不再另加。领奖不加次数。活跃任务是另一项，不进这 5 个。可领奖是整队都完成，游戏里按钮是领取。自己做完、队友还没齐是等队友，游戏里是强制完成，这里不强制完成。<span v-if="guildBoard.read_at"> {{ guildBoard.read_at }} 读到的。</span></p>
        <p v-if="!guildBoard.tasks.length" class="muted">身上没有任务。</p>
        <div v-else class="wide">
          <table>
            <thead>
              <tr><th>任务</th><th>星</th><th>状态</th><th>进度</th></tr>
            </thead>
            <tbody>
              <tr v-for="(it, i) in guildBoard.tasks" :key="i">
                <td>
                  <div>{{ it.name }}</div>
                  <div v-if="it.detail" class="muted">{{ it.detail }}</div>
                </td>
                <td>{{ it.star ? it.star + "星" : "—" }}</td>
                <td>{{ it.state }}</td>
                <td>{{ it.progress || "—" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </details>
    <div class="lock-row">
      <h2>最近执行</h2>
      <button type="button" @click="runDaily('guild_claim').catch((e) => showErr(e.message))">一键领取</button>
    </div>
    <p v-if="!dailyJobs.length" class="muted">还没有执行记录。</p>
    <div v-else class="wide">
      <table class="daily-jobs">
        <thead>
          <tr><th>项目</th><th>状态</th><th>说明</th><th>北京时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in dailyJobs" :key="it.id">
            <td>{{ it.label }}</td>
            <td>{{ it.status }}<button v-if="it.status === '排队' || it.status === '正在做'" type="button" class="ghost" @click="cancelDaily(it).catch((e) => showErr(e.message))">关停</button></td>
            <td class="reason">{{ it.detail || "—" }}</td>
            <td>{{ it.created_at || "—" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <h2>今日进度</h2>
    <p class="muted">点开关就保存。跑一轮时按这里的开和关做。标着恢复中的项先不能开。</p>
    <p v-if="!progressTasks.length" class="muted">还没有任务表。</p>
    <div v-else class="wide">
      <table>
        <thead>
          <tr><th>任务</th><th>开关</th><th>今日</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in progressTasks" :key="it.key">
            <td>
              <div>{{ it.name }}</div>
              <div v-if="it.key === '公会任务'" class="muted">{{ it.hint || "开着时每小时看一次。今日参与还没到 5 个就去领任务。到了 5 个，能领奖才去领，没有可领的就不动。身上的任务已经算进这个次数。活跃任务另算。" }}</div>
            </td>
            <td>
              <el-switch
                class="daily-switch"
                size="large"
                inline-prompt
                active-text="开"
                inactive-text="关"
                :model-value="it.on"
                :loading="dailySwitching === it.key"
                :disabled="it.locked"
                @change="saveDailySwitch(it, $event)"
              />
              <span v-if="it.locked" class="muted">功能尚在恢复中...</span>
              <span v-else-if="it.key === '公会任务'" class="muted">功能尚在完善</span>
            </td>
            <td>{{ it.key === "公会任务" && guildBoard ? guildBoard.finish : it.done }}/{{ it.max }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
