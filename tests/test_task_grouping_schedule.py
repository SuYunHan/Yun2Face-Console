# -*- coding: utf-8 -*-
"""任务表手动分组（男组/女组/未分组）与定时运行男组/女组选择及组内随机抽表回归测试。"""
import json
import os
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
    assert '<option value="male">男组' in js
    assert '<option value="female">女组' in js
    assert "setTaskGroup" in js

    # index.html 与 app.js 支持运行时间范围与每周运行日
    assert "schTimeStart" in html
    assert "schTimeEnd" in html
    assert "schTimeStart" in js
    assert "schTimeEnd" in js
    assert "schDay" in html
    assert "schDay" in js
    assert "setScheduleDaysQuick" in js
    assert "getSelectedScheduleDays" in js
    assert "setSelectedScheduleDays" in js
    assert "formatDaysOfWeek" in js


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


def test_days_of_week_normalization_and_scheduling(schedule_store):
    """测试每周运行日配置规范化、调度过滤与周期跳转。"""
    from datetime import datetime, timezone, timedelta
    tz8 = timezone(timedelta(hours=8))

    # 1. 规范化函数测试
    assert web_app._normalize_days_of_week(None) == [1, 2, 3, 4, 5, 6, 7]
    assert web_app._normalize_days_of_week("") == [1, 2, 3, 4, 5, 6, 7]
    assert web_app._normalize_days_of_week([]) == [1, 2, 3, 4, 5, 6, 7]
    assert web_app._normalize_days_of_week([5, 1, 3]) == [1, 3, 5]
    assert web_app._normalize_days_of_week("1, 3, 5") == [1, 3, 5]
    assert web_app._normalize_days_of_week("1-5") == [1, 2, 3, 4, 5]
    assert web_app._normalize_days_of_week("6-7") == [6, 7]
    assert web_app._normalize_days_of_week([0, 6]) == [6, 7]  # 0 映射为周日 7

    # 2. 调度测试：2026-11-18 为周三 (isoweekday = 3)
    wed_0600 = datetime(2026, 11, 18, 6, 0, 0, tzinfo=tz8).timestamp()
    assert datetime.fromtimestamp(wed_0600, tz8).isoweekday() == 3

    # 情况 A：如果允许周三，且 06:00 尚未开始 (07:00-08:00)，调度必须落在今天 (周三)
    next_ts = web_app._next_daily("07:00-08:00", now=wed_0600, days_of_week=[3, 5])
    dt = datetime.fromtimestamp(next_ts, tz8)
    assert dt.isoweekday() == 3
    assert dt.day == 18
    assert 7 <= dt.hour <= 8

    # 情况 B：如果不允许周三（仅允许周一和周五 [1, 5]），即使 06:00 还没到，也必须跳过周三与周四，直接调度到本周五 (2026-11-20)
    next_ts_fri = web_app._next_daily("07:00-08:00", now=wed_0600, days_of_week=[1, 5])
    dt_fri = datetime.fromtimestamp(next_ts_fri, tz8)
    assert dt_fri.isoweekday() == 5
    assert dt_fri.day == 20
    assert 7 <= dt_fri.hour <= 8

    # 情况 C：周三 08:30 已过，允许周三和周五 [3, 5]，必须顺延至本周五 (2026-11-20)
    wed_0830 = datetime(2026, 11, 18, 8, 30, 0, tzinfo=tz8).timestamp()
    next_ts_after = web_app._next_daily("07:00-08:00", now=wed_0830, days_of_week=[3, 5])
    dt_after = datetime.fromtimestamp(next_ts_after, tz8)
    assert dt_after.isoweekday() == 5
    assert dt_after.day == 20
    assert 7 <= dt_after.hour <= 8

    # 情况 D：周五 09:00 已过，允许周一和周三 [1, 3]，必须跨周末顺延到下周一 (2026-11-23)
    fri_0900 = datetime(2026, 11, 20, 9, 0, 0, tzinfo=tz8).timestamp()
    next_ts_next_mon = web_app._next_daily("07:00-08:00", now=fri_0900, days_of_week=[1, 3])
    dt_next_mon = datetime.fromtimestamp(next_ts_next_mon, tz8)
    assert dt_next_mon.isoweekday() == 1
    assert dt_next_mon.day == 23
    assert 7 <= dt_next_mon.hour <= 8

    # 3. 创建与更新 API 的 days_of_week 协同测试
    item = web_app.create_schedule({
        "name": "周二周四定时",
        "time_of_day": "07:00-08:00",
        "days_of_week": [2, 4],
        "options": {"mode": "dry", "task_file": "male"},
    })
    assert item["days_of_week"] == [2, 4]
    local_next = datetime.fromtimestamp(item["next_run"], tz8)
    assert local_next.isoweekday() in [2, 4]
    assert web_app._schedule_next_run_matches(item) is True

    # 更新为仅周日运行 [7]
    updated = web_app.update_schedule({"id": item["id"], "days_of_week": [7]})
    assert updated["days_of_week"] == [7]
    local_sun = datetime.fromtimestamp(updated["next_run"], tz8)
    assert local_sun.isoweekday() == 7
    assert web_app._schedule_next_run_matches(updated) is True


