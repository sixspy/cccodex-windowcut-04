"""红测一：已关窗重开后世代与物化错代的夹具复现。

夹具：关窗且已物化的窗口（generation=1，物化单挂 g1，窗内事件有效）。
现状缺陷：reopen_svc 仅空桩登记——台账写了 to_generation 却不动窗口世代、
不作废旧物化、不作废窗内事件，世代账与对账页分裂。
正确口径（甲路径·作废重算）：重开同事务作废旧世代物化与窗内事件、
generation+1，重开台账/窗口世代/物化列表/对账互证/审计五处同态。
"""
import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "reopen_gen.db")
    init_db()
    yield


def _auth(c: TestClient, username="admin", password="123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _build_closed_materialized_window(c: TestClient, h: dict, name: str) -> int:
    """夹具：开窗 → 入两笔窗内事件 → 关窗 → 物化，返回窗口 id。"""
    w = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": name,
            "open_at": "2026-11-05T00:00:00",
            "close_at": "2026-11-05T23:59:59",
        },
    ).json()
    wid = w["id"]
    for i in (1, 2):
        r = c.post(
            "/api/events",
            headers=h,
            json={
                "window_name": name,
                "biz_key": f"{name}-E{i}",
                "event_ts": f"2026-11-05T1{i}:00:00",
                "amount": 10.0 * i,
            },
        )
        assert r.json()["disposition"] == "窗内"
    assert c.post(f"/api/windows/{wid}/close", headers=h).status_code == 200
    mat = c.post(f"/api/windows/{wid}/materialize", headers=h, json={})
    assert mat.status_code == 200
    return wid


def test_reopen_void_recompute_generation_and_materialization_same_source():
    c = TestClient(app)
    h = _auth(c)
    wid = _build_closed_materialized_window(c, h, "W-REOPEN-G")

    # 夹具自检：重开前五处一致（g1 物化成功、对账对齐）
    before = c.get(f"/api/windows/{wid}", headers=h).json()["window"]
    assert before["generation"] == 1 and before["status"] == "已关闭"
    assert c.get(f"/api/reconcile/{wid}", headers=h).json()["aligned"] is True

    # 重开：甲路径·作废重算
    r = c.post(f"/api/windows/{wid}/reopen", headers=h, json={"mode": "作废重算"})
    assert r.status_code == 200
    body = r.json()
    assert body["from_generation"] == 1 and body["to_generation"] == 2

    # 窗口世代：抬升到 2 且窗重新开启
    after = c.get(f"/api/windows/{wid}", headers=h).json()["window"]
    assert after["generation"] == 2
    assert after["status"] == "开启"

    # 重开台账：可查且与窗口世代同源
    ledger = [
        x
        for x in c.get("/api/reopen-ledger", headers=h).json()["items"]
        if x["window_id"] == wid
    ]
    assert len(ledger) == 1
    assert ledger[0]["from_generation"] == 1
    assert ledger[0]["to_generation"] == 2
    assert ledger[0]["mode"] == "作废重算"
    assert ledger[0]["status"] == "已重开"

    # 物化列表：旧世代物化已作废，不再挂当前世代
    mats = [
        m
        for m in c.get("/api/materializations", headers=h).json()["items"]
        if m["window_id"] == wid
    ]
    assert len(mats) == 1
    assert mats[0]["status"] == "作废"
    assert mats[0]["generation"] == 1

    # 窗内事件：旧世代事件已作废
    evs = [
        e
        for e in c.get("/api/events", headers=h).json()["items"]
        if e["window_id"] == wid
    ]
    assert evs and all(e["disposition"] == "作废" for e in evs)

    # 对账互证：按当前世代取数，旧物化不再冒充当世代的账
    recon = c.get(f"/api/reconcile/{wid}", headers=h).json()
    assert recon["window"]["generation"] == 2
    assert recon["window_events"] == 0
    assert recon["materialized"] is None

    # 审计：重开留痕且世代与台账一致
    audits = c.get("/api/audit", headers=h).json()["items"]
    reopen_ok = [a for a in audits if a["action"] == "reopen_ok"]
    assert reopen_ok, "审计缺 reopen_ok"
    assert '"to_generation": 2' in reopen_ok[0]["payload"]
