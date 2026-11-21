"""假绿夹具：容量下调抬代数后，旧代占位仍确认进正式册。

走本机库文件 data/app.db（不猴补丁、不临时库），全程打真实接口：
  1. 造演示档 S-DRIFT-01（容量 10，代数 1），复位其历史演示数据；
  2. 占 2 席（占位时档期为代 1）；
  3. 直改库：容量 10→3、代数 +1（模拟运维容量下调、代数抬升的既成事实）；
  4. 拿旧代占位去确认。

退出码：
  0 = 假绿复现：旧代确认成功、对账灯全绿（即本夹具要暴露的缺陷）；
  1 = 旧代已被拒确认：假绿消除（修复后的期望态）；
  2 = 夹具自身故障。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
import db as dbmod  # noqa: E402

DEMO_CODE = "S-DRIFT-01"
DEMO_PARTY = "P-DRIFT"


def _reset_demo_slot() -> None:
    """复位演示档：清掉本夹具历次产生的占位/正式册，容量 10、代数 1。"""
    with dbmod.connect() as conn:
        row = conn.execute("SELECT id FROM slots WHERE code=?", (DEMO_CODE,)).fetchone()
        if row:
            conn.execute("DELETE FROM bookings WHERE slot_id=?", (row["id"],))
            conn.execute("DELETE FROM holds WHERE slot_id=?", (row["id"],))
            conn.execute(
                "UPDATE slots SET capacity=10, booked=0, generation=1, status='开放' WHERE id=?",
                (row["id"],),
            )
            conn.commit()


def _capacity_drift() -> None:
    """容量下调 10→3、代数抬升（运维既成事实，直改本机库）。"""
    with dbmod.connect() as conn:
        conn.execute(
            "UPDATE slots SET capacity=3, generation=generation+1 WHERE code=?",
            (DEMO_CODE,),
        )
        conn.commit()


def main() -> int:
    print(f"[夹具] 库文件: {dbmod.DB_PATH}")
    _reset_demo_slot()
    with TestClient(app) as c:  # 走真实应用与真实库
        r = c.post("/api/login", json={"username": "admin", "password": "123456"})
        assert r.status_code == 200, r.text
        h = {"Authorization": f"Bearer {r.json()['token']}"}

        r = c.post(
            "/api/slots",
            headers=h,
            json={
                "code": DEMO_CODE,
                "title": "假绿演示档",
                "start_at": "2026-11-22T09:00:00",
                "end_at": "2026-11-22T11:00:00",
                "capacity": 10,
            },
        )
        assert r.status_code in (200, 409), r.text  # 409 = 档已存在，复用

        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": DEMO_CODE, "party_key": DEMO_PARTY, "seats": 2},
        )
        assert r.status_code == 200 and r.json()["disposition"] == "占位", r.text
        hold_id = r.json()["hold_id"]
        print(f"[夹具] 占位成功 hold_id={hold_id}（占位时档期代 1，容量 10）")

        _capacity_drift()
        slot = c.get(f"/api/slots/{DEMO_CODE}", headers=h).json()
        print(f"[夹具] 容量下调抬代后: capacity={slot['capacity']} generation={slot['generation']}")

        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hold_id})
        recon = c.get(f"/api/reconcile/{DEMO_CODE}", headers=h).json()
        books = c.get("/api/bookings", headers=h).json()["items"]
        mine = [b for b in books if b["hold_id"] == hold_id]
        audits = c.get("/api/audit", headers=h, params={"limit": 5}).json()["items"]

        print(f"[夹具] 确认回执: HTTP {r.status_code} {r.text[:200]}")
        print(f"[夹具] 正式册该占位落账: {mine}")
        print(f"[夹具] 对账灯: {recon}")
        print(f"[夹具] 审计尾部: {[a['action'] for a in audits]}")

        if r.status_code == 200:
            snap = mine[0]["snapshot_gen"] if mine else None
            print(
                f"[假绿] 旧代（占位代 1）占位确认成功，snapshot_gen={snap}，"
                f"对账灯 ok={recon.get('ok')} —— 账面绿但代数已漂移"
            )
            return 0
        if r.status_code == 409:
            ok = not mine and recon.get("ok") is True
            print(
                f"[修复态] 旧代占位被拒确认（409），正式册零增={not mine}，"
                f"对账灯 ok={recon.get('ok')}，五处同态={'是' if ok else '否'}"
            )
            return 1
        print(f"[故障] 非预期回执: {r.status_code}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