def test_task_randomization_and_group_constraints(tmp_path, schedule_store):
    """测试男组与女组任务表随机化后的指标刚性约束：
    1. 男组：平均步频 60~280 步/min，配速 3~13 min/km，距离 > 1.6km；
    2. 女组：平均步频 60~280 步/min，配速 3~13 min/km，距离 > 1.2km 且 < 1.6km。
    """
    # 1. 针对男组与女组原表多次随机化校验
    t0_path = str(TASKS_FCH / "tasklist_0.json")
    t8_path = str(TASKS_FCH / "tasklist_8.json")

    # 男组随机化测试（50 次随机）
    for seed in range(50):
        out_male = task_tools.randomize_task(
            t0_path, str(tmp_path / f"male_{seed}.json"), group="male", seed=seed * 31 + 7
        )
        data = out_male["data"]
        dist = data["recordMileage"]
        pace = data["recodePace"]
        cadence = data["recodeCadence"]

        assert dist > 1.6, f"男组距离必须 > 1.6km，当前: {dist}"
        assert 3.0 <= pace <= 13.0, f"男组配速必须在 3~13 之间，当前: {pace}"
        assert 60 <= cadence <= 280, f"男组步频必须在 60~280 之间，当前: {cadence}"

        val = task_tools.validate_task(str(tmp_path / f"male_{seed}.json"), group="male")
        assert val["ok"] is True, f"校验失败: {val['errors']}"

    # 女组随机化测试（50 次随机）
    for seed in range(50):
        out_female = task_tools.randomize_task(
            t8_path, str(tmp_path / f"female_{seed}.json"), group="female", seed=seed * 43 + 13
        )
        data = out_female["data"]
        dist = data["recordMileage"]
        pace = data["recodePace"]
        cadence = data["recodeCadence"]

        assert 1.2 < dist < 1.6, f"女组距离必须在 1.2~1.6km 之间，当前: {dist}"
        assert 3.0 <= pace <= 13.0, f"女组配速必须在 3~13 之间，当前: {pace}"
        assert 60 <= cadence <= 280, f"女组步频必须在 60~280 之间，当前: {cadence}"

        val = task_tools.validate_task(str(tmp_path / f"female_{seed}.json"), group="female")
        assert val["ok"] is True, f"校验失败: {val['errors']}"

    # 2. validate_task 针对不合规指标的阻断拦截校验
    fake_bad = {
        "ok": True, "data": {
            "recordMileage": 1.5,   # 男组应 > 1.6
            "recodePace": 2.5,      # 配速 < 3
            "recodeCadence": 50,    # 步频 < 60
            "duration": 500,
            "manageList": [{"point": "1,1", "index": 0}],
            "pointsList": [
                {"point": "1,1", "runMileage": "0", "runTime": "0", "runStep": "0", "speed": 6.0},
                {"point": "1,2", "runMileage": "1500", "runTime": "500", "runStep": "416", "speed": 6.0},
            ]
        }
    }
    bad_p = str(tmp_path / "bad.json")
    with open(bad_p, "w", encoding="utf-8") as f:
        json.dump(fake_bad, f)

    v_bad_male = task_tools.validate_task(bad_p, group="male")
    assert v_bad_male["ok"] is False
    assert any("距离必须 > 1.6km" in e for e in v_bad_male["errors"])
    assert any("平均步频" in e for e in v_bad_male["errors"])
    assert any("配速" in e for e in v_bad_male["errors"])

    # 3. 运行前准备 _prepare_task_dir 在男/女组下的随机化与隔离验证
    dir_m, note_m, pick_m = web_app._prepare_task_dir(str(ROOT), "tasks_fch", "male", "rTESTMALE")
    try:
        assert pick_m == dir_m
        assert "已选定男组任务表" in note_m or "已从男组随机抽取" in note_m
        assert "随机化后指标" in note_m
        # 读取临时目录中实际跑的任务表
        copied_files = [f for f in os.listdir(pick_m) if f.endswith(".json")]
        assert len(copied_files) == 1
        with open(os.path.join(pick_m, copied_files[0]), "r", encoding="utf-8") as f:
            picked_obj = json.load(f)
        d_m = picked_obj["data"]
        assert d_m["recordMileage"] > 1.6
        assert 3.0 <= d_m["recodePace"] <= 13.0
        assert 60 <= d_m["recodeCadence"] <= 280
    finally:
        web_app._cleanup_task_pick(pick_m)

    dir_f, note_f, pick_f = web_app._prepare_task_dir(str(ROOT), "tasks_fch", "female", "rTESTFEMALE")
    try:
        assert pick_f == dir_f
        assert "已选定女组任务表" in note_f or "已从女组随机抽取" in note_f
        assert "随机化后指标" in note_f
        copied_files_f = [f for f in os.listdir(pick_f) if f.endswith(".json")]
        assert len(copied_files_f) == 1
        with open(os.path.join(pick_f, copied_files_f[0]), "r", encoding="utf-8") as f:
            picked_obj_f = json.load(f)
        d_f = picked_obj_f["data"]
        assert 1.2 < d_f["recordMileage"] < 1.6
        assert 3.0 <= d_f["recodePace"] <= 13.0
        assert 60 <= d_f["recodeCadence"] <= 280
    finally:
        web_app._cleanup_task_pick(pick_f)
