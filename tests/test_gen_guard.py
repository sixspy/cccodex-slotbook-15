"""代数拒确认：跨代占位禁止落正式；两人抢确认，后到只记争用。

口径见 ops/代数拒确认册（甲路径：代数比对，正式零增整笔回滚，拒写与回滚同败）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
import db as dbmod  # noqa: E402
from db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _hold(c, h, slot="S-AM-01", party="P-G1", seats=2):
    r = c.post(
        "/api/holds", headers=h,
        json={"slot_code": slot, "party_key": party, "seats": seats},
    )
    assert r.status_code == 200 and r.json()["disposition"] == "占位", r.text
    return r.json()["hold_id"]


def _capacity_drift(code, capacity):
    """容量下调、代数抬升（运维既成事实，直改库）。"""
    with dbmod.connect() as conn:
        conn.execute(
            "UPDATE slots SET capacity=?, generation=generation+1 WHERE code=?",
            (capacity, code),
        )
        conn.commit()


def test_old_gen_confirm_rejected():
    """容量下调抬代后，旧代占位确认须被拒：正式零增、记偏、灯不翻红。"""
    with TestClient(app) as c:
        h = _login(c)
        hid = _hold(c, h, seats=1)  # 占位时档期代 1
        _capacity_drift("S-AM-01", 3)  # 容量 10→3，代数 1→2；余量仍够，不挡确认
        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        # 回执带双方代数（禁止只弹一句容量已变更）
        assert detail["hold_gen"] == 1 and detail["slot_gen"] == 2
        # 正式册零增
        books = c.get("/api/bookings", headers=h).json()["items"]
        assert all(b["hold_id"] != hid for b in books)
        # 甲路径不标占位失效，留待自然过期
        holds = c.get("/api/holds", headers=h).json()["items"]
        assert [x for x in holds if x["id"] == hid][0]["status"] == "占位中"
        # 档期账面零增（种子已占 2 席），现代数 2
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["booked"] == 2 and slot["generation"] == 2
        # 审计记偏，且与回执同一代数口径
        audits = c.get("/api/audit", headers=h).json()["items"]
        rej = [a for a in audits if a["action"] == "confirm_reject_gen"]
        assert rej, "拒确认须落审计记偏"
        assert '"hold_gen": 1' in rej[0]["payload"]
        assert '"slot_gen": 2' in rej[0]["payload"]
        # 对账灯：无漂移、不翻红
        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert recon["ok"] is True and recon["gen_drift"] == 0


def test_two_party_old_gen_contention():
    """两人同时持旧代占位确认：须占档锁，后到只记争用；持锁人同样被拒。"""
    with TestClient(app) as c:
        ha = _login(c, "admin", "123456")
        hb = _login(c, "clerk", "123456")
        hid_a = _hold(c, ha, slot="S-PM-02", party="P-A", seats=1)
        hid_b = _hold(c, hb, slot="S-PM-02", party="P-B", seats=1)
        _capacity_drift("S-PM-02", 4)  # 代数 1→2
        # clerk 先占档锁
        r = c.post("/api/locks/acquire", headers=hb, json={"lock_key": "slot:S-PM-02"})
        assert r.status_code == 200, r.text
        # admin 后到：409 争用，争用落账，业务零写
        r = c.post("/api/bookings/confirm", headers=ha, json={"hold_id": hid_a})
        assert r.status_code == 409 and "争用" in str(r.json()["detail"]), r.text
        cont = c.get("/api/contention", headers=ha).json()["items"]
        mine = [x for x in cont if x["actor"] == "admin" and x["lock_key"] == "slot:S-PM-02"]
        assert len(mine) == 1, "后到只记争用，须落 contention_log"
        # 持锁人确认旧代占位：同样拒确认，不得 200
        r = c.post("/api/bookings/confirm", headers=hb, json={"hold_id": hid_b})
        assert r.status_code == 409, r.text
        # 全程正式册零增、两占位仍在占位中
        books = c.get("/api/bookings", headers=ha).json()["items"]
        assert all(b["hold_id"] not in (hid_a, hid_b) for b in books)
        holds = c.get("/api/holds", headers=ha).json()["items"]
        st = {x["id"]: x["status"] for x in holds}
        assert st[hid_a] == "占位中" and st[hid_b] == "占位中"
        # 无确认审计，只有 clerk 的记偏
        audits = c.get("/api/audit", headers=ha).json()["items"]
        assert not [
            a for a in audits
            if a["action"] == "booking_confirm" and a["target"] in ("P-A", "P-B")
        ]
        assert [a for a in audits if a["action"] == "confirm_reject_gen"]


def test_capacity_adjust_bumps_generation():
    """调容即抬代：旧代占位拒确认，新代占位可确认，五处同态。"""
    with TestClient(app) as c:
        h = _login(c)
        hid_old = _hold(c, h, party="P-OLD", seats=1)  # 代 1 占位
        r = c.post("/api/slots/capacity", headers=h,
                   json={"code": "S-AM-01", "capacity": 5})
        assert r.status_code == 200, r.text
        assert r.json()["generation"] == 2  # 容量变更抬代数
        assert r.json()["stale_holds"] == 1  # 旧代占位被点名
        # 旧代占位拒确认
        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid_old})
        assert r.status_code == 409, r.text
        # 新代占位可确认，回执快照代 = 现代数
        hid_new = _hold(c, h, party="P-NEW", seats=1)
        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid_new})
        assert r.status_code == 200 and r.json()["snapshot_gen"] == 2, r.text
        # 对账灯绿且无漂移
        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert recon["ok"] is True and recon["gen_drift"] == 0
        assert recon["stale_holds"] == 1
        # 调容落审计
        audits = c.get("/api/audit", headers=h).json()["items"]
        assert any(a["action"] == "slot_capacity" for a in audits)
