"""窗重开路径补测：失败回滚、窗重开册口径、全周期世代物化同源。

- 甲路径失败：状态与世代整体回滚，台账不留半截账。
- 窗重开册：当天首开封定口径，偏离拒写并记「重开偏了」，当天禁止改口。
- 全周期：重开 → 入新账 → 再关窗 → 再物化，新物化挂新世代，对账对齐。
"""
import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "reopen_paths.db")
    init_db()
    yield


def _auth(c: TestClient) -> dict:
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _build_closed_window(c: TestClient, h: dict, name: str, day: str) -> int:
    w = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": name,
            "open_at": f"{day}T00:00:00",
            "close_at": f"{day}T23:59:59",
        },
    ).json()
    wid = w["id"]
    c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": name,
            "biz_key": f"{name}-E1",
            "event_ts": f"{day}T10:00:00",
            "amount": 20.0,
        },
    )
    assert c.post(f"/api/windows/{wid}/close", headers=h).status_code == 200
    assert c.post(f"/api/windows/{wid}/materialize", headers=h, json={}).status_code == 200
    return wid


def _window(c, h, wid):
    return c.get(f"/api/windows/{wid}", headers=h).json()["window"]


def _ledger(c, h, wid):
    return [
        x
        for x in c.get("/api/reopen-ledger", headers=h).json()["items"]
        if x["window_id"] == wid
    ]


def _mats(c, h, wid):
    return [
        m
        for m in c.get("/api/materializations", headers=h).json()["items"]
        if m["window_id"] == wid
    ]


def test_reopen_failure_rolls_back_state_and_generation():
    c = TestClient(app)
    h = _auth(c)
    wid = _build_closed_window(c, h, "W-REOPEN-FAIL", "2026-11-07")

    r = c.post(
        f"/api/windows/{wid}/reopen",
        headers=h,
        json={"mode": "作废重算", "fail_simulate": True},
    )
    assert r.status_code == 500

    # 状态与世代回滚：窗仍关闭、g 仍为 1、物化仍成功、事件仍窗内
    w = _window(c, h, wid)
    assert w["generation"] == 1 and w["status"] == "已关闭"
    mats = _mats(c, h, wid)
    assert len(mats) == 1 and mats[0]["status"] == "成功"
    evs = [
        e
        for e in c.get("/api/events", headers=h).json()["items"]
        if e["window_id"] == wid
    ]
    assert len(evs) == 1 and evs[0]["disposition"] == "窗内"

    # 台账不留半截账；窗重开册未被失败封定；审计留失败痕
    assert _ledger(c, h, wid) == []
    assert c.get("/api/reopen-registry", headers=h).json()["items"] == []
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "reopen_fail" for a in audits)


def test_reopen_registry_pins_mode_and_deviation_refused():
    c = TestClient(app)
    h = _auth(c)
    wid_a = _build_closed_window(c, h, "W-REOPEN-REG-A", "2026-11-08")
    wid_b = _build_closed_window(c, h, "W-REOPEN-REG-B", "2026-11-09")

    # 未封定前请求乙路径：偏离本仓口径，拒写并记重开偏了
    r0 = c.post(f"/api/windows/{wid_a}/reopen", headers=h, json={"mode": "增量补丁"})
    assert r0.status_code == 409
    assert _window(c, h, wid_a)["generation"] == 1

    # 甲路径首开成功，窗重开册封定当日口径
    r1 = c.post(f"/api/windows/{wid_a}/reopen", headers=h, json={"mode": "作废重算"})
    assert r1.status_code == 200
    reg = c.get("/api/reopen-registry", headers=h).json()
    assert reg["canonical_mode"] == "作废重算"
    assert len(reg["items"]) == 1 and reg["items"][0]["mode"] == "作废重算"

    # 当天改口走乙路径：偏离窗重开册，拒写并记重开偏了
    r2 = c.post(f"/api/windows/{wid_b}/reopen", headers=h, json={"mode": "增量补丁"})
    assert r2.status_code == 409
    assert _window(c, h, wid_b)["generation"] == 1
    assert _ledger(c, h, wid_b) == []

    audits = c.get("/api/audit", headers=h).json()["items"]
    deviated = [a for a in audits if a["action"] == "reopen_deviated"]
    assert len(deviated) == 2
    assert all("重开偏了" in a["payload"] for a in deviated)

    # 同口径仍放行：当天禁止改口但不禁止同路重开
    r3 = c.post(f"/api/windows/{wid_b}/reopen", headers=h, json={"mode": "作废重算"})
    assert r3.status_code == 200
    assert _window(c, h, wid_b)["generation"] == 2


def test_reopen_then_reclose_materialize_generation_aligned():
    c = TestClient(app)
    h = _auth(c)
    wid = _build_closed_window(c, h, "W-REOPEN-CYCLE", "2026-11-10")

    assert (
        c.post(f"/api/windows/{wid}/reopen", headers=h, json={"mode": "作废重算"}).status_code
        == 200
    )
    # 重开后窗已开启，可入新世代的账
    ing = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-REOPEN-CYCLE",
            "biz_key": "W-REOPEN-CYCLE-E2",
            "event_ts": "2026-11-10T12:00:00",
            "amount": 5.0,
        },
    )
    assert ing.json()["disposition"] == "窗内"
    assert c.post(f"/api/windows/{wid}/close", headers=h).status_code == 200
    mat = c.post(f"/api/windows/{wid}/materialize", headers=h, json={})
    assert mat.status_code == 200

    # 新物化挂新世代，与窗口世代同源
    mats = _mats(c, h, wid)
    active = [m for m in mats if m["status"] == "成功"]
    voided = [m for m in mats if m["status"] == "作废"]
    assert len(active) == 1 and active[0]["generation"] == 2
    assert active[0]["partition_key"].endswith("-g2")
    assert len(voided) == 1 and voided[0]["generation"] == 1

    # 对账互证：只认当前世代的窗内事件与物化
    recon = c.get(f"/api/reconcile/{wid}", headers=h).json()
    assert recon["window"]["generation"] == 2
    assert recon["window_events"] == 1
    assert recon["aligned"] is True
    assert recon["materialized"]["generation"] == 2

    # 重开台账与窗口世代同态
    ledger = _ledger(c, h, wid)
    assert len(ledger) == 1
    assert ledger[0]["from_generation"] == 1 and ledger[0]["to_generation"] == 2


def test_reopen_requires_closed_window():
    c = TestClient(app)
    h = _auth(c)
    w = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-REOPEN-OPEN",
            "open_at": "2026-11-11T00:00:00",
            "close_at": "2026-11-11T23:59:59",
        },
    ).json()
    r = c.post(f"/api/windows/{w['id']}/reopen", headers=h, json={"mode": "作废重算"})
    assert r.status_code == 409
    assert _ledger(c, h, w["id"]) == []
