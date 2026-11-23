# -*- coding: utf-8 -*-
"""运行实例日志：每月一个目录，每天一个文件。"""
import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

import web_app

ROOT = Path(__file__).parents[1]


def _isolate(monkeypatch):
    base = ROOT / "out"
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / ("_test_run_store_" + uuid.uuid4().hex[:8])
    tmp.mkdir()
    monkeypatch.setattr(web_app, "RUN_DATA_DIR", str(tmp))
    monkeypatch.setattr(web_app, "SCHEDULE_TZ", timezone(timedelta(hours=8)))
    saved_runs = dict(web_app.RUNS)
    saved_seq = web_app._RUN_SEQ
    web_app.RUNS.clear()
    return tmp, saved_runs, saved_seq


def _restore(tmp, saved_runs, saved_seq):
    web_app.RUNS.clear()
    web_app.RUNS.update(saved_runs)
    web_app._RUN_SEQ = saved_seq
    shutil.rmtree(tmp, ignore_errors=True)


def _ts(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=8))).timestamp()


def test_legacy_runs_file_splits_into_month_folders(monkeypatch):
    tmp, saved_runs, saved_seq = _isolate(monkeypatch)
    try:
        legacy = {
            "r1": {"id": "r1", "status": "done", "mode": "dry", "started": _ts(2026, 11, 18, 23, 30),
                   "lines": ["晚上"], "returncode": 0},
            "r2": {"id": "r2", "status": "done", "mode": "dry", "started": _ts(2026, 11, 19, 0, 10),
                   "lines": ["次日"], "returncode": 0},
            "r3": {"id": "r3", "status": "running", "mode": "dry", "started": None,
                   "lines": ["没时间"], "returncode": None},
        }
        # 北京时间 11-19 00:30，对应 UTC 仍是 11-18，必须进 19 日的文件。
        utc_evening = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc).timestamp()
        legacy["r4"] = {"id": "r4", "status": "done", "mode": "real", "started": utc_evening,
                        "lines": ["跨日"], "returncode": 0}
        (tmp / "runs.json").write_text(json.dumps(legacy), encoding="utf-8")
        (tmp / "schedules.json").write_text("{}", encoding="utf-8")

        web_app._load_persisted_runs()

        assert not (tmp / "runs.json").exists()
        assert (tmp / "schedules.json").read_text(encoding="utf-8") == "{}"
        day18 = json.loads((tmp / "2026-11" / "2026-11-18.json").read_text(encoding="utf-8"))
        day19 = json.loads((tmp / "2026-11" / "2026-11-19.json").read_text(encoding="utf-8"))
        unknown = json.loads((tmp / "unknown" / "unknown.json").read_text(encoding="utf-8"))
        assert list(day18) == ["r1"]
        assert set(day19) == {"r2", "r4"}
        assert list(unknown) == ["r3"]
        assert web_app.RUNS["r3"]["status"] == "stopped"
        assert web_app.RUNS["r3"]["verdict"] == "网页服务重启时中断"
        by_id = {row["id"]: row["day"] for row in web_app.list_runs()}
        assert by_id == {"r1": "2026-11-18", "r2": "2026-11-19", "r3": "", "r4": "2026-11-19"}

        web_app.RUNS["r9"] = {
            "id": "r9", "status": "running", "mode": "real", "started": _ts(2026, 11, 19, 8),
            "lines": ["进行中"], "returncode": None, "proc": object(),
        }
        web_app._persist_run(web_app.RUNS["r9"])
        saved = json.loads((tmp / "2026-11" / "2026-11-19.json").read_text(encoding="utf-8"))
        assert saved["r9"]["lines"] == ["进行中"]
        assert "proc" not in saved["r9"]
        assert "r2" in saved

        web_app.clear_finished_runs()
        assert set(web_app.RUNS) == {"r9"}
        assert not (tmp / "2026-11" / "2026-11-18.json").exists()
        assert not (tmp / "unknown").exists()
        left = json.loads((tmp / "2026-11" / "2026-11-19.json").read_text(encoding="utf-8"))
        assert set(left) == {"r9"}
        assert (tmp / "schedules.json").read_text(encoding="utf-8") == "{}"
    finally:
        _restore(tmp, saved_runs, saved_seq)


