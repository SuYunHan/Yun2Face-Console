# -*- coding: utf-8 -*-
"""网页控制台：定时任务补跑策略（跳过过期/启用重排）与单表锁定回归测试。"""
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import web_app

ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "web" / "app.js"
INDEX_HTML = ROOT / "web" / "index.html"


@pytest.fixture
def schedule_store(monkeypatch):
    """定时任务落盘到临时目录；用默认权限自建目录，保证受限 Windows 环境可清理。"""
    base = ROOT / "out"
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / ("_test_schedule_policy_" + uuid.uuid4().hex[:8])
    tmp.mkdir()
    monkeypatch.setattr(web_app, "RUN_DATA_DIR", str(tmp))
    monkeypatch.setattr(web_app, "SCHEDULES_FILE", str(tmp / "schedules.json"))
    web_app.SCHEDULES.clear()
    try:
        yield tmp
    finally:
        web_app.SCHEDULES.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _create(**overrides):
    body = {
        "name": "TEST_STUDENT 晨跑",
        "time_of_day": "07:30",
        "options": {"mode": "dry", "project": "/tmp/proj",
                    "task_folder": "tasks_fch", "task_file": "tasklist_1.json"},
    }
    body.update(overrides)
    return web_app.create_schedule(body)


def test_overdue_beyond_grace_is_skipped_not_run(schedule_store, monkeypatch):
    item = _create()
    fired = []
    monkeypatch.setattr(web_app, "start_run",
                        lambda opts: fired.append(opts) or {"id": "rX"})

    late = item["next_run"] + web_app.SCHEDULE_GRACE_SEC + 60
    changed = web_app._schedule_step(now=late)
    web_app._persist_schedules()               # 循环里 changed 才落盘，这里模拟一次

    assert changed == 1
    assert fired == []                        # 绝不能补跑
    assert item["skipped_count"] == 1
    assert item["last_skipped"] == late
    assert item["next_run"] > late            # 顺延到下一次
    assert item.get("last_run") is None
    saved = json.loads((schedule_store / "schedules.json").read_text(encoding="utf-8"))
    assert saved[item["id"]]["skipped_count"] == 1


def test_within_grace_still_runs(schedule_store, monkeypatch):
    item = _create()
    fired = []
    monkeypatch.setattr(web_app, "start_run",
                        lambda opts: fired.append(opts) or {"id": "r1"})

    due = item["next_run"]
    changed = web_app._schedule_step(now=due + 60)

    assert changed == 1
    assert len(fired) == 1
    assert item["last_run"] == "r1"
    assert item["last_error"] == ""
    assert item["next_run"] > due + 60        # 顺延到下一次
    assert item.get("skipped_count") is None


def test_disabled_schedule_is_ignored(schedule_store, monkeypatch):
    item = _create()
    item["enabled"] = False
    fired = []
    monkeypatch.setattr(web_app, "start_run", lambda opts: fired.append(opts) or {"id": "rX"})
    before = item["next_run"]

    assert web_app._schedule_step(now=before + 7 * 86400) == 0
    assert fired == []
    assert item["next_run"] == before


def test_enable_recomputes_next_run_without_catch_up(schedule_store):
    item = _create()
    now = time.time()
    item["enabled"] = False
    item["next_run"] = now - 2 * 86400     # 模拟停用了两天，next_run 早已过期

    enabled = web_app.set_schedule_enabled(item["id"], True)

    assert enabled["enabled"] is True
    assert enabled["next_run"] > now       # 直接排到下一个未来时间，不补跑
    assert enabled.get("skipped_count") is None
    assert enabled["last_run"] is None


def test_disable_keeps_next_run(schedule_store):
    item = _create()
    item["next_run"] = 123.0
    disabled = web_app.set_schedule_enabled(item["id"], False)
    assert disabled["enabled"] is False
    assert disabled["next_run"] == 123.0


def test_unknown_schedule_toggle_raises(schedule_store):
    with pytest.raises(KeyError):
        web_app.set_schedule_enabled("s404", True)


