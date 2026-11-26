# -*- coding: utf-8 -*-
"""grayroll-06 失败测「争」(zheng)——两路会话抢同开关熔断锁，禁止无锁双成。

原 locks.acquire 是“先 SELECT 看有没有锁、再无条件 UPSERT”的 check-then-act，
并发下两路会话都读到无锁、随后互相覆盖 holder，于是双双 200、各写一笔
kill_events（无锁双成）。

本文件在未修复代码上必须见红：
- 原语级并发 acquire：N 路同时抢同一 key，多于 1 路“成功”；
- 真实 HTTP 双会话并发 POST /api/kill：双 200、kill_events=2。

修复后必须：恰好一路 200，另一路 409 且只写 contention_marks，
败方对正式账（kill_events / flags / rules / overrides）零改。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def srv():
    import tempfile

    import db

    old_db_path = db.DB_PATH
    tmp_dir = Path(tempfile.mkdtemp(prefix="grayroll06_zheng_"))
    db.DB_PATH = tmp_dir / "zheng.db"
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
    yield {"base": f"http://127.0.0.1:{port}", "db": db}

    server.should_exit = True
    thread.join(timeout=5)
    db.DB_PATH = old_db_path


def _login(base, username, password):
    import httpx

    r = httpx.post(
        f"{base}/api/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_zheng_acquire_primitive_exactly_one_winner(srv):
    """N 个线程同时 acquire 同一把锁：恰好一个赢家，其余 409，不得多赢。"""
    from fastapi import HTTPException

    from locks import acquire, release

    db = srv["db"]
    n = 4
    for round_idx in range(3):
        key = f"flag-kill:primitive-{round_idx}:3"
        start = threading.Barrier(n)
        outcomes = []
        out_lock = threading.Lock()

        def worker(i):
            conn = db.connect()
            start.wait()
            try:
                acquire(conn, key, f"holder-{round_idx}-{i}")
                with out_lock:
                    outcomes.append("win")
                # 赢家持锁一小段，确保其余线程都在锁被占期间尝试
                time.sleep(0.3)
                conn.commit()
                release(conn, key, f"holder-{round_idx}-{i}")
                conn.commit()
            except HTTPException as e:
                with out_lock:
                    outcomes.append(f"lose:{e.status_code}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            except Exception as e:  # pragma: no cover - 不应出现
                with out_lock:
                    outcomes.append(f"error:{type(e).__name__}")
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        wins = [o for o in outcomes if o == "win"]
        losses = [o for o in outcomes if o.startswith("lose:409")]
        assert len(outcomes) == n, outcomes
        assert len(wins) == 1, f"第 {round_idx} 轮出现无锁双成/多成: {outcomes}"
        assert len(losses) == n - 1, outcomes


def test_zheng_two_sessions_concurrent_kill_one_conflict(srv):
    """两路真实会话并发抢修同一 flag+env：一成一 409，败方零正式改。"""
    import httpx

    base, db = srv["base"], srv["db"]
    admin_tok = _login(base, "admin", "admin123")
    writer_tok = _login(base, "writer", "writer123")

    fid, eid = 1, 3
    barrier = threading.Barrier(2)
    results = {}
    res_lock = threading.Lock()

    def fire(name, token):
        headers = {"Authorization": f"Bearer {token}"}
        barrier.wait()
        try:
            r = httpx.post(
                f"{base}/api/kill",
                headers=headers,
                json={
                    "flag_id": fid,
                    "env_id": eid,
                    "reason": f"grayroll06 争用：{name} 同时抢修同开关",
                    "use_shadow": False,
                },
                timeout=20,
            )
            with res_lock:
                results[name] = r.status_code
        except Exception as e:  # pragma: no cover
            with res_lock:
                results[name] = f"ERR:{type(e).__name__}:{e}"

    t1 = threading.Thread(target=fire, args=("admin", admin_tok))
    t2 = threading.Thread(target=fire, args=("writer", writer_tok))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    codes = sorted(results.values(), key=lambda x: str(x))
    assert codes == [200, 409], f"必须恰好一成一冲突，实际 {results}"

    with db.connect() as conn:
        kill_n = conn.execute(
            "SELECT COUNT(*) c FROM kill_events WHERE flag_id=? AND env_id=?",
            (fid, eid),
        ).fetchone()["c"]
        mark_n = conn.execute(
            "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?",
            (f"flag-kill:{fid}:{eid}",),
        ).fetchone()["c"]
        active_ov = conn.execute(
            "SELECT COUNT(*) c FROM overrides WHERE flag_id=? AND env_id=? AND active=1",
            (fid, eid),
        ).fetchone()["c"]
        mark = conn.execute(
            "SELECT loser, winner FROM contention_marks WHERE lock_key=? ORDER BY id DESC LIMIT 1",
            (f"flag-kill:{fid}:{eid}",),
        ).fetchone()

    assert kill_n == 1, f"败方不得写正式 kill_events，实际 {kill_n} 笔"
    assert active_ov == 0, "赢家必须完成级联停用"
    assert mark_n >= 1, "败方必须只留下争用痕"
    assert mark and mark["loser"] in ("admin", "writer")
    assert mark["winner"] in ("admin", "writer", "concurrent-session")


def test_zheng_viewer_cannot_kill_still_403(srv):
    """只读账号即便参与也不得写（顺带守住角色口径）。"""
    import httpx

    base = srv["base"]
    tok = _login(base, "viewer", "viewer123")
    r = httpx.post(
        f"{base}/api/kill",
        headers={"Authorization": f"Bearer {tok}"},
        json={"flag_id": 2, "env_id": 2, "reason": "grayroll06 viewer 越权尝试"},
        timeout=10,
    )
    assert r.status_code == 403
