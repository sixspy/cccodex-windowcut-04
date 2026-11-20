"""红测二：双人同时重开同一 window_id，须占重开锁，后到只记争用。

夹具：关窗且已物化的窗口；甲（admin）先占 reopen:{wid} 锁，
乙（closer）同时发起重开 → 409，只落争用账不写业务；
甲放锁后重开成功，世代正常抬升。
"""
import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "reopen_lock.db")
    init_db()
    yield


def _auth(c: TestClient, username: str, password: str) -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _build_closed_window(c: TestClient, h: dict, name: str) -> int:
    w = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": name,
            "open_at": "2026-11-06T00:00:00",
            "close_at": "2026-11-06T23:59:59",
        },
    ).json()
    wid = w["id"]
    c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": name,
            "biz_key": f"{name}-E1",
            "event_ts": "2026-11-06T09:00:00",
            "amount": 7.5,
        },
    )
    assert c.post(f"/api/windows/{wid}/close", headers=h).status_code == 200
    assert c.post(f"/api/windows/{wid}/materialize", headers=h, json={}).status_code == 200
    return wid


def test_two_users_reopen_same_window_latecomer_only_records_contention():
    c = TestClient(app)
    h_admin = _auth(c, "admin", "123456")
    h_closer = _auth(c, "closer", "close123")
    wid = _build_closed_window(c, h_admin, "W-REOPEN-LOCK")

    # 甲先占重开锁（模拟其重开事务进行中）
    assert (
        c.post(
            "/api/locks/acquire", headers=h_admin, json={"lock_key": f"reopen:{wid}"}
        ).status_code
        == 200
    )

    # 乙同时重开同窗：只记争用，不写业务
    r = c.post(
        f"/api/windows/{wid}/reopen", headers=h_closer, json={"mode": "作废重算"}
    )
    assert r.status_code == 409
    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(x["lock_key"] == f"reopen:{wid}" for x in cont), "争用未落账"

    # 乙的请求不得产生任何业务写：世代/状态/台账/物化全部原样
    w = c.get(f"/api/windows/{wid}", headers=h_admin).json()["window"]
    assert w["generation"] == 1 and w["status"] == "已关闭"
    ledger = [
        x
        for x in c.get("/api/reopen-ledger", headers=h_admin).json()["items"]
        if x["window_id"] == wid
    ]
    assert ledger == []
    mats = [
        m
        for m in c.get("/api/materializations", headers=h_admin).json()["items"]
        if m["window_id"] == wid
    ]
    assert len(mats) == 1 and mats[0]["status"] == "成功"

    # 甲放锁后重开成功：世代抬升、台账落账
    assert (
        c.post(
            "/api/locks/release", headers=h_admin, json={"lock_key": f"reopen:{wid}"}
        ).status_code
        == 200
    )
    ok = c.post(f"/api/windows/{wid}/reopen", headers=h_admin, json={"mode": "作废重算"})
    assert ok.status_code == 200
    w2 = c.get(f"/api/windows/{wid}", headers=h_admin).json()["window"]
    assert w2["generation"] == 2 and w2["status"] == "开启"
    ledger2 = [
        x
        for x in c.get("/api/reopen-ledger", headers=h_admin).json()["items"]
        if x["window_id"] == wid
    ]
    assert len(ledger2) == 1 and ledger2[0]["to_generation"] == 2
