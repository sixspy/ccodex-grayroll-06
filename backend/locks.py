"""行锁与争用痕（嵌进发布/熔断主写路径）。

grayroll-06 争用口径：两路会话抢同一个开关环境写锁（如 flag-kill:{fid}:{eid}），
必须恰好一路成功；败方不进业务写口，只落一条 contention_marks 争用痕并收 409。

实现两层：
1. 进程内按 lock_key 的非阻塞互斥锁：单 uvicorn 进程（docker compose 默认单 worker）
   下给出严格的“一成一争”，消除“SELECT 无锁 -> 无条件 UPSERT 互相覆盖”的 check-then-act。
2. 数据库 row_locks 的原子条件占位：保证锁可观测、TTL 可过期，并为多进程/多 worker
   提供同一方向的最终防线。
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
TTL = 20.0

_guard = threading.Lock()
_proc_locks: dict[str, threading.Lock] = {}
_holders: dict[str, str] = {}


def _proc_lock_for(key: str) -> threading.Lock:
    with _guard:
        lk = _proc_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _proc_locks[key] = lk
        return lk


def _local_holder(key: str) -> str | None:
    with _guard:
        return _holders.get(key)


def _set_local_holder(key: str, holder: str) -> None:
    with _guard:
        _holders[key] = holder


def _clear_local_holder(key: str, holder: str) -> None:
    with _guard:
        if _holders.get(key) == holder:
            _holders.pop(key, None)


def _mark_contention(conn, key: str, loser: str, winner: str) -> None:
    """败方唯一允许的正式写：争用痕。单独提交，确保不被赢家事务回滚带走。"""
    now = time.time()
    try:
        conn.execute(
            "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
            (key, loser, winner, now),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _conflict(key: str, loser: str, winner: str | None, conn=None) -> None:
    winner = winner or _local_holder(key) or "concurrent-session"
    if conn is not None:
        _mark_contention(conn, key, loser, winner)
    raise HTTPException(409, detail="该开关环境正被另一会话抢修，熔断锁争用，请稍后重试")


def acquire(conn, key: str, holder: str) -> None:
    """占据 key 的写锁。成功者返回（锁行随调用方事务提交）；争用者落痕并抛 409。"""
    plock = _proc_lock_for(key)
    if not plock.acquire(blocking=False):
        # 同进程已有另一会话持锁：本会话立即判负，绝不进入业务写口。
        _conflict(key, holder, _local_holder(key), conn)

    _set_local_holder(key, holder)
    now = time.time()
    try:
        # 先清过期锁（崩溃残留），这是写操作，会在 SQLite 层串行化同 key 争用。
        conn.execute("DELETE FROM row_locks WHERE lock_key=? AND expires_at<=?", (key, now))
        # 原子条件占位：仅当无行、或同行持有者是自己（重入/续期）时才写入。
        conn.execute(
            """
            INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)
            ON CONFLICT(lock_key) DO UPDATE
            SET holder=excluded.holder, expires_at=excluded.expires_at
            WHERE row_locks.holder=?
            """,
            (key, holder, now + TTL, holder),
        )
        row = conn.execute(
            "SELECT holder, expires_at FROM row_locks WHERE lock_key=?", (key,)
        ).fetchone()
        if row is None or row["holder"] != holder or float(row["expires_at"]) <= now:
            winner = row["holder"] if row is not None else (_local_holder(key) or "other-worker")
            _clear_local_holder(key, holder)
            try:
                plock.release()
            except RuntimeError:
                pass
            _conflict(key, holder, winner, conn)
        # 赢家：不单独 commit，锁行与业务写在调用方同一事务里提交/回滚。
    except HTTPException:
        raise
    except Exception:
        _clear_local_holder(key, holder)
        try:
            plock.release()
        except RuntimeError:
            pass
        raise


def release(conn, key: str, holder: str) -> None:
    """释放数据库锁行与进程内互斥；自带提交，保证释放先于下一会话判定。"""
    try:
        conn.execute("DELETE FROM row_locks WHERE lock_key=? AND holder=?", (key, holder))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _clear_local_holder(key, holder)
        plock = _proc_locks.get(key)
        if plock is not None:
            try:
                plock.release()
            except RuntimeError:
                pass


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
