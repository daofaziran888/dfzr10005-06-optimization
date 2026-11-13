"""分段扫描的回归测试：断点恢复、失败隔离、缓存复用、并发幂等与历史口径兼容。"""
from __future__ import annotations

import threading
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_CAPACITY_SUMMARIES, SEED_ROWS, SEED_SCAN_BATCHES
from app.services.cost_scan import scan_service
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    # store 是进程级单例，每个用例都恢复到种子基线，避免用例之间的进度互相影响。
    for module, rows in SEED_ROWS.items():
        table = store.rows(module)
        table.clear()
        table.extend(deepcopy(rows))
    store.scan_batches().clear()
    store.scan_batches().extend(deepcopy(SEED_SCAN_BATCHES))
    store.capacity_summaries().clear()
    store.capacity_summaries().extend(deepcopy(SEED_CAPACITY_SUMMARIES))
    return TestClient(app)


def create_scan(client: TestClient, scope: str, **values):
    values = {"分析范围": scope, **values}
    return client.post("/api/cost/scans", json={"values": values})


FLAKY_SCOPE = "数仓明细层 dwd_*、汇总层 dws_* 与归档层 ods_*（含冷分区）"


def test_scan_is_split_by_table_and_partition(client: TestClient):
    response = create_scan(client, "dwd_order_detail 的 2026-09 分区", max_segments=3)
    assert response.status_code == 200
    payload = response.json()
    batch = payload["entry"]
    assert batch["状态"] == "已中断"
    assert batch["进度"]["已完成"] == 3
    assert batch["进度"]["待扫描"] > 0
    assert "断点" in payload["message"]
    keys = [segment["分段键"] for segment in batch["分段"]]
    assert len(keys) == len(set(keys)), "表 + 分区必须唯一，避免并发写回同一个分段键"
    assert all("|dt=" in key for key in keys)


def test_resume_keeps_completed_segments_and_finishes(client: TestClient):
    batch = create_scan(client, "dws_trade_summary 的 2026-08 分区", max_segments=2).json()["entry"]
    done_before = {
        segment["分段键"]: segment.get("完成时间")
        for segment in batch["分段"]
        if segment["状态"] == "已完成"
    }
    while True:
        response = client.post(
            f"/api/cost/scans/{batch['批次编号']}/resume", json={"values": {}}
        ).json()
        batch = response["entry"]
        if batch["进度"]["待扫描"] == 0:
            break
    assert batch["状态"] == "已完成"
    assert batch["已发布摘要"].startswith("CAP-")
    for segment in batch["分段"]:
        if segment["分段键"] in done_before:
            assert segment["完成时间"] == done_before[segment["分段键"]], "成功段不得被续扫覆盖"


def test_failed_segment_is_isolated_and_retryable(client: TestClient):
    response = create_scan(client, FLAKY_SCOPE, report_id=1)
    payload = response.json()
    batch = payload["entry"]
    assert batch["状态"] == "部分失败"
    failed = [segment for segment in batch["分段"] if segment["状态"] == "失败"]
    assert len(failed) == 1
    reason = failed[0]["失败原因"]
    assert reason["错误码"] == "PARTITION_READ_PERMISSION"
    assert "PermissionDenied" in reason["原因"]
    assert "重试失败段" in reason["处理建议"]
    assert reason["可重试"] is True
    # 报告仍在，且未因单个分段失败而丢失原有成本字段。
    report = client.get("/api/cost/1").json()
    assert report["扫描状态"] == "部分失败"
    assert report["存储总量"] and report["冷数据占比"] and report["预计节省"]

    retry = client.post(f"/api/cost/scans/{batch['批次编号']}/retry", json={"values": {}}).json()
    assert retry["entry"]["状态"] == "已完成"
    assert retry["entry"]["汇总发布次数"] == 1
    assert retry["entry"]["已发布摘要"]


def test_repeat_submission_keeps_one_batch_and_no_double_publish(client: TestClient):
    first = create_scan(client, "ads_cost_dashboard 的 2026-09 分区").json()["entry"]
    second = create_scan(client, "ads_cost_dashboard 的 2026-09 分区").json()["entry"]
    assert first["批次编号"] == second["批次编号"]
    assert second["汇总发布次数"] == 1
    assert client.get("/api/cost/scans").json()["total"] == 2  # 种子基线 + 本次


