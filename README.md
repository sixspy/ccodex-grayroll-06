# 灰度发布台（grayroll）

**所属领域：应用开发 · 子方向：全栈开发**

全栈产品：功能开关登记 → 环境绑定 → 人群圈选 → 灰度规则 → 发布生效 → 紧急熔断 → 单点覆盖 → 变更历史 → 在线求值。

## 骨架一句话

功能开关按环境配置灰度比例与人群，发布须版本落库才能生效，紧急熔断与变更历史同源。

## H3 切口候选（写路径互斥 · 须择一）

1. **发布生效**：同事务写入正式生效配置并升版本锁旧版，或只写影子发布草案、升正时原子落正式区（正式区升正前零改）。
2. **紧急熔断**：同事务立即写正式关闭并记变更，或影子紧急草案确认后原子熔断。
3. **人群绑定**：同事务写正式人群规则，或影子绑定草案升正。

正确性口径只能走甲乙中的一种且两边不得混用。

## grayroll-06 封板（熔断级联停用覆盖）

**命运取舍：拍板走「甲路径」并当班封死**——正式熔断在同一事务跨表双写“正式关闭 +
批量停用该 flag+env 全部活跃覆盖”，并落 `kill`/`override_off` 同 `trace_id` 双痕，
任一失败整笔回滚；禁止第三路折中、禁止甲乙口径混用。影子紧急草案仍保持正式区零改，
仅在原子升正事务复用同一甲内核（含级联）。**争用核销**：两路会话抢同一
`flag-kill:{fid}:{eid}` 锁必须一成一 409，败方只写 `contention_marks`、零正式改。
完整口径、三红三绿证据与五页同源说明见 [`GRAYROLL_06.md`](./GRAYROLL_06.md)。

- 三红分文件：`tests/test_grayroll_06_jia.py`、`tests/test_grayroll_06_yi.py`、`tests/test_grayroll_06_zheng.py`
- 并发双请求脚本：`scripts/kill_cascade_override_06`（或 `.py`，支持 `--base-url`）
- 验收：`python -m pytest -q` 全绿；`scripts/kill_cascade_override_06` 退出 0


## 一键起环境

```bash
docker compose up -d --build
```

浏览器：http://localhost:11720

- `admin` / `admin123`（管理员）
- `writer` / `writer123`（发布员）
- `viewer` / `viewer123`（只读）

## 本地开发

```powershell
cd projects/17-grayroll
$env:PYTHONPATH="backend"
python -m uvicorn app:app --app-dir backend --reload --port 8020
python -m pytest
python scripts/count_loc.py
```

## 空桩

- 远程配置推送（`remote_push`）
- 实验平台桥接（`experiment_bridge`）

接口固定返回 501，题面可点名。
