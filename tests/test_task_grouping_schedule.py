# -*- coding: utf-8 -*-
"""任务表手动分组（男组/女组/未分组）与定时运行男组/女组选择及组内随机抽表回归测试。"""
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


def test_manual_task_group_and_no_auto_inference(tmp_path):
    """测试取消自动推断，纯手动分组体系。"""
    # 1. 未手动配置的任务表，哪怕里程 < 1.5km 或文件名带 female，绝不会被自动划分组别
    assert task_tools.get_task_group("unassigned_route.json", groups_map={}) == ""
    assert task_tools.get_task_group({"data": {"recordMileage": 1.2}}, groups_map={}) == ""
    assert task_tools.get_task_group("female_run.json", groups_map={}) == ""
    assert task_tools.get_task_group("女生任务.json", groups_map={}) == ""
    assert task_tools.task_group_label("") == "未分组"

    # 2. 手动持久化分组测试
    fake_groups_file = str(tmp_path / "task_groups.json")
    fake_task_file = str(tmp_path / "custom.json")
    with open(fake_task_file, "w", encoding="utf-8") as f:
        json.dump({"data": {"pointsList": [{"point": "1,1", "runMileage": 1200}], "recordMileage": 1.2}}, f)

    # 手动设为女组
    res = task_tools.set_task_group(fake_task_file, "female", groups_file=fake_groups_file)
    assert res["group"] == "female"
    assert res["group_label"] == "女组"
    assert task_tools.get_task_group(filename="custom.json", groups_map=task_tools.load_task_groups_map(fake_groups_file)) == "female"

    # 手动设为男组
    res2 = task_tools.set_task_group(fake_task_file, "male", groups_file=fake_groups_file)
    assert res2["group"] == "male"
    assert res2["group_label"] == "男组"

    # 手动取消分组
    res3 = task_tools.set_task_group(fake_task_file, "", groups_file=fake_groups_file)
    assert res3["group"] == ""
    assert res3["group_label"] == "未分组"

    # 3. 验证现有 tasks_fch 下的任务表手动分组状态：tasklist_0~7 为男组，tasklist_8 为女组
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

    # 验证现有 9 张表的手动分组归属
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

    # app.js 中 fillScheduleTasks 提供清晰的男组与女组两个选项
    assert '<option value="male">男组</option>' in js
    assert '<option value="female">女组</option>' in js
    assert "setTaskGroup" in js

    # index.html 与 app.js 支持运行时间范围
    assert "schTimeStart" in html
    assert "schTimeEnd" in html
    assert "schTimeStart" in js
    assert "schTimeEnd" in js


def test_time_range_validation_and_scheduling(schedule_store):
    """测试运行时间范围格式校验、随机时刻调度与兼容性。"""
    from datetime import datetime, timezone, timedelta
    tz8 = timezone(timedelta(hours=8))

    # 1. 格式校验
    assert web_app._validate_time_of_day("07:00-08:00") == "07:00-08:00"
    assert web_app._validate_time_of_day("7:30~8:45") == "07:30-08:45"
    assert web_app._validate_time_of_day("07:30") == "07:30"
    with pytest.raises(ValueError):
        web_app._validate_time_of_day("25:00-08:00")
    with pytest.raises(ValueError):
        web_app._validate_time_of_day("07:00-24:00")

    # 2. 时间范围内随机挑选调度点
    # 设定基准时间为北京时间某天清晨 06:00（时间窗 07:00-08:00 尚未开始）
    base_ts = datetime(2026, 11, 18, 6, 0, 0, tzinfo=tz8).timestamp()
    picked_times = set()
    for _ in range(30):
        next_ts = web_app._next_daily("07:00-08:00", now=base_ts)
        local_dt = datetime.fromtimestamp(next_ts, tz8)
        assert local_dt.year == 2026
        assert local_dt.month == 11
        assert local_dt.day == 18
        # 时间必须在 07:00 到 08:00 之间
        assert 7 <= local_dt.hour <= 8
        if local_dt.hour == 8:
            assert local_dt.minute == 0
        picked_times.add(local_dt.strftime("%H:%M:%S"))

    # 30 次抽取应该产生多个不同的随机时刻（非固定死时间）
    assert len(picked_times) > 1

    # 3. 时间窗口已过去时，调度至明天窗口内的随机时刻
    past_ts = datetime(2026, 11, 18, 8, 30, 0, tzinfo=tz8).timestamp()
    next_tomorrow = web_app._next_daily("07:00-08:00", now=past_ts)
    tom_dt = datetime.fromtimestamp(next_tomorrow, tz8)
    assert tom_dt.day == 19
    assert 7 <= tom_dt.hour <= 8

    # 4. 创建带有时间范围的定时任务
    item = web_app.create_schedule({
        "name": "范围测试",
        "time_of_day": "07:00-08:00",
        "options": {"mode": "dry", "task_file": "male"},
    })
    assert item["time_of_day"] == "07:00-08:00"
    assert web_app._schedule_next_run_matches(item) is True
