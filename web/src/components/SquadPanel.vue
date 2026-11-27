<script setup>
import { ref, watch } from "vue";

const props = defineProps({
  squad: { type: Object, default: null },
  lead: { type: Boolean, default: false },
  idle: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  renameSquad: { type: Function, default: null },
  inviteMember: { type: Function, default: null },
  kickMember: { type: Function, default: null },
  setTask: { type: Function, default: null },
  answer: { type: Function, default: null },
  showErr: { type: Function, default: null },
});

const squadName = ref("国战编队");
const inviteWho = ref("");
watch(() => props.squad && props.squad.team_id, () => {
  squadName.value = props.squad && props.squad.name ? props.squad.name : "国战编队";
}, { immediate: true });

async function run(job) {
  try {
    await job();
  } catch (e) {
    if (props.showErr) props.showErr(e.message);
  }
}

function rename() {
  if (!props.renameSquad) return;
  run(() => props.renameSquad(squadName.value));
}

function sendInvite() {
  if (!props.inviteMember) return;
  const username = inviteWho.value.trim();
  run(async () => {
    await props.inviteMember(username);
    inviteWho.value = "";
  });
}

function kick(member) {
  if (!props.kickMember) return;
  run(() => props.kickMember(member));
}

function reply(on) {
  if (!props.answer) return;
  run(() => props.answer(on));
}

function task(state) {
  if (!props.setTask) return;
  run(() => props.setTask(state));
}
</script>

<template>
  <p v-if="!squad && idle" class="muted squad-idle">还没有编队。同区同国的指挥者邀请这个账号之后，会显示在这里。</p>
  <section v-else-if="squad" class="squad" aria-label="编队">
    <div class="squad-head">
      <div>
        <h3 class="squad-title">
          <template v-if="squad.self">你在指挥「{{ squad.name }}」</template>
          <template v-else-if="lead">编队「{{ squad.name }}」</template>
          <template v-else-if="squad.state === 'wait'">{{ squad.organizer }} 邀请加入「{{ squad.name }}」</template>
          <template v-else>所在编队「{{ squad.name }}」</template>
        </h3>
        <p class="muted squad-meta">
          <template v-if="!squad.self">指挥者 {{ squad.organizer }} · </template>{{ squad.region }}区 {{ squad.country_name }} · 任务{{ squad.task_text || "未开始" }}
        </p>
      </div>
      <div v-if="(lead || squad.self) && setTask" class="squad-head-actions">
        <button v-if="squad.task !== 'run'" type="button" :disabled="busy" @click="task('run')">{{ squad.task === "pause" ? "继续" : "开始" }}</button>
        <button v-if="squad.task === 'run'" type="button" class="ghost" :disabled="busy" @click="task('pause')">暂停</button>
        <button v-if="squad.task === 'run' || squad.task === 'pause'" type="button" class="ghost" :disabled="busy" @click="task('idle')">结束</button>
      </div>
      <div v-if="!lead && !squad.self && answer" class="squad-head-actions">
        <template v-if="squad.state === 'wait'">
          <button type="button" :disabled="busy" @click="reply(true)">接受</button>
          <button type="button" class="ghost" :disabled="busy" @click="reply(false)">拒绝</button>
        </template>
        <button v-else type="button" class="ghost" :disabled="busy" @click="reply(false)">退出</button>
      </div>
    </div>
    <p v-if="!lead && squad.state === 'in' && (squad.task === 'run' || squad.task === 'pause')" class="muted">编队任务还没结束，这个号自己的攻打和日常先停着。退出，或者指挥者结束任务之后，再继续自己的任务。</p>
    <p v-else-if="!lead && squad.state !== 'wait'" class="muted">已加入。指挥者开始任务之后，这个号会跟着做。任务进行或暂停时，自己的攻打和日常先停。</p>
    <form v-if="lead && renameSquad" class="lock-row" @submit.prevent="rename()">
      <label>编队名称
        <input v-model="squadName" maxlength="16" />
      </label>
      <button type="submit" :disabled="busy">保存</button>
    </form>
    <form v-if="lead && inviteMember" class="lock-row" @submit.prevent="sendInvite()">
      <label>对方账号
        <input v-model="inviteWho" required placeholder="登录账号" />
      </label>
      <button type="submit" :disabled="busy">邀请</button>
    </form>
    <p v-if="lead" class="muted">同区同国家才能邀请。还没从游戏读到国家的可以先邀请。</p>
    <div class="squad-roster">
      <div v-for="m in squad.roster || []" :key="m.id" class="squad-member">
        <span class="who">{{ m.username }}</span>
        <span class="muted">{{ m.organizer ? "指挥者 · " : "" }}{{ m.status }} · {{ m.presence || (m.online ? "在线" : "离线") }}</span>
        <button v-if="lead && !m.organizer && m.state !== 'out'" type="button" class="ghost" :disabled="busy" @click="kick(m)">踢出</button>
      </div>
    </div>
  </section>
</template>
