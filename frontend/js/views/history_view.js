import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const HistoryView = {
  setup() {
    const items = ref([]); const err = ref("");
    async function reload() {
      try { items.value = (await api("/api/history")).items || []; }
      catch (e) { err.value = e.message || String(e); }
    }
    function actionClass(a) {
      if (a === "kill" || a === "kill_promote") return "badge kill";
      if (a === "override_off") return "badge cascade";
      return "pill";
    }
    onMounted(reload);
    return { items, err, reload, actionClass };
  },
  template: `
  <div class="panel">
    <h2>变更历史 <span class="muted">· 熔断痕与覆盖停用痕按 trace 同源</span></h2>
    <div class="err" v-if="err">{{ err }}</div>
    <table>
      <thead><tr><th>动作</th><th>开关</th><th>环境</th><th>详情</th><th>同源 trace</th><th>操作者</th></tr></thead>
      <tbody>
        <tr v-for="it in items" :key="it.id">
          <td><span :class="actionClass(it.action)">{{ it.action }}</span></td>
          <td>{{ it.flag_key || '-' }}</td><td>{{ it.env_code || '-' }}</td>
          <td>{{ it.detail }}</td>
          <td><span v-if="it.trace_id" class="trace-chip">{{ it.trace_id }}</span><span v-else class="muted">-</span></td>
          <td>{{ it.actor }}</td>
        </tr>
      </tbody>
    </table>
    <button class="btn secondary" style="margin-top:0.7rem" @click="reload">刷新</button>
  </div>`
};
