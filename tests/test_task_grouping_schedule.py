# -*- coding: utf-8 -*-
"""任务表分组（男组/女组）与定时运行组内随机抽表回归测试。"""
import json
import shutil
import uuid
from pathlib import Path

import pytest

import task_tools
import web_app

ROOT = Path(__file__).parents[1]
TASKS_FCH = ROOT / "tasks_fch"
APP_JS = ROOT / "web" / "app.js"
INDEX_HTML = ROOT / "web" / "index.html"


@pytest.fixture
def schedule_store(monkeypatch):
    """定时任务落盘到隔离临时目录。"""
    base = ROOT / "out"
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / ("_test_schedule_group_" + uuid.uuid4().hex[:8])
    tmp.mkdir()
    monkeypatch.setattr(web_app, "RUN_DATA_DIR", str(tmp))
    monkeypatch.setattr(web_app, "SCHEDULES_FILE", str(tmp / "schedules.json"))
    web_app.SCHEDULES.clear()
    try:
        yield tmp
    finally:
        web_app.SCHEDULES.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_task_group_inference_and_summary():
    """测试男组/女组推断逻辑及 task_summary 注入 group 字段。"""
    # 明确根据里程 < 1.5km 为 female，>= 1.5km 为 male
    assert task_tools.infer_task_group("custom_test.json", mileage_km=1.2) == "female"
    assert task_tools.infer_task_group("custom_test.json", mileage_km=1.8) == "male"
    # 根据文件名推断
    assert task_tools.infer_task_group("female_route.json") == "female"
    assert task_tools.infer_task_group("women_run.json") == "female"
    assert task_tools.infer_task_group("女生任务表.json") == "female"
    assert task_tools.infer_task_group("male_route.json") == "male"

    # tasklist_0 ~ tasklist_7 为男组，tasklist_8 为女组
    assert task_tools.infer_task_group("tasklist_0.json", mileage_km=1.81) == "male"
    assert task_tools.infer_task_group("tasklist_8.json", mileage_km=1.22) == "female"

    # task_summary 包含 group 和 group_label
    s0 = task_tools.task_summary(str(TASKS_FCH / "tasklist_0.json"))
    assert s0["group"] == "male"
    assert s0["group_label"] == "男组"

    s8 = task_tools.task_summary(str(TASKS_FCH / "tasklist_8.json"))
    assert s8["group"] == "female"
    assert s8["group_label"] == "女组"


def test_list_tasks_groups_and_api():
    """测试任务列表与分组摘要。"""
    tasks = web_app.list_tasks_for(str(ROOT), "tasks_fch")
    assert len(tasks) >= 9
    groups = web_app._group_tasks_summary(tasks)
    assert "male" in groups
    assert "female" in groups

    male_files = groups["male"]
    female_files = groups["female"]

    # 验证现有 9 张表的分组归属
    for i in range(8):
        assert f"tasklist_{i}.json" in male_files
    assert "tasklist_8.json" in female_files


def test_schedule_options_and_random_group_pick(schedule_store):
    """测试定时任务配置 task_group 时，_schedule_run_options 动态随机抽表。"""
    # 1. 男组配置
    opts_male = {
        "mode": "dry",
        "project": str(ROOT),
        "task_folder": "tasks_fch",
        "task_file": "male",
        "task_group": "male",
    }
    cleaned = web_app._clean_schedule_options(opts_male)
    assert cleaned["task_group"] == "male"
    assert cleaned["task_file"] == "male"

    # 模拟多次运行抽表，验证均从男组中抽取且各次具备随机性
    male_pool = {f"tasklist_{i}.json" for i in range(8)}
    picked_male_files = set()
    for _ in range(30):
        run_opts = web_app._schedule_run_options(cleaned)
        picked_file = run_opts.get("task_file")
        assert picked_file in male_pool
        assert run_opts["task_group"] == "male"
        assert picked_file != "tasklist_8.json"  # 绝不能抽到女组表
        picked_male_files.add(picked_file)

    # 30 次随机抽取应该覆盖至少 2 种以上不同的男组表
    assert len(picked_male_files) > 1

    # 2. 女组配置
    opts_female = {
        "mode": "dry",
        "project": str(ROOT),
        "task_folder": "tasks_fch",
        "task_file": "female",
        "task_group": "female",
    }
    cleaned_female = web_app._clean_schedule_options(opts_female)
    assert cleaned_female["task_group"] == "female"

    run_opts_f = web_app._schedule_run_options(cleaned_female)
    assert run_opts_f["task_file"] == "tasklist_8.json"
    assert run_opts_f["task_group"] == "female"


def test_schedule_step_and_run_now_records_last_task_file(schedule_store, monkeypatch):
    """测试定时调度与立即运行时，记录每次随机抽中的 last_task_file。"""
    fired = []
    monkeypatch.setattr(web_app, "start_run",
                        lambda opts: fired.append(opts) or {"id": "run_test_id"})

    item = web_app.create_schedule({
        "name": "男组定时测试",
        "time_of_day": "07:30",
        "options": {
            "mode": "dry",
            "project": str(ROOT),
            "task_folder": "tasks_fch",
            "task_file": "male",
            "task_group": "male",
        },
    })

    # 立即运行
    res = web_app.run_schedule_now(item["id"])
    assert res.get("id") == "run_test_id"
    assert len(fired) == 1
    assert item["last_task_file"] in {f"tasklist_{i}.json" for i in range(8)}
    assert fired[0]["task_file"] == item["last_task_file"]

    # 触发定时步进
    item["next_run"] = 1000.0
    web_app._schedule_step(now=1010.0)
    assert len(fired) == 2
    assert item["last_task_file"] in {f"tasklist_{i}.json" for i in range(8)}
    assert fired[1]["task_file"] == item["last_task_file"]


def test_ui_contains_grouping_and_male_female_options():
    """测试前端代码中包含男组/女组选择与组别显示。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    # index.html 中任务表头包含组别，定时运行中包含组别提示
    assert "组别" in html
    assert "schTask" in html

    # app.js 中 fillScheduleTasks 提供男组与女组两个选项
    assert 'value="male"' in js
    assert 'value="female"' in js
    assert "男组" in js
    assert "女组" in js
    assert "setTaskGroup" in js
