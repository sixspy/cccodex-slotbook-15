"""对账：档期 booked 与正式册 seats 同源校验；正式册快照代与来源占位代数互证。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = []
        for s in slots:
            booked_sum = conn.execute(
                """SELECT COALESCE(SUM(seats),0) s FROM bookings
                   WHERE slot_id=? AND status='已确认'""",
                (s["id"],),
            ).fetchone()["s"]
            gen_drift = conn.execute(
                """SELECT COUNT(*) c FROM bookings b JOIN holds h ON h.id=b.hold_id
                   WHERE b.slot_id=? AND b.status='已确认' AND b.snapshot_gen != h.hold_gen""",
                (s["id"],),
            ).fetchone()["c"]
            stale_holds = conn.execute(
                """SELECT COUNT(*) c FROM holds
                   WHERE slot_id=? AND status='占位中' AND hold_gen != ?""",
                (s["id"], s["generation"]),
            ).fetchone()["c"]
            ok = int(booked_sum) == int(s["booked"]) and gen_drift == 0
            items.append(
                {
                    "slot_code": s["code"],
                    "slot_booked": s["booked"],
                    "booking_seats": booked_sum,
                    "generation": s["generation"],
                    "gen_drift": gen_drift,
                    "stale_holds": stale_holds,
                    "ok": ok,
                    "status": s["status"],
                }
            )
        return {"items": items, "all_ok": all(i["ok"] for i in items)}


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        booked_sum = conn.execute(
            """SELECT COALESCE(SUM(seats),0) s FROM bookings
               WHERE slot_id=? AND status='已确认'""",
            (s["id"],),
        ).fetchone()["s"]
        holds = conn.execute(
            "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
            (s["id"],),
        ).fetchone()["c"]
        gen_drift = conn.execute(
            """SELECT COUNT(*) c FROM bookings b JOIN holds h ON h.id=b.hold_id
               WHERE b.slot_id=? AND b.status='已确认' AND b.snapshot_gen != h.hold_gen""",
            (s["id"],),
        ).fetchone()["c"]
        stale_holds = conn.execute(
            """SELECT COUNT(*) c FROM holds
               WHERE slot_id=? AND status='占位中' AND hold_gen != ?""",
            (s["id"], s["generation"]),
        ).fetchone()["c"]
        return {
            "slot_code": code,
            "slot_booked": s["booked"],
            "booking_seats": booked_sum,
            "open_holds": holds,
            "ok": int(booked_sum) == int(s["booked"]) and gen_drift == 0,
            "generation": s["generation"],
            "gen_drift": gen_drift,
            "stale_holds": stale_holds,
        }
