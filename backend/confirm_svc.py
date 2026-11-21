"""确认入册：占位 → 正式预订；比对占位代数与档期现代数，跨代拒确认（甲路径）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, require_lock

router = APIRouter(tags=["confirm"])


class ConfirmIn(BaseModel):
    hold_id: int


@router.get("/api/bookings")
def list_bookings(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.*, s.code AS slot_code FROM bookings b
               JOIN slots s ON s.id=b.slot_id ORDER BY b.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/bookings/confirm")
def confirm_hold(body: ConfirmIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
        if not hold:
            raise HTTPException(404, "占位不存在")
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (hold["slot_id"],)).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"
        try:
            require_lock(conn, lock_key, user)
        except HTTPException:
            conn.commit()  # 后到只记争用：争用落账，业务零写
            raise
        try:
            hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
            if hold["status"] != "占位中":
                raise HTTPException(409, "占位已非占位中，禁止确认")
            if hold["expire_at"] < datetime.now().isoformat(timespec="seconds"):
                conn.execute(
                    "UPDATE holds SET status='已过期' WHERE id=?", (hold["id"],)
                )
                write_audit(conn, user, "hold_expire", str(hold["id"]), {})
                conn.commit()
                raise HTTPException(409, "占位已过期")
            # 代数拒确认册·甲路径：占位代数≠档期现代数 → 正式零增整笔回滚
            if int(hold["hold_gen"]) != int(slot["generation"]):
                write_audit(
                    conn,
                    user,
                    "confirm_reject_gen",
                    hold["party_key"],
                    {
                        "hold_id": hold["id"],
                        "slot": slot["code"],
                        "hold_gen": hold["hold_gen"],
                        "slot_gen": slot["generation"],
                        "seats": hold["seats"],
                        "register": "甲路径-代数比对整笔回滚",
                    },
                )
                conn.commit()  # 拒写（记偏）与业务零写同账同败
                raise HTTPException(
                    409,
                    detail={
                        "error": "gen_mismatch",
                        "message": (
                            f"代数不一致拒确认：占位代数{hold['hold_gen']}"
                            f"≠档期现代数{slot['generation']}，正式零增整笔回滚"
                        ),
                        "hold_id": hold["id"],
                        "hold_gen": hold["hold_gen"],
                        "slot_gen": slot["generation"],
                        "slot": slot["code"],
                        "register": "甲路径",
                    },
                )
            remain = int(slot["capacity"]) - int(slot["booked"])
            if hold["seats"] > remain:
                raise HTTPException(409, "确认时容量不足，请释放或改候补")
            now = datetime.now().isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO bookings(slot_id,hold_id,party_key,seats,snapshot_gen,status,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    slot["id"],
                    hold["id"],
                    hold["party_key"],
                    hold["seats"],
                    slot["generation"],
                    "已确认",
                    now,
                ),
            )
            conn.execute(
                "UPDATE holds SET status='已确认' WHERE id=?", (hold["id"],)
            )
            conn.execute(
                "UPDATE slots SET booked = booked + ? WHERE id=?",
                (hold["seats"], slot["id"]),
            )
            write_audit(
                conn,
                user,
                "booking_confirm",
                hold["party_key"],
                {"booking_id": cur.lastrowid, "gen": slot["generation"]},
            )
            conn.commit()
            return {"ok": True, "booking_id": cur.lastrowid, "snapshot_gen": slot["generation"]}
        finally:
            release(conn, lock_key, user)
            conn.commit()
