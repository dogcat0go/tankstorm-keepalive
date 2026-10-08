<script setup>
const cityId = defineModel("cityId", { type: String, required: true });
const uid = defineModel("uid", { type: String, required: true });
const lockCards = defineModel("lockCards", { type: String, required: true });
const retreatMode = defineModel("retreatMode", { type: String, required: true });
const retreatHops = defineModel("retreatHops", { type: String, required: true });
const retreatCity = defineModel("retreatCity", { type: String, required: true });
const retreatFail = defineModel("retreatFail", { type: Boolean, required: true });
const advanced = defineModel("advanced", { type: Boolean, required: true });

defineProps({
  me: { type: Object, required: true },
  cities: { type: Array, required: true },
  autoLock: { type: Boolean, default: false },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  lockNote: { type: String, default: "" },
  retreatNote: { type: String, default: "" },
  lockHint: { type: String, default: "" },
  items: { type: Array, required: true },
  players: { type: Array, required: true },
  locking: { type: String, default: "" },
  cityLabel: { type: Function, required: true },
  addSub: { type: Function, required: true },
  saveAutoLock: { type: Function, required: true },
  saveLockCards: { type: Function, required: true },
  saveRetreat: { type: Function, required: true },
  lockOne: { type: Function, required: true },
  removeSub: { type: Function, required: true },
  lockKey: { type: Function, required: true },
  isSubOpen: { type: Function, required: true },
  onSubToggle: { type: Function, required: true },
  statusOf: { type: Function, required: true },
  whenOf: { type: Function, required: true },
  cityText: { type: Function, required: true },
  showErr: { type: Function, required: true },
});
</script>

<template>
  <section>
    <h2>监控敌人</h2>
    <p class="muted">目前只有 1 区支持订阅敌方。</p>
    <p v-if="me.region && me.region !== 1" class="err">当前是 {{ me.region }} 区，还不能订阅。</p>
    <p v-if="err && errAt === 'watch'" class="err">{{ err }}</p>
    <form v-if="!me.region || me.region === 1" @submit.prevent="addSub().catch((e) => showErr(e.message))">
      <label>城市
        <select v-model="cityId" required>
          <option value="" disabled>选择城市</option>
          <option v-for="c in cities" :key="c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
        </select>
      </label>
      <label>用户 UID<input v-model="uid" inputmode="numeric" required /></label>
      <button type="submit">订阅</button>
    </form>
    <p v-if="!me.region || me.region === 1" class="muted">这个 UID 第一次出现在扫描结果里，发一条。之后只有从不在这座城变成在线，再发一条。这一轮扫完还没见到，就记成不在这座城。</p>
    <p v-if="me.remote_attack && (!me.region || me.region === 1)" class="lock-row">
      <label v-if="me.high_tier" class="switch">
        <input type="checkbox" :checked="autoLock" @change="saveAutoLock" />
        自动锁敌
      </label>
      <button type="button" class="ghost" @click="advanced = !advanced">{{ advanced ? "收起" : "高级配置" }}</button>
      <span v-if="me.high_tier" class="muted">{{ autoLock ? "已打开。订阅的人在城里就排队攻打，打开时人已经在的，马上排一条。" : "已关闭。" }}这一单没打完就跳过，等这个人下次再出现才排。同一个人一直在城里，不会重复排。人在城里时，这一行的索敌可以再排一条。</span>
    </p>
    <div v-if="me.remote_attack && advanced && (!me.region || me.region === 1)" class="advanced">
      <form class="lock-row" @submit.prevent="saveLockCards().catch((e) => showErr(e.message))">
        <label>1小时内最多恢复卡<input v-model="lockCards" class="mins" inputmode="numeric" required /></label>
        <button type="submit">保存</button>
        <span class="muted">{{ lockNote }}</span>
      </form>
      <p class="muted">只限制自动锁敌。最近 1 小时里最多开这么多张，用满就不再开，这一单跳过。手动攻打不占这个数。</p>
      <form class="lock-row retreat-row" @submit.prevent="saveRetreat().catch((e) => showErr(e.message))">
        <span class="switch">打完后退</span>
        <label class="choice"><input type="radio" value="off" v-model="retreatMode" />不后退</label>
        <label class="choice"><input type="radio" value="hops" v-model="retreatMode" />后退几座城</label>
        <label class="choice"><input type="radio" value="city" v-model="retreatMode" />退到指定城市</label>
        <label v-if="retreatMode === 'hops'">座数<input v-model="retreatHops" class="mins" inputmode="numeric" required /></label>
        <label v-if="retreatMode !== 'off'">{{ retreatMode === 'hops' ? '朝向' : '退到' }}
          <select v-model="retreatCity" :required="retreatMode === 'city'">
            <option v-if="retreatMode === 'city'" value="0" disabled>选择城市</option>
            <option v-if="retreatMode === 'hops' && !cities.some((c) => c.name === '马奇诺')" value="0">马奇诺</option>
            <option v-for="c in cities" :key="'r' + c.id" :value="String(c.id)">{{ cityLabel(c) }}</option>
          </select>
        </label>
        <label class="choice"><input type="checkbox" v-model="retreatFail" />没打成也后退</label>
        <button type="submit">保存</button>
        <span class="muted">{{ retreatNote }}</span>
      </form>
      <p class="muted">只对自动锁敌。打完按上面的走法退。同城的索敌订单会连着打完再退；后续还是这座城时，有一单没打完也不退。人已经在所在国首都（编号第一位是国家、第二位是 1，例如法国是 6101 巴黎）时不再后退，也不会为了后退去开恢复卡。打开「没打成也后退」时，没打到人也会退一次。后退几座城是朝所选城市走这么远就停，不走进终点。退到指定城市是走进那座城，不打它。两种走法只能选一种。</p>
    </div>
    <p v-if="lockHint" class="muted">{{ lockHint }}</p>
    <p v-if="!items.length" class="muted">还没有订阅。</p>
    <div v-else class="subs">
      <details v-for="p in players" :key="p.uid" class="sub" :open="isSubOpen(p.uid)" @toggle="onSubToggle(p.uid, $event)">
        <summary>
          <span class="sub-name">{{ p.name || p.uid }}</span>
          <span v-if="p.name" class="muted">{{ p.uid }}</span>
          <span v-if="p.present" class="on">在城里</span>
          <span class="muted">{{ p.cities.length }} 座城</span>
        </summary>
        <div v-for="it in p.cities" :key="it.city_id" class="sub-city">
          <span class="sub-where">{{ cityText(it) }}</span>
          <span class="sub-status" :class="it.present ? 'on' : 'off'">{{ statusOf(it) }}<template v-if="it.present && it.page != null"> · 第{{ it.page }}页</template></span>
          <span class="sub-time muted">{{ whenOf(it) }}</span>
          <span class="sub-actions">
            <button v-if="me.high_tier && it.present" type="button" class="ghost" title="给这个人排一条自动索敌" :disabled="locking === lockKey(it)" @click="lockOne(it).catch((e) => showErr(e.message, 'watch'))">索敌</button>
            <button type="button" class="ghost" @click="removeSub(it)">取消</button>
          </span>
        </div>
      </details>
    </div>
  </section>
</template>