def test_rescan_only_recomputes_segments_hit_by_new_sampling(client: TestClient):
    # 已知范围：采样批次 2 只覆盖两个 ODS 冷分区。
    first = create_scan(client, FLAKY_SCOPE).json()["entry"]
    client.post(f"/api/cost/scans/{first['批次编号']}/retry", json={"values": {}})
    second = create_scan(client, FLAKY_SCOPE, sampling_version=2).json()["entry"]
    assert len(second["本轮重算分段"]) == 2
    assert second["批次编号"] == first["批次编号"]
    assert second["状态"] == "已完成"


def test_cost_0002_historical_numbers_survive_rescan(client: TestClient):
    baseline = client.get("/api/cost/2").json()
    keys = ("分析范围", "存储总量", "冷数据占比", "预计节省", "生成时间")
    before = {key: baseline[key] for key in keys}
    assert before["存储总量"] == "31.72 TB"
    assert before["冷数据占比"] == "77.6%"
    assert before["预计节省"] == "8.61 TB"

    idempotent = create_scan(client, "存储成本优化样例2", report_id=2, sampling_version=1).json()
    assert idempotent["entry"]["批次编号"] == "SCAN-0002"
    assert idempotent["entry"]["汇总发布次数"] == 1
    after = client.get("/api/cost/2").json()
    assert {key: after[key] for key in keys} == before

    summary = client.get("/api/cost/capacity-summaries/CAP-0002").json()
    assert summary["摘要编号"] == "CAP-0002"
    assert summary["更新序号"] == 1
    bench = client.get("/api/benchmark/2").json()
    assert bench["容量摘要编号"] == "CAP-0002"
    assert bench["扫描范围"] == "存储成本优化样例2"


def test_benchmark_reports_show_current_scan_scope(client: TestClient):
    response = client.post(
        "/api/benchmark",
        json={"values": {"报告编号": "BENC-9001", "查询语句": "select 1", "执行耗时": "12ms"}},
    ).json()
    assert response["ok"] is True
    entry_id = response["entry"]["id"]
    entry = client.get(f"/api/benchmark/{entry_id}").json()
    assert entry["引用方式"] == "同步最新扫描"
    assert entry["容量摘要编号"] == "CAP-0002"
    assert entry["扫描范围"] == "存储成本优化样例2"

    batch = create_scan(client, "dim_customer 的 2026-01 分区").json()["entry"]
    summary_id = batch["已发布摘要"]
    assert summary_id, "扫描完成后必须发布容量摘要供性能分析引用"

    # 未显式引用的报告自动同步到最新扫描范围。
    latest = client.get(f"/api/benchmark/{entry_id}").json()
    assert latest["容量摘要编号"] == summary_id
    assert latest["扫描范围"] == "dim_customer 的 2026-01 分区"
    assert latest["扫描存储总量"] == client.get(f"/api/cost/capacity-summaries/{summary_id}").json()["存储总量"]

    # 显式引用历史摘要时展示历史范围，不被最新扫描覆盖。
    explicit = client.post(
        "/api/benchmark",
        json={
            "values": {
                "报告编号": "BENC-9002",
                "查询语句": "select 2",
                "执行耗时": "30ms",
                "容量摘要编号": "CAP-0002",
            }
        },
    ).json()
    pinned = client.get(f"/api/benchmark/{explicit['entry']['id']}").json()
    assert pinned["引用方式"] == "显式引用"
    assert pinned["扫描范围"] == "存储成本优化样例2"

    rows = client.get("/api/benchmark?size=50").json()["items"]
    target = next(row for row in rows if row["报告编号"] == "BENC-9001")
    assert target["扫描范围"] == "dim_customer 的 2026-01 分区"


def test_benchmark_unknown_summary_is_rejected(client: TestClient):
    response = client.post(
        "/api/benchmark",
        json={
            "values": {
                "报告编号": "BENC-9003",
                "查询语句": "select 3",
                "执行耗时": "40ms",
                "容量摘要编号": "CAP-9999",
            }
        },
    ).json()
    assert response["ok"] is False
    assert "CAP-9999" in response["message"]