def _make_project(tmp: Path, files):
    proj = tmp / "proj"
    tasks = proj / "tasks_test"
    tasks.mkdir(parents=True)
    for name in files:
        (tasks / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    return proj, tasks


def test_selected_task_file_is_copied_to_isolated_dir(schedule_store):
    proj, tasks = _make_project(schedule_store, ["a.json", "b.json"])
    pick = None
    try:
        task_dir, note, pick = web_app._prepare_task_dir(
            str(proj), "tasks_test", "b.json", "rPICK1")
        assert pick == task_dir
        assert os.path.isdir(pick)
        assert os.listdir(pick) == ["b.json"]          # 目录里只有选中的表
        assert (Path(pick) / "b.json").read_bytes() == (tasks / "b.json").read_bytes()
        assert "已锁定任务表 b.json" in note
    finally:
        web_app._cleanup_task_pick(pick)
    assert not Path(web_app._task_pick_dir("rPICK1")).exists()


def test_no_task_file_keeps_dir_and_says_random(schedule_store):
    proj, tasks = _make_project(schedule_store, ["a.json", "b.json"])
    task_dir, note, pick = web_app._prepare_task_dir(str(proj), "tasks_test", "", "rPICK2")
    assert pick is None
    assert Path(task_dir).resolve() == tasks.resolve()
    assert "随机挑选" in note


def test_single_file_dir_has_no_pick_copy(schedule_store):
    proj, tasks = _make_project(schedule_store, ["only.json"])
    task_dir, note, pick = web_app._prepare_task_dir(str(proj), "tasks_test", "only.json", "rPICK3")
    assert pick is None
    assert Path(task_dir).resolve() == tasks.resolve()
    assert note == ""
    assert not Path(web_app._task_pick_dir("rPICK3")).exists()


def test_unknown_task_file_rejected(schedule_store):
    proj, _ = _make_project(schedule_store, ["a.json", "b.json"])
    with pytest.raises(ValueError):
        web_app._prepare_task_dir(str(proj), "tasks_test", "missing.json", "rPICK4")


def test_edit_name_keeps_imminent_next_run(schedule_store):
    item = _create()
    item["next_run"] = time.time() - 60       # 宽限期内，马上就该跑
    keep = item["next_run"]

    updated = web_app.update_schedule({"id": item["id"], "name": "只改名"})

    assert updated["next_run"] == keep         # 改名不能把这次运行挤掉


def test_edit_name_rolls_stale_next_run_forward(schedule_store):
    item = _create()
    item["next_run"] = time.time() - 3 * 86400

    updated = web_app.update_schedule({"id": item["id"], "name": "只改名"})

    assert updated["next_run"] > time.time()   # 已过期的安排顺延到下一次


def test_next_daily_always_uses_beijing_timezone():
    """无论服务器本地时区是什么，07:10 都指北京时间 07:10。"""
    tz8 = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 22, 0, 0, 0, tzinfo=tz8).timestamp()
    assert web_app._next_daily("07:10", now) == \
        datetime(2026, 10, 22, 7, 10, 0, tzinfo=tz8).timestamp()

    late = datetime(2026, 10, 22, 7, 11, 0, tzinfo=tz8).timestamp()
    assert web_app._next_daily("07:10", late) == \
        datetime(2026, 10, 23, 7, 10, 0, tzinfo=tz8).timestamp()


def test_schedule_next_run_matches_detects_wrong_timezone():
    tz8 = timezone(timedelta(hours=8))
    item = {"time_of_day": "07:10"}
    # 旧版本在 UTC 服务器上把 07:10 存成了 UTC 07:10（=北京 15:10）→ 不匹配
    item["next_run"] = datetime(2026, 10, 22, 7, 10, tzinfo=timezone.utc).timestamp()
    assert web_app._schedule_next_run_matches(item) is False
    # 北京时间 07:10 → 匹配
    item["next_run"] = datetime(2026, 10, 22, 7, 10, tzinfo=tz8).timestamp()
    assert web_app._schedule_next_run_matches(item) is True


def test_load_schedules_repairs_utc_computed_next_run(schedule_store):
    tz8 = timezone(timedelta(hours=8))
    raw = {
        "s1": {
            "id": "s1", "name": "晨跑", "time_of_day": "07:10", "enabled": True,
            "created": 1.0,
            # 模拟“服务器是 UTC 时算出来的 07:10”，实际是北京时间 15:10
            "next_run": datetime(2026, 10, 22, 7, 10, tzinfo=timezone.utc).timestamp(),
            "options": {"mode": "dry"}, "last_run": None,
            "last_started": None, "last_error": "",
        },
    }
    (schedule_store / "schedules.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    web_app.SCHEDULES.clear()

    web_app._load_schedules()

    item = web_app.SCHEDULES["s1"]
    local = datetime.fromtimestamp(item["next_run"], tz8)
    assert (local.hour, local.minute) == (7, 10)      # 已纠正为北京时间 07:10
    saved = json.loads((schedule_store / "schedules.json").read_text(encoding="utf-8"))
    assert saved["s1"]["next_run"] == item["next_run"]  # 纠正结果已落盘


def test_timezone_info_reports_beijing():
    info = web_app.schedule_timezone_info()
    assert info["offset_minutes"] == 480
    assert info["label"] == "UTC+08:00"
    assert isinstance(info["matches_server"], bool)


def test_ui_shows_schedule_timezone():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "fmtTsIn" in source
    assert "time_zone" in source
    assert "renderScheduleTz" in source
    assert 'id="schTZ"' in html


def test_ui_communicates_no_catch_up_and_skip_count():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "已跳过" in source
    assert "skipped_count" in source
    assert "不补跑" in html
