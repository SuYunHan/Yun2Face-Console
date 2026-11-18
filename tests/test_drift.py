import copy
import json
import math
import os
import pytest

from tools.drift import add_drift, haversine_distance, split_data, load_json


@pytest.fixture
def sample_task():
    return {
        "msg": "success",
        "code": 200,
        "data": {
            "duration": 600,
            "recordMileage": "1.20",
            "recodeCadence": "150",
            "recodePace": "5.50",
            "pointsList": [
                {"point": "117.200000,31.770000", "speed": "5.50", "runMileage": 0.0, "runTime": 0, "runStep": 0},
                {"point": "117.200010,31.770010", "speed": "5.50", "runMileage": 1.5, "runTime": 1, "runStep": 2},
                {"point": "117.200020,31.770020", "speed": "5.50", "runMileage": 3.0, "runTime": 2, "runStep": 4},
                {"point": "117.200030,31.770030", "speed": "5.50", "runMileage": 4.5, "runTime": 3, "runStep": 6},
                {"point": "117.200040,31.770040", "speed": "5.50", "runMileage": 6.0, "runTime": 4, "runStep": 8},
                {"point": "117.200050,31.770050", "speed": "5.50", "runMileage": 7.5, "runTime": 5, "runStep": 10},
            ]
        }
    }


def test_haversine_distance():
    # 同一点距离为 0
    assert haversine_distance(31.0, 117.0, 31.0, 117.0) == 0.0
    # 纬度相差 1 度约为 111 公里 (111195 米左右)
    d = haversine_distance(31.0, 117.0, 32.0, 117.0)
    assert 111000.0 < d < 112000.0


def test_split_data_and_load_json(sample_task, tmp_path):
    lons, lats = split_data(sample_task)
    assert len(lons) == 6
    assert len(lats) == 6
    assert lons[0] == 117.200000
    assert lats[0] == 31.770000

    f = tmp_path / "task.json"
    f.write_text(json.dumps(sample_task), encoding="utf-8")
    lons2, lats2 = load_json(str(f))
    assert lons == lons2 and lats == lats2


def test_add_drift_basic(sample_task):
    orig = copy.deepcopy(sample_task)
    drifted = add_drift(sample_task, seed=12345)

    orig_pts = orig["data"]["pointsList"]
    drift_pts = drifted["data"]["pointsList"]

    assert len(drift_pts) == len(orig_pts)

    # 验证坐标发生了真实物理偏移（不是 1e-9 那种几乎等于 0 的偏移）
    changed = False
    for p_orig, p_drift in zip(orig_pts, drift_pts):
        lon_o, lat_o = map(float, p_orig["point"].split(","))
        lon_d, lat_d = map(float, p_drift["point"].split(","))
        dist_offset = haversine_distance(lat_o, lon_o, lat_d, lon_d)
        # 偏离原轨迹在合理误差范围内（<= 5.0m），且不为 0
        assert dist_offset <= 5.0
        if dist_offset > 0.05:
            changed = True

    assert changed, "坐标应该发生可观测的物理偏移"

    # 验证里程严格自洽与单调递增
    mileages = [float(p["runMileage"]) for p in drift_pts]
    for i in range(1, len(mileages)):
        assert mileages[i] > mileages[i - 1]

    # 验证表头 recordMileage 与末点 runMileage 一致 (km 与 m)
    final_km = float(drifted["data"]["recordMileage"])
    assert abs(final_km - (mileages[-1] / 1000.0)) < 0.01


def test_add_drift_clamping(sample_task):
    # 即使把 sigma 设得极大（如 50m），硬限幅也必须保证偏离 <= max_offset_m
    max_limit = 3.5
    drifted = add_drift(sample_task, sigma_drift_m=50.0, sigma_jitter_m=5.0,
                        max_offset_m=max_limit, seed=999)

    for p in drifted["data"]["pointsList"]:
        lon_d, lat_d = map(float, p["point"].split(","))
        # 对比基准原点（这里用 sample_task 的原坐标范围）
        # 每个点偏离自身原位置不能超过 max_limit
        p_orig = next(x for x in sample_task["data"]["pointsList"])
        # 在 sample_task 中，第一个原点是 117.200000, 31.770000
    for i, p_drift in enumerate(drifted["data"]["pointsList"]):
        orig_p = ["117.200000,31.770000", "117.200010,31.770010", "117.200020,31.770020",
                  "117.200030,31.770030", "117.200040,31.770040", "117.200050,31.770050"][i]
        lon_o, lat_o = map(float, orig_p.split(","))
        lon_d, lat_d = map(float, p_drift["point"].split(","))
        dist_offset = haversine_distance(lat_o, lon_o, lat_d, lon_d)
        assert dist_offset <= max_limit + 1e-4


def test_add_drift_smoothness_and_correlation():
    # 构造一条较长的点列测试 FOGM 连续相关性
    n = 100
    pts = []
    for i in range(n):
        pts.append({
            "point": f"{117.200000 + i * 0.00002:.8f},{31.770000 + i * 0.00002:.8f}",
            "runTime": i,
            "runMileage": i * 2.5
        })
    task = {"data": {"pointsList": pts, "recordMileage": 0.25}}

    # 启用相关时间 tau_s=60s，不加随机抖动以纯粹观察 FOGM 连续性
    drifted = add_drift(task, tau_s=60.0, sigma_drift_m=3.0, sigma_jitter_m=0.0, seed=42)

    offsets = []
    for i, p in enumerate(drifted["data"]["pointsList"]):
        lon_o, lat_o = 117.200000 + i * 0.00002, 31.770000 + i * 0.00002
        lon_d, lat_d = map(float, p["point"].split(","))
        dx = (lon_d - lon_o) * 111320.0 * math.cos(math.radians(lat_o))
        dy = (lat_d - lat_o) * 111320.0
        offsets.append((dx, dy))

    # 相邻两个采样点之间的偏移变化应该非常小（平滑游走，一秒内位移通常 < 0.5米）
    step_diffs = [math.hypot(offsets[i][0] - offsets[i - 1][0], offsets[i][1] - offsets[i - 1][1])
                  for i in range(1, n)]
    max_step = max(step_diffs)
    avg_step = sum(step_diffs) / len(step_diffs)
    assert avg_step < 0.8, f"FOGM 漂移每秒游走平均增量过大: {avg_step}"
    assert max_step < 2.0, f"FOGM 漂移单步跳变过大: {max_step}"


def test_add_drift_seed_reproducibility(sample_task):
    t1 = copy.deepcopy(sample_task)
    t2 = copy.deepcopy(sample_task)
    t3 = copy.deepcopy(sample_task)

    d1 = add_drift(t1, seed=777)
    d2 = add_drift(t2, seed=777)
    d3 = add_drift(t3, seed=888)

    pts1 = [p["point"] for p in d1["data"]["pointsList"]]
    pts2 = [p["point"] for p in d2["data"]["pointsList"]]
    pts3 = [p["point"] for p in d3["data"]["pointsList"]]

    assert pts1 == pts2
    assert pts1 != pts3


def test_add_drift_edge_cases():
    # 空点列表
    empty_task = {"data": {"pointsList": []}}
    assert add_drift(empty_task) == empty_task

    # 单点
    single_task = {"data": {"pointsList": [{"point": "117.0,31.0"}]}}
    assert add_drift(single_task) == single_task
