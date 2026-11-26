#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grayroll-06 并发双请求脚本 kill_cascade_override_06。

两路会话（默认 admin 与 writer）经 Barrier 同时 POST /api/kill 抢同一开关环境的
熔断锁 flag-kill:{flag_id}:{env_id}，核销：
  1) 恰好一路 200，另一路 409（禁止无锁双成）；
  2) 败方零正式改：kill_events 仅 +1；
  3) 赢家同事务级联停用该 flag+env 全部活跃覆盖；
  4) 求值面全关且 override_applied=false；
  5) 历史 kill 与 override_off 共用同一 trace_id（双痕同源）；
  6) /api/locks/contention 留争用痕。

用法：
  python scripts/kill_cascade_override_06.py                 # 自建临时单文件库 + 本机 uvicorn
  python scripts/kill_cascade_override_06.py --base-url http://127.0.0.1:8000
  GRAYROLL_BASE_URL=http://127.0.0.1:11816 scripts/kill_cascade_override_06

退出码 0 表示全部核销通过，1 表示发现无锁双成/未级联/假绿。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def _request(method: str, url: str, token: str | None = None, payload=None, timeout: float = 20.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed


def login(base: str, username: str, password: str) -> str:
    status, body = _request(
        "POST", f"{base}/api/login", payload={"username": username, "password": password}
    )
    if status != 200:
        raise SystemExit(f"登录失败 {username}: {status} {body}")
    return body["token"]


def start_self_hosted() -> tuple[str, object]:
    """起一个用临时单文件库的本机 uvicorn，返回 base_url 与 server（用于收尾）。"""
    sys.path.insert(0, str(BACKEND))
    import tempfile

    import db

    tmp_dir = Path(tempfile.mkdtemp(prefix="kill_cascade_override_06_"))
    db.DB_PATH = tmp_dir / "grayroll06_kill.db"
    db.init_db()

    import uvicorn
    from app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=__import__("os").environ.get("GRAYROLL_BASE_URL", ""))
    ap.add_argument("--flag-id", type=int, default=1)
    ap.add_argument("--env-id", type=int, default=3)
    ap.add_argument("--user-a", default="admin")
    ap.add_argument("--pass-a", default="admin123")
    ap.add_argument("--user-b", default="writer")
    ap.add_argument("--pass-b", default="writer123")
    args = ap.parse_args()

    server = None
    if args.base_url:
        base = args.base_url.rstrip("/")
        own_server = False
    else:
        # 自建服务需要 fastapi/uvicorn；当前解释器缺失时，优先重-exec 到仓库 .venv。
        try:
            import fastapi  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            venv_py = ROOT / ".venv" / "bin" / "python"
            if venv_py.exists() and str(venv_py) != sys.executable:
                import os

                os.execv(str(venv_py), [str(venv_py)] + sys.argv)
            raise SystemExit(
                "自建服务需要 fastapi/uvicorn，请先 pip install -r requirements.txt，"
                "或用 --base-url 指向已起服务。"
            )
        base, server = start_self_hosted()
        own_server = True
    print(f"[kill_cascade_override_06] target={base} flag={args.flag_id} env={args.env_id}")

    failures: list[str] = []
    try:
        tok_a = login(base, args.user_a, args.pass_a)
        tok_b = login(base, args.user_b, args.pass_b)
        ha, hb = tok_a, tok_b

        # 事前账：该 pair 的正式熔断数、活跃覆盖
        _, kills_before = _request("GET", f"{base}/api/kill", ha)
        before_events = [
            k for k in kills_before.get("official", [])
            if k["flag_id"] == args.flag_id and k["env_id"] == args.env_id
        ]
        _, ovs_before = _request("GET", f"{base}/api/overrides", ha)
        pair_ovs_before = [
            o for o in ovs_before.get("items", [])
            if o["flag_id"] == args.flag_id and o["env_id"] == args.env_id
        ]
        active_before = [o for o in pair_ovs_before if o["active"]]
        print(f"事前：kill_events={len(before_events)}，pair 覆盖={len(pair_ovs_before)}，活跃={len(active_before)}")

        barrier = threading.Barrier(2)
        outcomes: dict[str, tuple[int, dict]] = {}
        olock = threading.Lock()

        def fire(name: str, token: str):
            body = {
                "flag_id": args.flag_id,
                "env_id": args.env_id,
                "reason": f"kill_cascade_override_06 双会话并发抢修 {name}",
                "use_shadow": False,
            }
            barrier.wait()
            status, resp = _request("POST", f"{base}/api/kill", token, body)
            with olock:
                outcomes[name] = (status, resp)

        ta = threading.Thread(target=fire, args=(args.user_a, ha))
        tb = threading.Thread(target=fire, args=(args.user_b, hb))
        t0 = time.time()
        ta.start(); tb.start(); ta.join(30); tb.join(30)
        dt = (time.time() - t0) * 1000

        (sa, ra), (sb, rb) = outcomes[args.user_a], outcomes[args.user_b]
        codes = sorted([sa, sb])
        print(f"并发结果（{dt:.0f}ms）：{args.user_a}={sa} {ra if sa != 200 else {k: ra.get(k) for k in ('mode','id','trace_id','cascaded_overrides')}}")
        print(f"          {args.user_b}={sb} {rb if sb != 200 else {k: rb.get(k) for k in ('mode','id','trace_id','cascaded_overrides')}}")

        print("\n===== 争用核销 =====")
        if codes != [200, 409]:
            failures.append(f"无锁双成/异常：状态码 {codes}（应为 [200,409]），响应 {outcomes}")
        else:
            print("OK 恰好一路 200，败方 409")

        winner_resp = ra if sa == 200 else rb
        trace_id = winner_resp.get("trace_id", "")
        cascaded_n = winner_resp.get("cascaded_overrides")

        _, kills_after = _request("GET", f"{base}/api/kill", ha)
        after_events = [
            k for k in kills_after.get("official", [])
            if k["flag_id"] == args.flag_id and k["env_id"] == args.env_id
        ]
        delta = len(after_events) - len(before_events)
        if delta != 1:
            failures.append(f"败方写了正式账：kill_events 增量={delta}（应为 1）")
        else:
            print("OK 败方零正式改，kill_events 仅 +1")

        _, marks = _request("GET", f"{base}/api/locks/contention", ha)
        key = f"flag-kill:{args.flag_id}:{args.env_id}"
        pair_marks = [m for m in marks.get("items", []) if m["lock_key"] == key]
        if not pair_marks:
            failures.append("缺少 contention_marks 争用痕")
        else:
            m0 = pair_marks[0]
            print(f"OK 争用痕：loser={m0['loser']} winner={m0['winner']} key={key}")

        print("\n===== 级联停用 / 求值全关 / 双痕同源 =====")
        _, ovs_after = _request("GET", f"{base}/api/overrides", ha)
        pair_after = [
            o for o in ovs_after.get("items", [])
            if o["flag_id"] == args.flag_id and o["env_id"] == args.env_id
        ]
        still_active = [o for o in pair_after if o["active"]]
        if still_active:
            failures.append(f"熔断后仍有活跃覆盖：{[(o['id'], o['subject_key'], o['effect']) for o in still_active]}")
        else:
            print(f"OK pair 内 {len(pair_after)} 条覆盖全部停用（响应 cascaded_overrides={cascaded_n}）")

        # 求值面：pair 内每条覆盖的主体都必须求值关闭且 override_applied=False
        for o in pair_after:
            st, ev = _request(
                "POST", f"{base}/api/evaluate", ha,
                {"flag_key": o["flag_key"], "env_code": o["env_code"], "subject_key": o["subject_key"]},
            )
            if st != 200 or ev.get("enabled") is not False or ev.get("override_applied") is not False:
                failures.append(f"求值面仍被覆盖撬开/假绿：{o['subject_key']} -> {st} {ev}")
        if pair_after and not any("求值面" in x for x in failures):
            print(f"OK {len(pair_after)} 个覆盖主体求值全关，override_applied=false")

        _, hist = _request(
            "GET",
            f"{base}/api/history?flag_id={args.flag_id}&env_id={args.env_id}&limit=50",
            ha,
        )
        items = hist.get("items", [])
        kill_traces = {h.get("trace_id") for h in items if h["action"] in ("kill", "kill_promote") and h.get("trace_id")}
        off_traces = {h.get("trace_id") for h in items if h["action"] == "override_off" and h.get("trace_id")}
        if not trace_id:
            failures.append("赢家响应缺少 trace_id")
        elif trace_id not in kill_traces:
            failures.append("历史缺少本次 kill 同源 trace")
        if active_before:
            if trace_id not in off_traces:
                failures.append("历史缺少与 kill 同 trace_id 的 override_off 痕（双痕不同源）")
            else:
                same = [h for h in items if h.get("trace_id") == trace_id]
                print(f"OK 历史双痕同源 trace_id={trace_id}，同 trace {len(same)} 条（kill + override_off）")
        else:
            print("（外部目标事前无活跃覆盖，跳过双痕数量核销；自建库默认夹具会有 1 条）")

    finally:
        if own_server and server is not None:
            server.should_exit = True

    print("\n===== 结论 =====")
    if failures:
        for f in failures:
            print("FAIL -", f)
        print(f"kill_cascade_override_06 核销未通过：{len(failures)} 项")
        return 1
    print("PASS - 一成一争、败方零正式改、覆盖全停、求值全关、双痕同源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
