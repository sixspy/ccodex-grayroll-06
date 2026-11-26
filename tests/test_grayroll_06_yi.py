# -*- coding: utf-8 -*-
"""grayroll-06 失败测「乙」——求值服务这一面的假绿。

坑点：原实现求值时先看 flags.status=='killed' 直接返回 enabled=False，
表面“熔断后求值已关”，其实覆盖行依旧 active=1、force_on 上膛。
一旦开关经 /api/restore（或发布/爬坡）回到 active，未被级联停用的
force_on 覆盖立刻把求值重新撬开（假绿穿帮）。

本文件在未修复代码上必须见红：
- 熔断后求值响应没有 override_active=False / override_applied=False 的同源事实；
- 熔断 -> 恢复 active 后，u-9999 被残留 force_on 覆盖撬开，enabled=True。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import db

    test_db = tmp_path / "grayroll06_yi.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    from app import app

    with TestClient(app) as c:
        token = c.post(
            "/api/login", json={"username": "admin", "password": "admin123"}
        ).json()["token"]
        yield c, {"Authorization": f"Bearer {token}"}, db


def _kill(c, h):
    return c.post(
        "/api/kill",
        headers=h,
        json={
            "flag_id": 1,
            "env_id": 3,
            "reason": "grayroll06 乙：熔断级联覆盖求值复验",
            "use_shadow": False,
        },
    )


def _eval(c, h, subject="u-9999"):
    return c.post(
        "/api/evaluate",
        headers=h,
        json={"flag_key": "checkout.new_pay", "env_code": "prod", "subject_key": subject},
    )


def test_yi_immediate_eval_all_off_after_kill(client):
    """熔断当下，带 force_on 覆盖的主体也必须求值全关。"""
    c, h, _ = client
    assert _eval(c, h).json()["enabled"] is True  # 夹具前置：覆盖能撬开

    r = _kill(c, h)
    assert r.status_code == 200, r.text

    ev = _eval(c, h).json()
    assert ev["enabled"] is False
    assert not str(ev["reason"]).startswith("override:"), (
        "求值不得再走活跃覆盖分支",
        ev,
    )


def test_yi_evaluate_reports_cascaded_override_inactive(client):
    """求值服务必须给出覆盖已不活跃、未生效的同源事实，而不是只靠全局 killed 遮挡。"""
    c, h, _ = client
    assert _kill(c, h).status_code == 200

    ev = _eval(c, h).json()
    # 新增的求值侧事实：该主体那条覆盖已随熔断停用、本次求值未采用任何覆盖
    assert ev.get("override_active") is False, ev
    assert ev.get("override_applied") is False, ev
    ov = ev.get("override")
    assert ov and ov["active"] is False and ov["effect"] == "force_on"


def test_yi_un_cascaded_override_pries_open_after_reactivation(client):
    """真红：去掉全局 killed 遮挡（恢复 active）后，未级联的 force_on 会把求值撬开。

    修复后覆盖在熔断同事务被停用，恢复不会重新上膛，求值仍全关（落到 0% 正式规则）。
    """
    c, h, db = client
    assert _kill(c, h).status_code == 200

    rs = c.post(
        "/api/restore",
        headers=h,
        json={
            "flag_id": 1,
            "env_id": 3,
            "kind": "percent",
            "percent": 0,
            "note": "grayroll06 恢复后复验不得被覆盖撬开",
        },
    )
    assert rs.status_code == 200, rs.text
    assert rs.json()["status"] == "active"

    # 恢复动作不得替熔断把已级联停用的覆盖重新激活
    with db.connect() as conn:
        active = conn.execute(
            "SELECT active FROM overrides WHERE id=1"
        ).fetchone()["active"]
    assert active == 0

    ev = _eval(c, h).json()
    assert ev["enabled"] is False, f"求值被残留覆盖撬开（假绿穿帮）: {ev}"
    assert not str(ev["reason"]).startswith("override:"), ev
