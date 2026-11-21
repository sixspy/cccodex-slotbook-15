"""档期盘：开档、关档、调容抬代、容量查询。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, require_lock

router = APIRouter(tags=["slots"])


class SlotIn(BaseModel):
    code: str
    title: str
    start_at: str
    end_at: str
    capacity: int = Field(ge=1, le=500)


class CloseIn(BaseModel):
    code: str


class CapacityIn(BaseModel):
    code: str
    capacity: int = Field(ge=1, le=500)


@router.get("/api/slots")
def list_slots(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM slots ORDER BY start_at, code"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/slots/{code}")
def get_slot(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, "档期不存在")
        return dict(row)


@router.post("/api/slots")
def create_slot(body: SlotIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            exists = conn.execute(
                "SELECT id FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if exists:
                raise HTTPException(409, "档期编码已存在")
            if body.end_at <= body.start_at:
                raise HTTPException(400, "结束时间必须晚于开始")
            conn.execute(
                """INSERT INTO slots(code,title,start_at,end_at,capacity,booked,status,generation)
                   VALUES (?,?,?,?,?,0,'开放',1)""",
                (body.code, body.title, body.start_at, body.end_at, body.capacity),
            )
            write_audit(conn, user, "slot_create", body.code, body.model_dump())
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True, "code": body.code}


@router.post("/api/slots/close")
def close_slot(body: CloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if row["status"] == "关闭":
                raise HTTPException(409, "档期已关闭")
            open_holds = conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
                (row["id"],),
            ).fetchone()["c"]
            if open_holds:
                raise HTTPException(409, "仍有未确认占位，禁止关档")
            conn.execute(
                "UPDATE slots SET status='关闭' WHERE id=?", (row["id"],)
            )
            write_audit(conn, user, "slot_close", body.code, {"generation": row["generation"]})
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True}


@router.post("/api/slots/capacity")
def adjust_capacity(body: CapacityIn, authorization: AuthHeader = None):
    """调容即抬代：容量变更把档期 generation+1，旧代占位在确认口被拦。"""
    user = require_writer(authorization)
    lock_key = f"slot:{body.code}"
    with connect() as conn:
        try:
            require_lock(conn, lock_key, user)
        except HTTPException:
            conn.commit()  # 争用落账，业务零写
            raise
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if row["status"] != "开放":
                raise HTTPException(409, "档期未开放，禁止调容")
            if body.capacity < int(row["booked"]):
                raise HTTPException(409, "容量不得低于已确认占用")
            if body.capacity == int(row["capacity"]):
                stale = conn.execute(
                    """SELECT COUNT(*) c FROM holds
                       WHERE slot_id=? AND status='占位中' AND hold_gen != ?""",
                    (row["id"], row["generation"]),
                ).fetchone()["c"]
                return {
                    "ok": True,
                    "code": body.code,
                    "capacity": row["capacity"],
                    "generation": row["generation"],
                    "stale_holds": stale,
                }
            new_gen = int(row["generation"]) + 1
            conn.execute(
                "UPDATE slots SET capacity=?, generation=? WHERE id=?",
                (body.capacity, new_gen, row["id"]),
            )
            stale = conn.execute(
                """SELECT COUNT(*) c FROM holds
                   WHERE slot_id=? AND status='占位中' AND hold_gen != ?""",
                (row["id"], new_gen),
            ).fetchone()["c"]
            write_audit(
                conn,
                user,
                "slot_capacity",
                body.code,
                {
                    "old_capacity": row["capacity"],
                    "new_capacity": body.capacity,
                    "old_gen": row["generation"],
                    "new_gen": new_gen,
                    "stale_holds": stale,
                },
            )
            conn.commit()
            return {
                "ok": True,
                "code": body.code,
                "capacity": body.capacity,
                "generation": new_gen,
                "stale_holds": stale,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return {
            "slots_open": conn.execute(
                "SELECT COUNT(*) c FROM slots WHERE status='开放'"
            ).fetchone()["c"],
            "holds_open": conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE status='占位中'"
            ).fetchone()["c"],
            "bookings": conn.execute(
                "SELECT COUNT(*) c FROM bookings WHERE status='已确认'"
            ).fetchone()["c"],
            "waitlist": conn.execute(
                "SELECT COUNT(*) c FROM waitlist WHERE status='候补中'"
            ).fetchone()["c"],
            "now": datetime.now().isoformat(timespec="seconds"),
        }
