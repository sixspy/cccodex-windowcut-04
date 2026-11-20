"""窗重开：甲路径·作废重算（本仓正确性口径唯一落库形态，两边不得混用）。

- 重开同事务：作废旧世代物化与窗内事件、generation+1、窗重新开启；
  失败则状态与世代整体回滚，台账不留半截账。
- 窗重开册：当天首开封定口径并落 reopen_registry，当天禁止改口；
  偏离拒写并记「重开偏了」（reopen_deviated）。
- 重开锁 reopen:{wid}：两路同时重开同一窗口，后到只记争用不写业务。
- 重开台账 / 窗口世代 / 物化列表 / 对账互证 / 审计五处同态。
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, write_audit
from locks import release, try_acquire
from models import ReopenIn

router = APIRouter(tags=["reopen"])

CANONICAL_MODE = "作废重算"  # 甲路径：重开即作废旧世代重算；乙路径（增量补丁）不落地
LEDGER_DONE = "已重开"


class _ReopenAbort(Exception):
    """夹具用：强制重开事务中段失败。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@router.get("/api/reopen-ledger")
def list_reopen(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, w.name AS window_name, w.generation AS current_generation
               FROM reopen_ledger r
               JOIN windows w ON w.id=r.window_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/reopen-registry")
def get_registry(authorization: AuthHeader = None):
    """窗重开册：当日口径封定记录，供对账页与审计互证。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reopen_registry ORDER BY id DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows], "canonical_mode": CANONICAL_MODE}


def _deviate(conn, user: str, wid: int, day: str, requested: str, pinned: str | None):
    """偏离窗重开册：拒写并记重开偏了。"""
    write_audit(
        conn,
        user,
        "reopen_deviated",
        str(wid),
        {
            "note": "重开偏了",
            "day": day,
            "requested_mode": requested,
            "registry_mode": pinned or CANONICAL_MODE,
        },
    )
    conn.commit()
    raise HTTPException(
        409, f"偏离窗重开册（当日口径={pinned or CANONICAL_MODE}）：拒写并记重开偏了"
    )


@router.post("/api/windows/{wid}/reopen")
def reopen_window(wid: int, body: ReopenIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"reopen:{wid}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 争用落账：后到只记争用，不写业务
            raise HTTPException(409, "争用：重开锁被占，后到只记争用")
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] != "已关闭":
                raise HTTPException(409, "仅已关闭窗可重开")
            today = date.today().isoformat()
            reg = conn.execute(
                "SELECT * FROM reopen_registry WHERE day=?", (today,)
            ).fetchone()
            if reg is not None and reg["mode"] != body.mode:
                _deviate(conn, user, wid, today, body.mode, reg["mode"])
            if reg is None and body.mode != CANONICAL_MODE:
                _deviate(conn, user, wid, today, body.mode, None)

            from_gen = int(w["generation"])
            to_gen = from_gen + 1
            now = _now()
            try:
                if reg is None:
                    conn.execute(
                        """INSERT INTO reopen_registry(day,mode,decided_by,decided_at)
                           VALUES (?,?,?,?)""",
                        (today, CANONICAL_MODE, user, now),
                    )
                void_mats = conn.execute(
                    """UPDATE materializations SET status='作废'
                       WHERE window_id=? AND status='成功'""",
                    (wid,),
                ).rowcount
                void_events = conn.execute(
                    """UPDATE events SET disposition='作废'
                       WHERE window_id=? AND disposition='窗内'""",
                    (wid,),
                ).rowcount
                conn.execute(
                    """UPDATE windows SET status='开启', generation=?, seal_token=NULL
                       WHERE id=?""",
                    (to_gen, wid),
                )
                conn.execute(
                    """INSERT INTO reopen_ledger(window_id,from_generation,to_generation,mode,status,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (wid, from_gen, to_gen, CANONICAL_MODE, LEDGER_DONE, now),
                )
                write_audit(
                    conn,
                    user,
                    "reopen_ok",
                    str(wid),
                    {
                        "mode": CANONICAL_MODE,
                        "from_generation": from_gen,
                        "to_generation": to_gen,
                        "voided_materializations": void_mats,
                        "voided_events": void_events,
                    },
                )
                if body.fail_simulate:
                    raise _ReopenAbort("夹具模拟重开中段失败")
            except HTTPException:
                conn.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 —— 失败则状态与世代整体回滚
                conn.rollback()
                write_audit(
                    conn,
                    user,
                    "reopen_fail",
                    str(wid),
                    {
                        "mode": CANONICAL_MODE,
                        "from_generation": from_gen,
                        "to_generation": to_gen,
                        "error": str(exc),
                        "rolled_back": True,
                    },
                )
                conn.commit()
                raise HTTPException(500, "重开失败：状态与世代已回滚") from exc
            conn.commit()
            return {
                "ok": True,
                "mode": CANONICAL_MODE,
                "window_id": wid,
                "from_generation": from_gen,
                "to_generation": to_gen,
                "voided_materializations": void_mats,
                "voided_events": void_events,
                "status": LEDGER_DONE,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()
