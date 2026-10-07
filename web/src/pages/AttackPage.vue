<script setup>
const attackCity = defineModel("attackCity", { type: String, required: true });
const attackUid = defineModel("attackUid", { type: String, required: true });
const cardMax = defineModel("cardMax", { type: String, required: true });
const attackAdvanced = defineModel("attackAdvanced", { type: Boolean, required: true });
const clearMode = defineModel("clearMode", { type: String, required: true });
const clearFrom = defineModel("clearFrom", { type: String, required: true });
const clearTo = defineModel("clearTo", { type: String, required: true });
const clearWait = defineModel("clearWait", { type: String, required: true });
const clearScan = defineModel("clearScan", { type: String, required: true });
const prioUid = defineModel("prioUid", { type: String, required: true });
const prioRank = defineModel("prioRank", { type: String, required: true });
const moveCity = defineModel("moveCity", { type: String, required: true });
const clearRows = defineModel("clearRows", { type: Array, required: true });

defineProps({
  me: { type: Object, required: true },
  proc: { type: Object, default: null },
  cities: { type: Array, required: true },
  orders: { type: Array, required: true },
  storms: { type: Array, required: true },
  cityCounts: { type: Array, required: true },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  clearNote: { type: String, default: "" },
  clearRetryText: { type: String, default: "" },
  prioShown: { type: Array, required: true },
  cityLabel: { type: Function, required: true },
  hereText: { type: Function, required: true },
  cityText: { type: Function, required: true },
  addOrder: { type: Function, required: true },
  saveClearPlan: { type: Function, required: true },
  addPriority: { type: Function, required: true },
  moveToCity: { type: Function, required: true },
  cancelOrder: { type: Function, required: true },
  beatText: { type: Function, required: true },
  orderCity: { type: Function, required: true },
  orderUid: { type: Function, required: true },
  orderStatus: { type: Function, required: true },
  orderOpen: { type: Function, required: true },
  holdCell: { type: Function, required: true },
  showErr: { type: Function, required: true },
});

function dropPriority(uid) {
  clearRows.value = clearRows.value.filter((item) => item.uid !== uid);
}
</script>