@pytest.fixture
def run_store(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "RUN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(web_app, "SCHEDULE_TZ", timezone(timedelta(hours=8)))
    monkeypatch.setattr(web_app, "RUNS", {})
    monkeypatch.setattr(web_app, "_RUN_SEQ", count(1))
    return tmp_path


def _finished_run(rid, started, *, lines=None):
    return {"id": rid, "started": started, "status": "done", "mode": "dry",
            "returncode": 0, "lines": lines or []}


def test_corrupt_legacy_log_is_kept(run_store):
    legacy = run_store / "runs.json"
    broken = '{"r1":{"status":"done"},"r2":'
    legacy.write_text(broken, encoding="utf-8")

    with pytest.raises(ValueError):
        web_app._load_persisted_runs()

    assert legacy.read_text(encoding="utf-8") == broken
    assert list(run_store.iterdir()) == [legacy]


def test_failed_migration_stops_startup_and_can_be_retried(run_store, monkeypatch):
    legacy = run_store / "runs.json"
    records = {"r1": _finished_run("r1", _ts(2026, 11, 19, 12)),
               "r2": _finished_run("r2", _ts(2026, 11, 20, 12))}
    legacy.write_text(json.dumps(records), encoding="utf-8")
    write_run_file = web_app._write_run_file

    def fail_second_day(path, records):
        if Path(path).name == "2026-11-20.json":
            raise OSError("second day write failed")
        write_run_file(path, records)

    with monkeypatch.context() as patch:
        patch.setattr(web_app, "_write_run_file", fail_second_day)
        with pytest.raises(OSError, match="second day write failed"):
            web_app._load_persisted_runs()

    assert json.loads(legacy.read_text(encoding="utf-8")) == records
    assert not web_app.RUNS
    web_app._load_persisted_runs()
    assert set(web_app.RUNS) == {"r1", "r2"}
    assert next(web_app._RUN_SEQ) == 3
    assert not legacy.exists()


def test_migration_keeps_conflicting_records(run_store):
    started = _ts(2026, 11, 20, 8)
    legacy = run_store / "runs.json"
    legacy.write_text(json.dumps({"r1": _finished_run("r1", started, lines=["old"])}), encoding="utf-8")
    run = _finished_run("r1", started, lines=["new"])
    web_app.RUNS["r1"] = run
    web_app._persist_run(run)
    daily = run_store / "2026-11" / "2026-11-20.json"
    web_app.RUNS.clear()

    with pytest.raises(ValueError, match="r1"):
        web_app._load_persisted_runs()

    assert json.loads(legacy.read_text(encoding="utf-8"))["r1"]["lines"] == ["old"]
    assert json.loads(daily.read_text(encoding="utf-8"))["r1"]["lines"] == ["new"]


def test_timezone_change_keeps_existing_log_in_its_original_file(run_store, monkeypatch):
    run = _finished_run("r7", _ts(2026, 11, 20, 2))
    web_app.RUNS["r7"] = run
    web_app._persist_run(run)
    original_path = run_store / "2026-11" / "2026-11-20.json"
    assert original_path.is_file()

    monkeypatch.setattr(web_app, "SCHEDULE_TZ", timezone.utc)
    web_app.RUNS.clear()
    web_app._load_persisted_runs()
    loaded = web_app.RUNS["r7"]
    loaded["lines"] = ["updated"]
    web_app._persist_run(loaded)
    web_app.RUNS.clear()
    web_app._load_persisted_runs()

    assert list(run_store.glob("*/*.json")) == [original_path]
    assert web_app.RUNS["r7"]["lines"] == ["updated"]


def test_clear_keeps_record_visible_if_log_cannot_be_deleted(run_store, monkeypatch):
    run = _finished_run("r8", _ts(2026, 11, 20, 2))
    web_app.RUNS["r8"] = run
    web_app._persist_run(run)

    def fail_to_remove(path):
        raise PermissionError("cannot remove daily log")

    with monkeypatch.context() as patch:
        patch.setattr(web_app.os, "remove", fail_to_remove)
        with pytest.raises(PermissionError, match="cannot remove daily log"):
            web_app.clear_finished_runs()
        assert "r8" in web_app.RUNS

    web_app.clear_finished_runs()
    web_app.RUNS.clear()
    web_app._load_persisted_runs()
    assert not web_app.RUNS


def test_late_persist_does_not_restore_cleared_record(run_store):
    run = _finished_run("r8", _ts(2026, 11, 20, 2))
    web_app.RUNS["r8"] = run
    web_app._persist_run(run)

    web_app.clear_finished_runs()
    web_app._persist_run(run)
    web_app._load_persisted_runs()

    assert not web_app.RUNS


def test_run_list_api_exposes_grouping_timezone(run_store, monkeypatch):
    monkeypatch.setattr(web_app, "SCHEDULE_TZ_OFFSET_MIN", 480)
    data = web_app.app.test_client().get("/api/runs").json
    assert data.get("time_zone", {}).get("offset_minutes") == 480
