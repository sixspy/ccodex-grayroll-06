import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const KillView = {
  setup() {
    const official = ref([]); const shadows = ref([]); const marks = ref([]);
    const form = ref({ flag_id: 1, env_id: 3, reason: "生产新支付链路异常，紧急熔断并级联停用覆盖", use_shadow: false });
    const err = ref(""); const msg = ref("");
    async function reload() {
      const res = await api("/api/kill");
      official.value = res.official || []; shadows.value = res.shadows || [];
      try { marks.value = (await api("/api/locks/contention?limit=20")).items || []; } catch { marks.value = []; }
    }
    async function submit() {
      err.value = ""; msg.value = "";
      try {
        const res = await api("/api/kill", { method: "POST", body: JSON.stringify(form.value) });
        if (res.mode === "official") {
          msg.value = `正式熔断已提交（v${res.version}）：同事务级联停用覆盖 ${res.cascaded_overrides} 条；同源痕 ${res.trace_id}`;
        } else {
          msg.value = `影子紧急草案已写入 #${res.id}：正式区零改，升正时才级联停用覆盖`;
        }
        await reload();
      } catch (e) { err.value = e.message || String(e); await reload(); }
    }
    async function promote(id) {
      err.value = ""; msg.value = "";
      try {
        const res = await api(`/api/kill/shadow/${id}/promote`, { method: "POST", body: "{}" });
        msg.value = `影子已原子升正（v${res.version}），级联停用覆盖 ${res.cascaded_overrides} 条；同源痕 ${res.trace_id}`;
        await reload();
      }
      catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { official, shadows, marks, form, err, msg, submit, promote, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>紧急熔断</h2>
      <p class="hint">甲路径（已封板）：正式关闭与该环境全部活跃覆盖停用在同一事务跨表双写，失败整笔回滚；两路会话抢同一把熔断锁时败方 409 且零正式改。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>原因</label><textarea v-model="form.reason" rows="3"></textarea></div>
      <div class="field"><label><input type="checkbox" v-model="form.use_shadow" /> 影子紧急草案（正式区零改，升正才级联）</label></div>
      <button class="btn danger" @click="submit">执行熔断</button>
      <p class="muted" v-if="msg" style="word-break:break-all">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>熔断记录</h2>
      <table><thead><tr><th>ID</th><th>开关</th><th>环境</th><th>原因</th></tr></thead>
      <tbody><tr v-for="k in official" :key="k.id"><td>{{ k.id }}</td><td>{{ k.flag_id }}</td><td>{{ k.env_id }}</td><td>{{ k.reason }}</td></tr></tbody></table>
      <h2 style="margin-top:1rem">影子熔断</h2>
      <table><thead><tr><th>ID</th><th>状态</th><th></th></tr></thead>
      <tbody>
        <tr v-for="s in shadows" :key="s.id">
          <td>{{ s.id }}</td><td>{{ s.status }}</td>
          <td><button class="btn secondary" v-if="s.status==='open'" @click="promote(s.id)">升正并级联</button></td>
        </tr>
      </tbody></table>
      <h2 style="margin-top:1rem">争用痕（败方只留痕）</h2>
      <table><thead><tr><th>锁键</th><th>败方</th><th>赢家</th></tr></thead>
      <tbody>
        <tr v-for="m in marks" :key="m.id"><td>{{ m.lock_key }}</td><td>{{ m.loser }}</td><td>{{ m.winner }}</td></tr>
        <tr v-if="!marks.length"><td colspan="3" class="muted">暂无争用</td></tr>
      </tbody></table>
    </div>
  </div>`
};
