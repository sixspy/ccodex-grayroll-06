# -*- coding: utf-8 -*-
"""grayroll-06 失败测「甲」——写口正式账实。

验收口径（甲路径，已在说明中拍板封死）：
正式熔断必须在【同一事务】内写正式关闭（flags.status=killed + 官方零比例规则 +
kill_events）并【批量停用该 flag+env 下全部活跃 overrides】，同时落下
kill 与 override_off 两条同源历史痕；任一步失败整笔回滚，不得留下半笔正式账。

本文件在未修复代码上必须见红：
- 熔断后 overrides.active 仍为 1（级联缺失）；
- 响应没有 cascaded_overrides/trace_id 正式账实；
- 历史没有与 kill 同源的 override_off 痕；
- 用触发器令 overrides 写入失败时，旧代码根本不写 overrides，熔断仍成功，
  证明不存在“同事务跨表双写，否则回滚”。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    import db

    test_db = tmp_path / "grayroll06_jia.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    from app import app

    with TestClient(app) as client:
        token = client.post(
            "/api/login", json={"username": "admin", "password": "admin123"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        yield {"client": client, "headers": headers, "db": db, "app": app}


def _kill(client, headers, **over):
    body = {
        "flag_id": 1,
        "env_id": 3,
        "reason": "grayroll06 甲：生产新支付紧急熔断",
        "use_shadow": False,
    }
    body.update(over)
    return client.post("/api/kill", headers=headers, json=body)


def test_jia_cascade_deactivates_every_active_override(ctx):
    """熔断后，该开关环境下全部活跃覆盖必须被同事务停用。"""
    c, h = ctx["client"], ctx["headers"]
    db = ctx["db"]

    # 夹具前置：checkout.new_pay(1)/prod(3) 存在活跃 force_on 覆盖 id=1
    with db.connect() as conn:
        before = conn.execute(
            "SELECT id, active FROM overrides WHERE flag_id=1 AND env_id=3 AND active=1"
        ).fetchall()
        assert [r["id"] for r in before] == [1]

    r = _kill(c, h)
    assert r.status_code == 200, r.text
    payload = r.json()

    # 写口必须回报级联账实（数量与同源 trace）
    assert payload.get("mode") == "official"
    assert payload.get("cascaded_overrides") == 1
    assert payload.get("trace_id"), "正式熔断必须带同源 trace_id"

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, active, stop_reason FROM overrides WHERE flag_id=1 AND env_id=3"
        ).fetchall()
    assert rows, "夹具覆盖行应仍在（软停用，不删除）"
    for row in rows:
        assert row["active"] == 0, f"覆盖 {row['id']} 熔断后仍活跃"
        assert row["stop_reason"] == "kill_cascade"

    listed = c.get("/api/overrides", headers=h).json()["items"]
    target = [o for o in listed if o["flag_id"] == 1 and o["env_id"] == 3]
    assert target and all(o["active"] in (0, False) for o in target)


def test_jia_official_close_rows_are_stopped(ctx):
    """正式账：flags=killed、官方规则零比例、kill_events 落一笔。"""
    c, h = ctx["client"], ctx["headers"]
    r = _kill(c, h)
    assert r.status_code == 200, r.text

    flag = c.get("/api/flags/1", headers=h).json()
    assert flag["status"] == "killed"

    rules = c.get("/api/rules", headers=h).json()["official"]
    pair = [x for x in rules if x["flag_id"] == 1 and x["env_id"] == 3]
    assert pair and all(int(x["percent"]) == 0 for x in pair)

    kills = c.get("/api/kill", headers=h).json()["official"]
    mine = [k for k in kills if k["flag_id"] == 1 and k["env_id"] == 3]
    assert len(mine) == 1


def test_jia_history_double_trace_same_source(ctx):
    """历史双痕同源：kill 与 override_off 同 flag/env、同 trace_id、同操作者。"""
    c, h = ctx["client"], ctx["headers"]
    r = _kill(c, h)
    assert r.status_code == 200, r.text
    trace = r.json().get("trace_id")
    assert trace

    items = c.get("/api/history", headers=h).json()["items"]
    pair = [x for x in items if x.get("flag_id") == 1 and x.get("env_id") == 3]
    kills = [x for x in pair if x["action"] == "kill"]
    offs = [x for x in pair if x["action"] == "override_off"]

    assert len(kills) == 1, "必须有一条正式熔断痕"
    assert len(offs) >= 1, "必须有覆盖停用痕（历史页不能只见熔断）"
    assert any("级联" in x["detail"] or "cascade" in x["detail"].lower() for x in offs)

    kill_row, off_row = kills[0], offs[0]
    assert kill_row.get("trace_id"), "kill 痕必须带同源 trace_id"
    assert kill_row["trace_id"] == off_row["trace_id"] == trace
    assert kill_row["actor"] == off_row["actor"] == "admin"


def test_jia_cascade_failure_rolls_back_entire_kill(ctx):
    """跨表双写同事务：覆盖停用写不进去时，整笔熔断回滚，正式账零改。"""
    db, app = ctx["db"], ctx["app"]

    # 注入：任何对 overrides 的 UPDATE 都 ABORT —— 级联停用必然失败
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER grayroll06_fail_override_update
            BEFORE UPDATE ON overrides
            BEGIN
                SELECT RAISE(ABORT, 'grayroll06 injected cascade failure');
            END;
            """
        )
        conn.commit()

    with TestClient(app, raise_server_exceptions=False) as c2:
        token = c2.post(
            "/api/login", json={"username": "admin", "password": "admin123"}
        ).json()["token"]
        h2 = {"Authorization": f"Bearer {token}"}
        r = _kill(c2, h2)
        assert r.status_code >= 500, (
            "级联停用失败时熔断写口必须整体失败，而不是半笔成功",
            r.status_code,
            r.text,
        )

    with db.connect() as conn:
        kill_n = conn.execute(
            "SELECT COUNT(*) c FROM kill_events WHERE flag_id=1 AND env_id=3"
        ).fetchone()["c"]
        status = conn.execute("SELECT status FROM flags WHERE id=1").fetchone()["status"]
        ov_active = conn.execute(
            "SELECT active FROM overrides WHERE id=1"
        ).fetchone()["active"]
        rule = conn.execute(
            "SELECT percent, version FROM flag_rules WHERE flag_id=1 AND env_id=3 AND official=1"
        ).fetchone()

    assert kill_n == 0, "回滚后不得残留 kill_events"
    assert status == "active", "回滚后 flags.status 必须仍是 active"
    assert ov_active == 1, "回滚后覆盖必须仍活跃（不得半笔停用）"
    assert rule["percent"] == 20 and rule["version"] == 2, "正式规则不得被半笔改写"
