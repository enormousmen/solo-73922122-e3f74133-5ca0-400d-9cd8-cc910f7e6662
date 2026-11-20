"""纳秒采样时间的回归测试。

覆盖：
1. 上报 / 查询完整保留最多 9 位小数秒；
2. 晚到的旧报告不得覆盖同微秒内更晚采样的报告；
3. 实际同一时刻才按最后入库决出最新；
4. 结单要求采样**严格晚于**获批（含同一微秒内更晚纳秒）；
5. 晚到的旧报告不得让许可提前结单；
6. 旧 SQLite 库迁移后仍以已保存时刻为准。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402
from app.models import UtcInstant  # noqa: E402

MANIFEST = {
    "shards": {
        "shard-a": ["n1", "n2", "n3"],
        "shard-b": ["n2", "n3", "n4"],
        "shard-c": ["n3", "n4", "n5"],
    }
}
ALL_NODES = ["n1", "n2", "n3", "n4", "n5"]
T0 = UtcInstant.parse("2026-11-19T09:00:00Z")


@pytest.fixture()
def make_client(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(MANIFEST))
    db_path = tmp_path / "permits.db"
    clients = []

    def _make():
        app = create_app(db_path=str(db_path), manifest_path=str(manifest_path))
        c = TestClient(app)
        clients.append(c)
        return c

    yield _make, db_path
    for c in clients:
        c.close()


@pytest.fixture()
def client(make_client):
    return make_client[0]()


def report(client, node, sampled_at, healthy=True, boot="boot-1"):
    return client.post(
        "/liveness",
        json={
            "reports": [
                {
                    "node_id": node,
                    "sampled_at": sampled_at,
                    "healthy": healthy,
                    "boot_id": boot,
                }
            ]
        },
    )


def seed_all_healthy(client, boot="boot-1", sampled_at=T0.to_iso()):
    for n in ALL_NODES:
        assert report(client, n, sampled_at, boot=boot).status_code == 201


def request_permit(client, request_id, nodes):
    return client.post("/permits", json={"request_id": request_id, "nodes": nodes})


# ---------- 时间精度 ----------


def test_sampled_at_preserves_nine_fractional_digits(client):
    text = "2026-11-19T10:00:00.123456789Z"
    assert report(client, "n1", text).status_code == 201
    got = client.get("/liveness/n1").json()
    assert got["sampled_at"] == "2026-11-19T10:00:00.123456789+00:00"
    all_nodes = client.get("/liveness").json()["nodes"]
    assert all_nodes["n1"]["sampled_at"] == "2026-11-19T10:00:00.123456789+00:00"


def test_sampled_at_normalized_to_utc_keeps_nanos(client):
    assert report(client, "n1", "2026-11-19T18:00:00.123456789+08:00").status_code == 201
    assert (
        client.get("/liveness/n1").json()["sampled_at"]
        == "2026-11-19T10:00:00.123456789+00:00"
    )


def test_late_older_report_does_not_override_within_same_microsecond(client):
    # 两份都在同一微秒内，仅纳秒不同；较旧的一份后到。
    assert report(client, "n1", "2026-11-19T10:00:00.123456700Z", boot="newer").status_code == 201
    assert report(client, "n1", "2026-11-19T10:00:00.123456100Z", boot="older").status_code == 201
    got = client.get("/liveness/n1").json()
    assert got["boot_id"] == "newer"
    assert got["sampled_at"] == "2026-11-19T10:00:00.1234567+00:00"


def test_same_instant_takes_last_ingested(client):
    same = "2026-11-19T10:00:00.123456789Z"
    assert report(client, "n1", same, boot="first").status_code == 201
    assert report(client, "n1", same, boot="second").status_code == 201
    assert client.get("/liveness/n1").json()["boot_id"] == "second"


# ---------- 结单 ----------


def test_finish_accepts_sample_one_nanosecond_after_approval(client):
    seed_all_healthy(client)
    r = request_permit(client, "req-1", ["n1"])
    assert r.status_code == 201, r.text
    approved = UtcInstant.parse(r.json()["approved_at"])
    # 同一微秒内、但在获批后 1 纳秒采集：旧实现会截断后判成“未晚于获批”。
    after = UtcInstant(approved.nanos + 1)
    assert report(client, "n1", after.to_iso(), boot="boot-2").status_code == 201
    r = client.post("/permits/req-1/finish")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "finished"


def test_finish_rejects_sample_at_exactly_approval_time(client):
    seed_all_healthy(client)
    r = request_permit(client, "req-1", ["n1"])
    approved_text = r.json()["approved_at"]
    assert report(client, "n1", approved_text, boot="boot-2").status_code == 201
    r = client.post("/permits/req-1/finish")
    assert r.status_code == 409
    assert r.json()["detail"]["nodes"] == {"n1": "not_sampled_after_approval"}


def test_late_stale_report_cannot_finish_permit_early(client):
    """实际最新报告启动标识未变，晚到的更旧报告不得伪造“已重启”。"""
    seed_all_healthy(client)
    r = request_permit(client, "req-1", ["n1"])
    approved = UtcInstant.parse(r.json()["approved_at"])

    # 实际最新：获批后采集，仍为旧启动标识 -> 不应允许结单。
    newer = UtcInstant(approved.nanos + 500)
    assert report(client, "n1", newer.to_iso(), boot="boot-1").status_code == 201
    # 更旧（仍在同一微秒）但 boot 已变的报告后到。
    older = UtcInstant(approved.nanos + 100)
    assert report(client, "n1", older.to_iso(), boot="boot-2").status_code == 201

    r = client.post("/permits/req-1/finish")
    assert r.status_code == 409
    assert r.json()["detail"]["nodes"] == {"n1": "boot_id_unchanged"}
    # 许可仍 open，节点继续被占用。
    assert client.get("/permits/req-1").json()["status"] == "open"
    assert request_permit(client, "req-2", ["n1"]).status_code == 409


def test_finish_rejected_keeps_node_occupied(client):
    seed_all_healthy(client)
    assert request_permit(client, "req-1", ["n1"]).status_code == 201
    assert client.post("/permits/req-1/finish").status_code == 409
    assert client.get("/permits/req-1").json()["status"] == "open"
    r = request_permit(client, "req-2", ["n1"])
    assert r.status_code == 409
    assert r.json()["detail"]["occupied_nodes"] == [{"node": "n1", "permit": "req-1"}]


# ---------- 旧库迁移 ----------


def test_legacy_database_rows_keep_saved_instant(make_client):
    _, db_path = make_client
    # 按旧 schema 建库并写入旧记录（无 sampled_at_ns 列）。
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE liveness_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            sampled_at TEXT NOT NULL,
            healthy INTEGER NOT NULL,
            boot_id TEXT NOT NULL,
            received_at TEXT NOT NULL
        );
        CREATE TABLE permits (
            request_id TEXT PRIMARY KEY,
            nodes TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'finished')),
            approved_at TEXT NOT NULL,
            finished_at TEXT,
            boot_ids_at_approval TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO liveness_reports "
        "(node_id, sampled_at, healthy, boot_id, received_at) VALUES (?,?,?,?,?)",
        ("n1", "2026-11-19T10:00:00.123456+00:00", 1, "legacy-boot",
         "2026-11-19T10:00:01+00:00"),
    )
    conn.execute(
        "INSERT INTO liveness_reports "
        "(node_id, sampled_at, healthy, boot_id, received_at) VALUES (?,?,?,?,?)",
        ("n1", "2026-11-19T10:00:00.123455+00:00", 0, "older-boot",
         "2026-11-19T10:00:02+00:00"),
    )
    conn.commit()
    conn.close()

    c = make_client[0]()
    got = c.get("/liveness/n1").json()
    assert got["boot_id"] == "legacy-boot"
    assert got["healthy"] is True
    assert got["sampled_at"] == "2026-11-19T10:00:00.123456+00:00"
    # 迁移后继续接收 9 位小数秒的新报告。
    assert report(c, "n1", "2026-11-19T10:00:00.123456789Z", boot="new").status_code == 201
    assert c.get("/liveness/n1").json()["sampled_at"] == "2026-11-19T10:00:00.123456789+00:00"
    # 重启后迁移幂等，值不变。
    c.close()
    c2 = make_client[0]()
    assert c2.get("/liveness/n1").json()["boot_id"] == "new"
