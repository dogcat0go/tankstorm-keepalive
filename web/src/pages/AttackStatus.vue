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
  setAttackPause: { type: Function, required: true },
  closeAttack: { type: Function, required: true },
  closeBusy: { type: Boolean, default: false },
  saveOffAt: { type: Function, required: true },
  offNote: { type: String, default: "" },
  reloadQr: { type: Function, required: true },
  showErr: { type: Function, required: true },
  loginPlatDefault: { type: String, default: "空间" },
});

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
        <li>先选 QQ 空间还是 QQ 游戏大厅，再点「登录」，二维码会出现在这里。</li>
        <li>用另一台设备打开手机 QQ，扫这张图。</li>
        <li>扫完在手机上点「确认登录」。这个 QQ 就绑到当前账号，之后只用这一个。</li>
      </ol>
      <ol v-else>
        <li>用另一台设备的手机 QQ 扫下面这张码。</li>
        <li>扫完在手机上点「确认登录」，停在确认页才算扫上。</li>
        <li>过期后这里会自动换成新码，对着页面上最新的一张扫。</li>
      </ol>
      <p class="muted">不要把图存进同一台手机的相册再扫，腾讯会拒绝。一个登录账号只绑一个攻打 QQ，绑上之后不能换成另一个。平时从大厅进游戏的选大厅，两边不是同一套登录票据。NapCat 第一次登录要扫它自己的码，管理员打开 /api/test。</p>
      <p v-if="bindWait && !qrSrc" class="bind-wait">二维码正在生成，请稍等。</p>
    </div>
    <p v-if="pushLoginVisible(proc)" class="login-row">
      <label class="choice"><input v-model="loginPlat" type="radio" value="空间" /> QQ空间</label>
      <label class="choice"><input v-model="loginPlat" type="radio" value="大厅" /> QQ游戏大厅</label>
      <button type="button" class="login" @click="pushLogin(loginPlat).catch((e) => showErr(e.message, 'bar'))">登录</button>
    </p>
    <div class="proc">
      <span class="proc-text">攻打 QQ {{ proc && proc.qq ? proc.qq : "还没绑定" }}：{{ procText(proc) }}<template v-if="proc && proc.online && proc.seen_at && !proc.paused"> · {{ proc.seen_at }}</template></span>
      <button v-if="proc && proc.online && proc.paused && proc.phase !== 'login' && proc.keepalive" type="button" class="ghost" @click="setAttackPause(false).catch((e) => showErr(e.message, 'bar'))">继续</button>
      <button v-else-if="proc && proc.online && proc.keepalive" type="button" class="ghost" @click="setAttackPause(true).catch((e) => showErr(e.message, 'bar'))">暂停</button>
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
    <p v-if="err && errAt === 'bar'" class="err">{{ err }}</p>
  </section>
</template>