def test_empty_capacity_summary_state(client: TestClient):
    for summary in list(store.capacity_summaries()):
        store.capacity_summaries().remove(summary)
    items = client.get("/api/cost/capacity-summaries").json()
    assert items["items"] == []
    # 显式引用 CAP-0002 的 BENC-0002 给出失效提示，未引用的报告显示暂无数据。
    stale = client.get("/api/benchmark/2").json()
    assert stale["扫描状态"] == "引用的容量摘要 CAP-0002 已失效"
    fresh = client.get("/api/benchmark?size=50").json()["items"]
    unreferenced = next(row for row in fresh if not row.get("容量摘要编号"))
    assert unreferenced["扫描状态"] == "暂无数据"
    assert unreferenced["扫描范围"] == "未引用容量摘要"
    response = create_scan(client, "空范围占位").json()
    assert response["ok"] is False
    assert "暂无数据" in response["message"]


def test_empty_scope_returns_actionable_message(client: TestClient):
    response = create_scan(client, "   ").json()
    assert response["ok"] is False
    assert "分析范围为空" in response["message"]


def test_unknown_scope_has_no_segments(client: TestClient):
    response = create_scan(client, "空范围占位").json()
    assert response["ok"] is False
    assert "暂无数据" in response["message"]


def test_stale_claim_returns_to_pending_and_is_recoverable(client: TestClient):
    batch = create_scan(client, "ods_event_log 的 2026-07 分区", max_segments=1).json()["entry"]
    stored = scan_service.get_scan(batch["批次编号"])
    stuck = next(seg for seg in stored["分段"] if seg["状态"] == "待扫描")
    # 直接改内存里的批次，模拟执行线程被中断：认领时间停留在很久以前。
    live = next(
        seg
        for seg in store.scan_batches()
        if seg["批次编号"] == batch["批次编号"]
    )["分段"]
    target = next(seg for seg in live if seg["分段键"] == stuck["分段键"])
    target["状态"] = "扫描中"
    target["认领时间"] = "2020-01-01 00:00:00"
    target["认领标识"] = "dead-thread"

    resumed = client.post(f"/api/cost/scans/{batch['批次编号']}/resume", json={"values": {}}).json()
    final = resumed["entry"]
    recovered = next(seg for seg in final["分段"] if seg["分段键"] == target["分段键"])
    assert recovered["中断次数"] >= 1
    assert recovered["状态"] in {"已完成", "失败"}
    assert final["状态"] != "扫描中"


def test_rebinding_published_scope_to_other_report_is_rejected(client: TestClient):
    response = create_scan(client, "存储成本优化样例2", report_id=1).json()
    assert response["ok"] is False
    # 历史批次的摘要编号被 BENC-0002/COST-0002 引用，不能换绑到其他报告。
    assert "已绑定报告 COST-0002" in response["message"]
    assert client.get("/api/cost/2").json()["容量摘要编号"] == "CAP-0002"


def test_partial_scan_does_not_publish_stale_summary(client: TestClient):
    batch = create_scan(client, FLAKY_SCOPE, report_id=1).json()["entry"]
    assert batch["状态"] == "部分失败"
    assert batch["已发布摘要"] == ""
    assert client.get("/api/cost/capacity-summaries").json()["total"] == 1  # 只剩历史 CAP-0002
    report = client.get("/api/cost/1").json()
    # 未完成扫描时报告保留原始字段，不能被半份结果覆盖。
    assert report["存储总量"] == "存储成本优化样例1"
    assert report["冷数据占比"] == "存储成本优化样例1"
    assert report["预计节省"] == "存储成本优化样例1"


def test_concurrent_submissions_share_one_batch_and_publish_once():
    scope = "并发测试范围 concurrency_*"
    for batch in list(store.scan_batches()):
        if batch.get("分析范围") == scope:
            store.scan_batches().remove(batch)

    def submit() -> None:
        scan_service.create_scan(scope=scope, sampling_version=1)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    matches = [batch for batch in store.scan_batches() if batch.get("分析范围") == scope]
    assert len(matches) == 1
    batch = matches[0]
    assert batch["汇总发布次数"] == 1, "并发争抢结束段时预计节省只能写入一次"
    assert batch["进度"]["已完成"] == batch["进度"]["总数"]
    assert not [seg for seg in batch["分段"] if seg["状态"] == "扫描中"]
    summaries = [s for s in store.capacity_summaries() if s["扫描范围"] == scope]
    assert len(summaries) == 1
