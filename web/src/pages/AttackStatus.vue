<script setup>
defineProps({
  proc: { type: Object, default: null },
  qrSrc: { type: String, default: "" },
  err: { type: String, default: "" },
  errAt: { type: String, default: "" },
  pushLoginVisible: { type: Function, required: true },
  procText: { type: Function, required: true },
  pushLogin: { type: Function, required: true },
  setAttackPause: { type: Function, required: true },
  reloadQr: { type: Function, required: true },
  showErr: { type: Function, required: true },
});
</script>

<template>
  <section class="attack-bar" aria-label="攻打号状态">
    <p v-if="pushLoginVisible(proc)">
      <button type="button" class="login" @click="pushLogin().catch((e) => showErr(e.message, 'bar'))">登录</button>
    </p>
    <p class="proc">
      <span class="proc-text">攻打 QQ {{ proc && proc.qq ? proc.qq : "还没绑定" }}：{{ procText(proc) }}<template v-if="proc && proc.online && proc.seen_at && !proc.paused"> · {{ proc.seen_at }}</template></span>
      <button v-if="proc && proc.online && proc.paused && proc.phase !== 'login' && proc.keepalive" type="button" class="ghost" @click="setAttackPause(false).catch((e) => showErr(e.message, 'bar'))">继续</button>
      <button v-else-if="proc && proc.online && proc.keepalive" type="button" class="ghost" @click="setAttackPause(true).catch((e) => showErr(e.message, 'bar'))">暂停</button>
    </p>
    <img v-if="qrSrc" class="qr" :src="qrSrc" alt="攻打号登录二维码" @error="reloadQr" />
    <p v-if="err && errAt === 'bar'" class="err">{{ err }}</p>
  </section>
</template>
