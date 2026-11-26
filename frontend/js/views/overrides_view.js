import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const OverridesView = {
  setup() {
    const items = ref([]);
    const form = ref({ flag_id: 1, env_id: 3, subject_key: "u-7777", effect: "force_on", note: "客服个案" });
    const err = ref(""); const msg = ref(""); const busyId = ref(null);
    async function reload() { items.value = (await api("/api/overrides")).items || []; }
    async function submit() {
      err.value = ""; msg.value = "";
      try { await api("/api/overrides", { method: "POST", body: JSON.stringify(form.value) }); msg.value = "覆盖已保存（同键会重新激活）"; await reload(); }
      catch (e) { err.value = e.message || String(e); }
    }
    async function disable(it) {
      err.value = ""; msg.value = ""; busyId.value = it.id;
      try { await api(`/api/overrides/${it.id}/disable`, { method: "POST" }); msg.value = `已手动停用 ${it.subject_key}`; await reload(); }
      catch (e) { err.value = e.message || String(e); }
      finally { busyId.value = null; }
    }
    function stateText(it) {
      if (it.active) return "生效中";
      if (it.stop_reason === "kill_cascade") return "熔断级联停用";
      return "已停用";
    }
    onMounted(reload);
    return { items, form, err, msg, busyId, submit, disable, reload, stateText };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>单点覆盖</h2>
      <p class="hint">强制开/关优先于灰度比例；开关环境正式熔断时，同环境全部活跃覆盖会在同一事务被级联停用。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>主体键</label><input v-model="form.subject_key" /></div>
      <div class="field"><label>效果</label>
        <select v-model="form.effect"><option value="force_on">强制开</option><option value="force_off">强制关</option></select>
      </div>
      <button class="btn" @click="submit">保存覆盖</button>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>覆盖列表</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>主体</th><th>效果</th><th>状态</th><th>停用原因</th><th></th></tr></thead>
      <tbody>
        <tr v-for="it in items" :key="it.id" :class="{ 'row-cascaded': !it.active && it.stop_reason==='kill_cascade' }">
          <td>{{ it.flag_key }}</td><td>{{ it.env_code }}</td><td>{{ it.subject_key }}</td>
          <td><span :class="it.effect==='force_on' ? 'badge on' : 'badge off'">{{ it.effect }}</span></td>
          <td>
            <span v-if="it.active" class="badge on">生效中</span>
            <span v-else-if="it.stop_reason==='kill_cascade'" class="badge cascade">熔断级联停用</span>
            <span v-else class="badge off">已停用</span>
          </td>
          <td class="muted">{{ stateText(it) }}</td>
          <td>
            <button v-if="it.active" class="btn secondary" :disabled="busyId===it.id" @click="disable(it)">停用</button>
            <span v-else class="muted">—</span>
          </td>
        </tr>
      </tbody></table>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reload">刷新</button>
    </div>
  </div>`
};
