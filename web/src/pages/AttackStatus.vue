<script setup>
import { ref, watch } from "vue";

const offAt = defineModel("offAt", { type: String, required: true });

const props = defineProps({
  proc: { type: Object, default: null },
  qrSrc: { type: String, default: "" },
  bindWait: { type: Boolean, default: false },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  pushLoginVisible: { type: Function, required: true },
  procText: { type: Function, required: true },
  pushLogin: { type: Function, required: true },
  closeAttack: { type: Function, required: true },
  closeBusy: { type: Boolean, default: false },
  saveOffAt: { type: Function, required: true },
  offNote: { type: String, default: "" },
  reloadQr: { type: Function, required: true },
  showErr: { type: Function, required: true },
  loginPlatDefault: { type: String, default: "空间" },
  command: { type: Object, default: null },
  saveCommandAccept: { type: Function, required: true },
  inviteMember: { type: Function, required: true },
  kickMember: { type: Function, required: true },
  renameSquad: { type: Function, required: true },
});

const inviteWho = ref("");
const squadName = ref("国战编队");
watch(() => props.command && props.command.team_id, () => {
  squadName.value = props.command && props.command.name ? props.command.name : "国战编队";
});

async function answer(on) {
  try {
    await props.saveCommandAccept(on);
  } catch (e) {
    props.showErr(e.message, "bar");
  }
}

async function sendInvite() {
  try {
    await props.inviteMember(inviteWho.value.trim());
    inviteWho.value = "";
  } catch (e) {
    props.showErr(e.message, "bar");
  }
}

async function rename() {
  try {
    await props.renameSquad(squadName.value);
  } catch (e) {
    props.showErr(e.message, "bar");
  }
}

async function kick(member) {
  try {
    await props.kickMember(member.id);
  } catch (e) {
    props.showErr(e.message, "bar");
  }
}

const loginPlat = ref("空间");
try {
  const saved = localStorage.getItem("ts_login_plat");
  loginPlat.value = saved === "大厅" || saved === "空间"
    ? saved
    : (props.loginPlatDefault || "空间");
} catch (e) {
  loginPlat.value = props.loginPlatDefault || "空间";
}
watch(loginPlat, (value) => {
  try {
    localStorage.setItem("ts_login_plat", value);
  } catch (e) {}
});
watch(() => props.loginPlatDefault, (value) => {
  let saved = "";
  try {
    saved = localStorage.getItem("ts_login_plat") || "";
  } catch (e) {}
  if (saved === "大厅" || saved === "空间") return;
  if (value === "大厅" || value === "空间") loginPlat.value = value;
});
</script>

<template>
  <section class="attack-bar" aria-label="攻打号状态">
    <div v-if="!(proc && proc.qq)" class="bind-guide">
      <p class="bind-title">先绑定攻打 QQ</p>
      <ol v-if="!qrSrc">
        <li>先选 QQ 空间还是 QQ 游戏大厅，再点「登录」。</li>
        <li>用另一台设备打开手机 QQ，扫这里出现的图。</li>
        <li>扫完在手机上点「确认登录」。这个 QQ 就绑到当前账号，之后只用这一个。</li>
      </ol>
      <ol v-else>
        <li>用另一台设备的手机 QQ 扫下面这张码。</li>
        <li>扫完在手机上点「确认登录」，停在确认页才算扫上。</li>
        <li>过期后这里会自动换成新码，对着页面上最新的一张扫。</li>
      </ol>
      <p class="muted">不要把图存进同一台手机的相册再扫，腾讯会拒绝。一个登录账号只绑一个攻打 QQ，绑上之后不能换成另一个。平时从大厅进游戏的选大厅，两边不是同一套登录票据。</p>
      <p v-if="bindWait && !qrSrc" class="bind-wait">二维码正在生成，请稍等。</p>
    </div>
    <p v-if="pushLoginVisible(proc)" class="login-row">
      <label class="choice"><input v-model="loginPlat" type="radio" value="空间" /> QQ空间</label>
      <label class="choice"><input v-model="loginPlat" type="radio" value="大厅" /> QQ游戏大厅</label>
      <button type="button" class="login" @click="pushLogin(loginPlat).catch((e) => showErr(e.message, 'bar'))">登录</button>
    </p>
    <div class="proc">
      <span class="proc-text">攻打 QQ {{ proc && proc.qq ? proc.qq : "还没绑定" }}：{{ procText(proc) }}<template v-if="proc && proc.online && proc.seen_at && !proc.paused"> · {{ proc.seen_at }}</template></span>
      <button v-if="proc && proc.qq && !proc.shutdown" type="button" class="ghost" :disabled="closeBusy" @click="closeAttack().catch((e) => showErr(e.message, 'bar'))">关闭</button>
      <form class="off-set" @submit.prevent="saveOffAt().catch((e) => showErr(e.message, 'bar'))">
        <label class="choice">定时关闭
          <input v-model="offAt" type="datetime-local" class="when" step="60" />
        </label>
        <button type="submit">保存</button>
        <span class="muted">{{ offNote || "北京时间，留空不关" }}</span>
      </form>
    </div>
    <img v-if="qrSrc" class="qr" :src="qrSrc" alt="攻打号登录二维码" @error="reloadQr" />
    <p v-if="qrSrc && !(proc && proc.qq)" class="muted bind-foot">扫上之后，页面上的「还没绑定」会变成这个 QQ 号。</p>
    <div v-if="command && command.team_id" class="proc">
      <template v-if="command.self">
        <span class="proc-text">你在指挥「{{ command.name }}」· {{ command.region }}区 {{ command.country_name }}。</span>
        <form class="off-set" @submit.prevent="rename()">
          <label class="choice">编队名称
            <input v-model="squadName" maxlength="16" />
          </label>
          <button type="submit">保存</button>
        </form>
        <form class="off-set" @submit.prevent="sendInvite()">
          <label class="choice">邀请账号
            <input v-model="inviteWho" required placeholder="对方的登录账号" />
          </label>
          <button type="submit">邀请</button>
        </form>
      </template>
      <template v-else-if="command.state === 'wait'">
        <span class="proc-text">{{ command.organizer }} 邀请这个号加入「{{ command.name }}」· {{ command.region }}区 {{ command.country_name }}。</span>
        <button type="button" @click="answer(true)">接受</button>
        <button type="button" class="ghost" @click="answer(false)">拒绝</button>
      </template>
      <template v-else>
        <span class="proc-text">所在编队「{{ command.name }}」，指挥者 {{ command.organizer }}。点地图时，已加入的号会跟着做。</span>
        <button type="button" class="ghost" @click="answer(false)">退出</button>
      </template>
      <ul class="desk-lines">
        <li v-for="m in command.roster || []" :key="m.id">
          {{ m.username }}
          <span class="muted">{{ m.organizer ? "指挥者 · " : "" }}{{ m.status }}</span>
          <button v-if="command.self && !m.organizer && m.state !== 'out'" type="button" class="ghost" @click="kick(m)">踢出</button>
        </li>
      </ul>
    </div>
    <p v-if="err && errAt === 'bar'" class="err">{{ err }}</p>
  </section>
</template>
