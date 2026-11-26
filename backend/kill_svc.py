"""紧急熔断。

grayroll-06 拍板口径＝甲路径（当班封死，禁止第三路折中）：
正式熔断在【同一事务】内完成跨表双写——
  1) flag_rules 写官方零比例关闭规则并升版本；
  2) flags.status='killed'；
  3) kill_events 落正式熔断事件；
  4) overrides 批量级联停用该 flag+env 下全部活跃覆盖；
  5) change_history 落 kill 与逐条 override_off，共用同一 trace_id（双痕同源）。
以上任一步失败整笔 rollback，绝不留半笔正式账。

影子紧急草案沿用既有形态：草案阶段正式区（flags/rules/overrides）零改；
其原子升正的那一刻执行与甲路径完全相同的同事务级联，不产生第二套正确性口径。
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from overrides_svc import cascade_for_kill
from policies import assert_not_killed_or_allow_kill, describe_path
from validators import require_int, require_str

router = APIRouter(prefix="/api/kill", tags=["kill"])


class KillIn(BaseModel):
    flag_id: int
    env_id: int
    reason: str
    use_shadow: bool = False


@router.get("")
def list_kills(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        official = [dict(r) for r in conn.execute("SELECT * FROM kill_events ORDER BY id DESC").fetchall()]
        shadows = [dict(r) for r in conn.execute("SELECT * FROM kill_shadows ORDER BY id DESC").fetchall()]
    return {"official": official, "shadows": shadows}


def _write_official_kill(
    conn,
    *,
    fid: int,
    eid: int,
    reason: str,
    actor: str,
    history_action: str = "kill",
    audit_action: str = "kill",
    audit_detail: str | None = None,
):
    """同事务跨表双写的正式熔断内核。不单独 commit，由调用方统一提交/回滚。"""
    now = time.time()
    old = conn.execute(
        "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
    ).fetchone()
    ver = (old["version"] + 1) if old else 1
    if old:
        conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
    conn.execute(
        "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
        "VALUES(?,?,?,?,NULL,?,1,?)",
        (fid, eid, "percent", 0, ver, now),
    )
    conn.execute("UPDATE flags SET status='killed' WHERE id=?", (fid,))
    cur = conn.execute(
        "INSERT INTO kill_events(flag_id, env_id, reason, official, created_at, created_by) "
        "VALUES(?,?,?,1,?,?)",
        (fid, eid, reason, now, actor),
    )
    kid = cur.lastrowid
    trace_id = f"kill-{kid}-{uuid.uuid4().hex[:10]}"

    write_audit(
        conn,
        actor,
        audit_action,
        "kill_event",
        kid,
        audit_detail if audit_detail is not None else describe_path("kill", False),
    )
    write_history(conn, actor, history_action, reason, flag_id=fid, env_id=eid, trace_id=trace_id)

    # 甲路径关键：同一事务内批量级联停用全部活跃覆盖，并落同源 override_off 痕。
    stopped = cascade_for_kill(
        conn, flag_id=fid, env_id=eid, actor=actor, trace_id=trace_id
    )
    return kid, ver, trace_id, stopped


@router.post("")
def kill_switch(body: KillIn, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    reason = require_str(body.reason, "熔断原因", 6)
    key = f"flag-kill:{fid}:{eid}"
    with connect() as conn:
        # 争用败方会在这里只写 contention_marks 后抛 409，不进入任何业务写。
        acquire(conn, key, user["username"])
        result = None
        try:
            fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
            if not fl:
                raise HTTPException(404, detail="开关不存在")
            assert_not_killed_or_allow_kill(fl["status"], for_kill=True)
            now = time.time()
            if body.use_shadow:
                # 影子紧急草案：正式区零改（含覆盖），升正时才级联。
                cur = conn.execute(
                    "INSERT INTO kill_shadows(flag_id, env_id, reason, status, created_at, created_by) "
                    "VALUES(?,?,?,?,?,?)",
                    (fid, eid, reason, "open", now, user["username"]),
                )
                sid = cur.lastrowid
                write_audit(conn, user["username"], "kill_shadow", "kill_shadow", sid, describe_path("kill", True))
                write_history(conn, user["username"], "kill_shadow", reason, flag_id=fid, env_id=eid)
                result = {"mode": "shadow", "id": sid, "cascaded_overrides": 0, "deactivated_overrides": 0}
            else:
                kid, ver, trace_id, stopped = _write_official_kill(
                    conn, fid=fid, eid=eid, reason=reason, actor=user["username"]
                )
                result = {
                    "mode": "official",
                    "id": kid,
                    "version": ver,
                    "trace_id": trace_id,
                    "cascaded_overrides": len(stopped),
                    "deactivated_overrides": len(stopped),
                    "cascaded": stopped,
                }
            conn.commit()
        except Exception:
            # 甲路径“否则整笔回滚”：绝不能让 finally 把半笔账提交出去。
            conn.rollback()
            raise
        finally:
            release(conn, key, user["username"])
        return result


@router.post("/shadow/{shadow_id}/promote")
def promote_kill_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        sh = conn.execute("SELECT * FROM kill_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh or sh["status"] != "open":
            raise HTTPException(400, detail="影子熔断不可升正")
        fid, eid = sh["flag_id"], sh["env_id"]
        key = f"flag-kill:{fid}:{eid}"
        acquire(conn, key, user["username"])
        result = None
        try:
            kid, ver, trace_id, stopped = _write_official_kill(
                conn,
                fid=fid,
                eid=eid,
                reason=sh["reason"],
                actor=user["username"],
                history_action="kill_promote",
                audit_action="kill_promote",
                audit_detail=f"from_shadow={shadow_id}",
            )
            conn.execute(
                "UPDATE kill_shadows SET status='promoted' WHERE id=?", (shadow_id,)
            )
            conn.commit()
            result = {
                "id": kid,
                "version": ver,
                "mode": "official",
                "trace_id": trace_id,
                "cascaded_overrides": len(stopped),
                "deactivated_overrides": len(stopped),
                "cascaded": stopped,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn, key, user["username"])
        return result