<template>
  <section>
    <h2>国战助手</h2>
    <p v-if="err && errAt === 'attack'" class="err">{{ err }}</p>
    <p v-if="!me.remote_attack" class="muted">当前是{{ me.tier || "初级" }}。打人和清城要中级或高级。刷摩多军团在日常任务。</p>
    <template v-if="me.remote_attack">
      <form class="attack-row" @submit.prevent="addOrder()">
        <label>城市
          <select v-model="attackCity" required>
            <option value="" disabled>选择城市</option>
            <option v-for="c in cities.filter((c) => !/魔多|摩多/.test(c.name || ''))" :key="c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
          </select>
        </label>
        <label>UID<input v-model="attackUid" inputmode="numeric" placeholder="留空则打整座城" /></label>
        <label>最多恢复卡<input v-model="cardMax" class="mins" inputmode="numeric" required /></label>
        <button type="submit" :disabled="!(proc && proc.keepalive)">提交攻打</button>
      </form>
      <p class="muted">打完或打不过之后，游戏连接再保持账号旁设定的挂机时间，可和自动锁敌一起用。有打不过的人挡路时，这段时间会继续看路径，通了立刻接着打原来的订单。0 表示打完就下线。继续和提交攻打都要这个号已经挂机保活。没挂上就下单，游戏也登不进去。</p>
      <p v-if="me.high_tier">
        <button type="button" class="ghost" @click="attackAdvanced = !attackAdvanced">{{ attackAdvanced ? "收起" : "高级配置" }}</button>
      </p>
      <div v-if="me.high_tier && attackAdvanced" class="advanced">
        <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => showErr(e.message))">
          <span class="switch">清城扫页</span>
          <label class="choice"><input type="radio" value="head" v-model="clearMode" />前5页</label>
          <label class="choice"><input type="radio" value="range" v-model="clearMode" />指定范围</label>
          <template v-if="clearMode === 'range'">
            <label>从<input v-model="clearFrom" class="mins" inputmode="numeric" required /></label>
            <label>到<input v-model="clearTo" class="mins" inputmode="numeric" required /></label>
          </template>
        </form>
        <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => showErr(e.message))">
          <span class="switch">空城再打</span>
          <label class="choice">分钟<input v-model="clearWait" class="mins" inputmode="numeric" required /></label>
          <span v-if="clearRetryText" class="muted">{{ clearRetryText }}</span>
        </form>
        <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => showErr(e.message))">
          <span class="switch">扫页冷却</span>
          <label class="choice">秒<input v-model="clearScan" class="mins" inputmode="numeric" required /></label>
        </form>
        <form class="lock-row" @submit.prevent="addPriority().catch((e) => showErr(e.message))">
          <span class="switch">优先 UID</span>
          <label>UID<input v-model="prioUid" inputmode="numeric" /></label>
          <label>优先级<input v-model="prioRank" class="mins" inputmode="numeric" /></label>
          <button type="submit">添加</button>
        </form>
        <div v-for="(row, i) in prioShown" :key="row.uid" class="prio-row">
          <span class="muted">{{ i + 1 }}</span>
          <span>{{ row.uid }}</span>
          <label>优先级<input v-model="row.rank" class="mins" inputmode="numeric" /></label>
          <button type="button" class="ghost" @click="dropPriority(row.uid)">删除</button>
        </div>
        <form class="lock-row" @submit.prevent="saveClearPlan().catch((e) => showErr(e.message))">
          <button type="submit">保存</button>
          <span class="muted">{{ clearNote }}</span>
        </form>
        <p class="muted">只对留空 UID 的清城。攻打号自己扫这些页。优先名单里数字小的先打，同一级按扫到的先后。名单以外的人排在后面，再往后的页不打。扫页冷却是两次扫页至少隔开的秒数，填 0 表示每次出手后的冷却都扫。到点就在那次冷却里再扫，新上来的人按同样的顺序接着打。最多 50 个 UID。这几页没人，或者还剩打不过的人时，过上面的分钟再启动同一条订单，倒计时写在这一行和下面的订单里。0 表示空了或清不完就结束。</p>
      </div>
    </template>
    <p v-if="proc && proc.online" class="proc here-row">
      <span v-if="hereText(proc)">{{ hereText(proc) }}</span>
      <template v-if="me.remote_attack">
        <label class="choice">移动到
          <select v-model="moveCity">
            <option value="" disabled>选择城市</option>
            <option v-for="c in cities.filter((c) => !/魔多|摩多/.test(c.name || ''))" :key="'mv' + c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
          </select>
        </label>
        <button type="button" @click="moveToCity().catch((e) => showErr(e.message))">移动</button>
        <span v-if="proc.move_note" class="muted">{{ proc.move_note }}</span>
      </template>
    </p>
    <div v-if="storms.length" class="storms">
      <p class="muted">最近 1 小时拒绝的超级强攻</p>
      <p v-for="(s, i) in storms" :key="i">{{ s.at }} · {{ s.name }}</p>
    </div>
    <p class="muted">每 2 秒刷新一次。每个攻打 QQ 各有一条线程，状态按 QQ 号分开。还没打完的最多两条，下面最多显示三条。还没绑定的，按页面上方的步骤先扫码。点了登录，就挂机保活 180 分钟，连上之后可以接订单。一个登录账号只绑一个攻打 QQ，绑上之后不能换成另一个。同一个 QQ 可以绑给多个登录账号。</p>
    <div class="orders" v-if="orders.length">
      <table>
        <thead>
          <tr><th>城市</th><th>名称</th><th>击退敌方数量</th><th>状态</th><th>说明</th><th>保活剩余倒计时</th><th>北京时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in orders" :key="it.id">
            <td>{{ orderCity(it) }}</td>
            <td>{{ orderUid(it) }}</td>
            <td>{{ beatText(it) }}</td>
            <td>{{ orderStatus(it.status) }}<button v-if="orderOpen(it)" type="button" class="ghost" @click="cancelOrder(it).catch((e) => showErr(e.message))">关停</button></td>
            <td class="reason">{{ it.reason || "—" }}</td>
            <td>{{ holdCell(it) }}</td>
            <td>{{ it.created_at || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <div class="order-cards">
        <article class="order-card" v-for="it in orders" :key="'c' + it.id">
          <p><span class="k">城市</span>{{ orderCity(it) }}</p>
          <p><span class="k">名称</span>{{ orderUid(it) }}</p>
          <p><span class="k">击退敌方数量</span>{{ beatText(it) }}</p>
          <p><span class="k">状态</span>{{ orderStatus(it.status) }}</p>
          <p class="reason"><span class="k">说明</span>{{ it.reason || "—" }}</p>
          <p><span class="k">保活剩余倒计时</span>{{ holdCell(it) }}</p>
          <p><span class="k">北京时间</span>{{ it.created_at || "—" }}</p>
          <p v-if="orderOpen(it)"><button type="button" class="ghost" @click="cancelOrder(it).catch((e) => showErr(e.message))">关停</button></p>
        </article>
      </div>
    </div>
    <h2>城市人数</h2>
    <p class="muted">订阅过的城。人数是最近一次扫描打开面板时的玩家数量，同一座城只列一行。</p>
    <p v-if="!cityCounts.length" class="muted">还没有订阅的城市。</p>
    <div v-else class="wide">
      <table>
        <thead>
          <tr><th>城市</th><th>人数</th><th>扫描时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in cityCounts" :key="it.city_id">
            <td>{{ cityText(it) }}</td>
            <td>{{ it.user_cnt == null ? "—" : it.user_cnt }}</td>
            <td>{{ it.scanned_at || "—" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
