# -*- coding: utf-8 -*-
"""网页控制台：定时任务修改 + 运行实例开始时间的回归测试。"""
import json
import shutil
import uuid
from pathlib import Path

import pytest

import web_app

ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "web" / "app.js"
INDEX_HTML = ROOT / "web" / "index.html"


@pytest.fixture
def schedule_store(monkeypatch):
    """把定时任务落盘目录挪到临时目录，避免污染 web_data。

    不用 pytest 的 tmp_path：它固定以 0o700 建目录，在部分受限 Windows
    环境里会不可访问；这里用默认权限自建并保证清理。
    """
    base = ROOT / "out"
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / ("_test_schedules_" + uuid.uuid4().hex[:8])
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
        "name": "JW250124 晨跑",
        "time_of_day": "07:30",
        "options": {
            "mode": "dry",
            "project": "/opt/yunForNewVersion/students_cofig/JW250124",
            "student": "JW250124",
            "config": "/opt/yunForNewVersion/students_cofig/JW250124/config.ini",
            "task_folder": "tasks_fch",
            "task_file": "tasklist_1.json",
            "face_student": "JW250124",
            "face_photo": "JW250124_D1.jpg",
            "face_stem": "JW250124_D1",
        },
    }
    body.update(overrides)
    return web_app.create_schedule(body)


def test_update_schedule_edits_name_time_and_options_and_persists(schedule_store):
    item = _create()
    item["last_run"] = "r7"
    item["last_started"] = 123.0
    item["last_error"] = "旧错误"

    updated = web_app.update_schedule({
        "id": item["id"],
        "name": "JW250124 夜跑",
        "time_of_day": "08:15",
        "options": {"mode": "real", "task_file": "tasklist_3.json"},
    })

    assert updated["id"] == item["id"]
    assert updated["name"] == "JW250124 夜跑"
    assert updated["time_of_day"] == "08:15"
    # options 做浅合并：只改传入项，其余保持
    assert updated["options"]["mode"] == "real"
    assert updated["options"]["task_file"] == "tasklist_3.json"
    assert updated["options"]["task_folder"] == "tasks_fch"
    assert updated["options"]["face_photo"] == "JW250124_D1.jpg"
    # 运行历史保留，编辑后清空旧错误
    assert updated["last_run"] == "r7"
    assert updated["last_started"] == 123.0
    assert updated["last_error"] == ""
    assert updated["next_run"] > 0

    persisted = json.loads(
        (schedule_store / "schedules.json").read_text(encoding="utf-8"))
    saved = persisted[item["id"]]
    assert saved["name"] == "JW250124 夜跑"
    assert saved["time_of_day"] == "08:15"
    assert saved["options"]["mode"] == "real"


def test_update_schedule_keeps_unspecified_fields(schedule_store):
    item = _create()
    before = web_app.SCHEDULES[item["id"]]

    updated = web_app.update_schedule({"id": item["id"], "name": "只改名称"})

    assert updated["name"] == "只改名称"
    assert updated["time_of_day"] == "07:30"
    assert updated["options"] == before["options"]
    assert updated["enabled"] is True
    assert updated["created"] == before["created"]


def test_update_schedule_can_toggle_enabled(schedule_store):
    item = _create()
    updated = web_app.update_schedule({"id": item["id"], "enabled": False})
    assert updated["enabled"] is False
    assert web_app.update_schedule({"id": item["id"], "enabled": True})["enabled"] is True


def test_update_schedule_rejects_bad_time_and_missing_id(schedule_store):
    item = _create()

    with pytest.raises(ValueError):
        web_app.update_schedule({"id": item["id"], "time_of_day": "25:00"})
    # 非法时间不能被悄悄写成默认值
    assert web_app.SCHEDULES[item["id"]]["time_of_day"] == "07:30"

    with pytest.raises(ValueError):
        web_app.update_schedule({"id": item["id"], "time_of_day": "not-a-time"})

    with pytest.raises(KeyError):
        web_app.update_schedule({"id": "s404"})


def test_create_schedule_still_normalizes_time(schedule_store):
    item = _create(time_of_day="7:05")
    assert item["time_of_day"] == "07:05"
    assert web_app.list_schedules()[0]["id"] == item["id"]


def test_list_runs_exposes_started_and_ended(monkeypatch):
    monkeypatch.setattr(web_app, "RUNS", {
        "r1": {
            "id": "r1", "argv": ["python", "main.py"], "lines": [],
            "status": "done", "started": 1700000000.0, "ended": 1700000060.0,
            "returncode": 0, "mode": "dry", "task_file": None,
            "task_folder": None, "project": "p", "config": "c",
            "student": "", "note": "", "verdict": "正常结束",
        },
    })
    row = web_app.list_runs()[0]
    assert row["started"] == 1700000000.0
    assert row["ended"] == 1700000060.0
    assert row["elapsed"] == 60.0


def test_run_panels_render_started_time():
    source = APP_JS.read_text(encoding="utf-8")
    assert "function fmtTs(" in source
    # 实例列表带"开始/结束"时间
    assert "开始 ${fmtTs(r.started)}" in source
    assert "结束 ' + fmtTs(r.ended)" in source
    # 输出面板标题也带开始时间
    assert "function setOutputTitle(" in source
    assert "开始 ' + fmtTs(run.started)" in source


def test_schedule_page_supports_editing_saved_tasks():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "action: 'update'" in source
    assert "async function editSchedule(" in source
    assert "async function saveSchedule(" in source
    assert "async function applyScheduleForm(" in source
    assert "function cancelScheduleEdit(" in source
    assert "onclick=\"editSchedule(" in source

    assert 'id="schFormTitle"' in html
    assert 'id="schSave"' in html
    assert 'onclick="saveSchedule()"' in html
    assert 'id="schCancel"' in html
    assert 'onclick="cancelScheduleEdit()"' in html
