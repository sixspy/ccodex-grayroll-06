# grayroll-06 封板说明：正式熔断同事务级联停用活跃覆盖

领域：应用开发 · 全栈开发。切口：紧急熔断写路径互斥 + 覆盖级联。
开关夹具：`checkout.new_pay`（flags.id=1）/ `prod`（environments.id=3），
存在活跃 `force_on` 覆盖 `u-9999`（overrides.id=1）。

## 一、命运取舍（甲 / 乙拍板 · 当班封死）

**本班拍板走「甲路径」，即刻封死，禁止第三路折中，也禁止甲、乙两套口径混用。**

- 甲路径（本班验收口径）：`POST /api/kill`（`use_shadow=false`，默认正式熔断）
  在**同一个 SQLite 事务 / 同一条连接**内完成跨表双写：
  1. `flag_rules` 写官方零比例关闭规则并升版本；
  2. `flags.status='killed'`；
  3. `kill_events` 落正式熔断事件；
  4. **批量 `UPDATE overrides SET active=0, stop_reason='kill_cascade'`，停用该 `flag_id+env_id` 下全部活跃覆盖**；
  5. `change_history` 落 `kill` 痕与逐条 `override_off` 痕，二者共用同一 `trace_id`（双痕同源）。

  以上任一步抛错：`conn.rollback()` **整笔回滚**，`kill_events / flags / flag_rules / overrides`
  不得留下半笔账；锁在 `finally` 释放。绝不再用旧实现里“异常时 `finally: commit()` 把半笔账提交”的写法。

- 乙路径的处置（不是混用口径）：仓库原有“影子紧急草案”能力保留——`use_shadow=true`
  阶段只写 `kill_shadows`，正式区（flags / rules / **overrides**）零改；在
  `/api/kill/shadow/{id}/promote` **原子升正的那一刻**，调用与甲路径完全相同的
  `_write_official_kill()` 同事务内核（含覆盖级联与同源双痕）。这是把甲口径贯彻到
  “正式化发生的那个事务”，不存在“只改角标 / 只做单点校验”的第三路。

封板结论：grayroll-06 的独验收以**正式熔断（甲）后的即时状态**为准——
熔断后覆盖全停、求值全关、历史双痕同源。任何想改走“只落影子、正式零改、以后再说”
的反复，都属于偏离已封取舍，必须记入下文「偏航记录」并拒写。

## 二、争用核销（两路会话同抢一个熔断锁 · 败方零正式改）

锁键：`flag-kill:{flag_id}:{env_id}`，与既有 `row_locks / contention_marks` 同源。

旧 `locks.acquire` 是“先 `SELECT` 看锁、再无条件 `UPSERT`”的 check-then-act：
并发两路都读到无锁，随后互相覆盖 holder，实测两路并发 `POST /api/kill` **双 200、
各写一笔 `kill_events`（无锁双成）**；4 线程原语争抢曾出现 12/12 轮全部“成功”。

核销后的锁两层：

1. 进程内按 lock_key 的**非阻塞互斥锁**：单 uvicorn 进程（compose 默认单 worker）内，
   同键并发只有一路 `acquire(blocking=False)` 成功；败方**不进任何业务写**，
   只用自己的连接写一条 `contention_marks(lock_key, loser, winner)` 并返回 **409**。
2. 数据库 `row_locks` 的**原子条件占位**（清过期锁 + `INSERT ... ON CONFLICT ... WHERE holder=self`
   + 回读裁决），保证锁可观测、TTL 可过期，并作为多 worker / 多进程方向的防线。

败方命运：**零正式改**——不写 `kill_events`、不动 `flags / flag_rules / overrides`，
唯一写入是争用痕。赢家命运：完成第一节的同事务跨表双写。
`release()` 先提交删除锁行，再释放进程内互斥。

核销手段：

