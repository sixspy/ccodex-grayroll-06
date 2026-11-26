"""变更历史。grayroll-06：支持按开关/环境/trace_id 追同源双痕。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def list_history(
    authorization: str | None = Header(default=None),
    limit: int = 100,
    flag_id: int | None = None,
    env_id: int | None = None,
    trace_id: str | None = None,
):
    current_user(authorization)
    where, params = [], []
    if flag_id is not None:
        where.append("h.flag_id=?")
        params.append(flag_id)
    if env_id is not None:
        where.append("h.env_id=?")
        params.append(env_id)
    if trace_id:
        where.append("h.trace_id=?")
        params.append(trace_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT h.*, f.key AS flag_key, e.code AS env_code
                FROM change_history h
                LEFT JOIN flags f ON f.id=h.flag_id
                LEFT JOIN environments e ON e.id=h.env_id
                {clause}
                ORDER BY h.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        ]
    return {"items": rows}