- `tests/test_grayroll_06_zheng.py`：原语级 4 线程 ×3 轮必须恰好 1 赢；
  真实 uvicorn + 两会话（admin/writer）并发双请求必须 `[200,409]`、
  `kill_events` 增量 1、存在对应 `contention_marks`。
- 并发双请求脚本 **`scripts/kill_cascade_override_06`**
  （等价 `scripts/kill_cascade_override_06.py`）：默认自建临时单文件库并起本机
  uvicorn，用 Barrier 同时打争用写口，依次核销“一成一争 / 败方零正式改 /
  覆盖全停 / 求值全关 / 双痕同源 / 争用痕”，全过退出 0。也可
  `--base-url http://127.0.0.1:8000` 打已起服务（按事前计数做增量核销）。

```bash
scripts/kill_cascade_override_06
scripts/kill_cascade_override_06 --base-url http://127.0.0.1:8000 --flag-id 1 --env-id 3
```

## 三、三红 → 三绿（测试源文件闸）

先写测试并在**未修复代码**上分别见红，再动服务：

| 文件 | 盯的服务/假绿面 | 见红事实（修复前实测） |
| --- | --- | --- |
| `tests/test_grayroll_06_jia.py` | 熔断写口 / 覆盖服务 / 正式账实 + 历史同源 | 熔断后 `overrides.active` 仍为 1；无 `trace_id/cascaded_overrides`；无 `override_off` 痕；注入 overrides 写失败时熔断仍 200（无整笔回滚）→ 3 failed |
| `tests/test_grayroll_06_yi.py` | 求值服务这一面的假绿 | 求值响应无 `override_active=False`；熔断→恢复 active 后，残留 `force_on` 把求值撬回 `enabled=True` → 2 failed |
| `tests/test_grayroll_06_zheng.py` | 锁 / 争用写口 | 原语争抢 4/4 赢；HTTP 双会话 `{admin:200, writer:200}`、`kill_events=2` → 2 failed |

修复后：

```bash
python -m pytest tests/test_grayroll_06_jia.py tests/test_grayroll_06_yi.py tests/test_grayroll_06_zheng.py -q
# 10 passed
python -m pytest -q
# 全量 20 passed（含既有 10 个用例，无回归）
```

## 四、五页同源 + 逐页打开

覆盖、求值、历史、总览、规则五页（外加熔断页）都从同一个 SQLite 单文件库经 API 取数，
级联一旦提交，五页同源可见：

- **覆盖页**：行状态区分 `生效中 / 已停用 / 熔断级联停用`（`stop_reason=kill_cascade`），
  活跃行可手动停用（复用既有 `/api/overrides/{id}/disable`）。
- **求值页**：裁决条显示 ON/OFF、`reason`、该主体覆盖 `active/stop_reason`，
  级联后显示“覆盖 force_on · 已熔断级联停用”，`override_applied=false`。
- **历史页**：动作徽标区分 `kill / kill_promote / override_off`，新增“同源 trace”列。
- **总览页**：新增“已熔断 / 活跃覆盖”统计卡，级联后活跃覆盖数下降（与
  `/api/dashboard` 的 `active_overrides` 同源）。
- **规则页**：被熔断写成 0% 的官方规则标“熔断关闭”。
- **熔断页**：默认定位 `checkout.new_pay/prod`；提交后回报级联条数与 trace；
  新增“争用痕（败方只留痕）”表；影子升正按钮标注“升正并级联”。

已用 jsdom + 仓库自带 `vue.global.prod.js` 真实挂载六页并对接活体后端：
六页全部渲染 grayroll-06 状态、Vue 0 报错；并发 4 连得到 `200,409,409,409`。

## 五、偏航记录

无。实现严格走第一节封死的甲路径；影子升正仅在“正式化事务”复用同一甲内核。
未改登录/导航形态，未换库（默认仍是单文件 `data/app.db`，仅新增幂等加列迁移；
`GRAYROLL_DB` 仅供测试/演练指向临时单文件）。后续若偏离已封取舍，在此登记并拒写。
