"""基于一阶高斯-马尔可夫模型（FOGM）的真实 GPS 定位漂移与步态误差模拟器。

取代老旧的 ±1e-9 度刚体全局平移：
  1. 物理拟真：基于时间自相关的一阶高斯-马尔可夫过程（时间常数 tau_s≈45s，漂移标准差 sigma_drift_m≈2.5m），
     模拟真实卫星轨道几何变化（DOP）与对流层/电离层延迟带来的低频平缓随机游走；
  2. 步态抖动：叠加约 0.4m 高频高斯白噪声，模拟跑步手臂挥动和天线朝向变化；
  3. 硬限幅边界：严格限制单点最大偏离距离（默认 5.0m），确保绝不漂出操场或校园道路跑道；
  4. 表内数据自洽重构：逐点根据新坐标重算 runMileage 与表头 recordMileage，消灭风控破绽；
  5. 100% 向后兼容：保持 add_drift、haversine_distance、split_data、load_json 接口。
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

EARTH_RADIUS = 6371000.0  # 地球半径（米）
METERS_PER_DEG_LAT = 111320.0  # 纬度 1 度对应的米数


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两点之间的球面大圆距离（米，使用 Haversine 公式）。"""
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * (math.sin(dlon / 2.0) ** 2)
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.asin(math.sqrt(a))
    return EARTH_RADIUS * c


def split_data(data: Dict[str, Any]) -> Tuple[List[float], List[float]]:
    """提取 pointsList 中的每一个点的经纬度列表 (lonData, latData)。"""
    lonData: List[float] = []
    latData: List[float] = []
    for point in data.get('data', {}).get('pointsList', []):
        point_str = point['point']
        lon, lat = map(float, point_str.split(','))
        lonData.append(lon)
        latData.append(lat)
    return lonData, latData


def load_json(filePath: str) -> Tuple[List[float], List[float]]:
    """加载 JSON 文件并提取经纬度。"""
    with open(filePath, 'r', encoding='utf-8') as f:
        data = json.loads(f.read())
    return split_data(data)


def add_drift(data: Dict[str, Any],
              *,
              tau_s: float = 45.0,
              sigma_drift_m: float = 2.5,
              sigma_jitter_m: float = 0.4,
              max_offset_m: float = 5.0,
              rebuild_mileage: bool = True,
              seed: Optional[int] = None) -> Dict[str, Any]:
    """为任务表添加真实 GPS 连续定位游走漂移与步态抖动。

    :param data: 任务表字典（包含 data.pointsList 与 data.recordMileage）
    :param tau_s: 高斯-马尔可夫过程时间自相关常数（秒，默认 45 秒）
    :param sigma_drift_m: 慢速游走漂移标准差（米，默认 2.5 米）
    :param sigma_jitter_m: 高频步态/天线抖动标准差（米，默认 0.4 米）
    :param max_offset_m: 最大允许偏离原轨迹硬上限（米，默认 5.0 米）
    :param rebuild_mileage: 是否按新坐标重新累积计算 runMileage 和 recordMileage（默认 True）
    :param seed: 随机种子，指定时可完全复现漂移轨迹
    :return: 修改后的原字典（支持链式调用）
    """
    pts = data.get("data", {}).get("pointsList", [])
    if not pts or len(pts) < 2:
        return data

    rng = random.Random(seed) if seed is not None else random.Random()

    coords: List[Tuple[float, float]] = []
    for p in pts:
        parts = p["point"].split(",")
        coords.append((float(parts[0].strip()), float(parts[1].strip())))

    n = len(pts)

    # 提取时间序列，计算相邻点时间差
    times: List[float] = []
    has_time = True
    for p in pts:
        raw_t = p.get("runTime")
        if raw_t is not None:
            try:
                times.append(float(raw_t))
            except (ValueError, TypeError):
                has_time = False
                break
        else:
            has_time = False
            break

    # 初始漂移状态 (x: 东西向偏移米, y: 南北向偏移米)
    x = rng.gauss(0.0, sigma_drift_m * 0.5)
    y = rng.gauss(0.0, sigma_drift_m * 0.5)
    init_r = math.hypot(x, y)
    if init_r > max_offset_m * 0.7:
        scale = (max_offset_m * 0.7) / max(1e-9, init_r)
        x *= scale
        y *= scale

    new_coords: List[Tuple[float, float]] = []

    for i in range(n):
        if i > 0:
            if has_time and i < len(times):
                dt = times[i] - times[i - 1]
                if dt <= 0 or dt > 60:
                    dt = 1.0
            else:
                dist_prev = haversine_distance(coords[i - 1][1], coords[i - 1][0],
                                               coords[i][1], coords[i][0])
                dt = max(0.5, min(10.0, dist_prev / 3.0))

            # 一阶高斯-马尔可夫转移更新
            phi = math.exp(-dt / max(1.0, tau_s))
            step_sigma = sigma_drift_m * math.sqrt(max(0.0, 1.0 - phi * phi))
            x = phi * x + rng.gauss(0.0, step_sigma)
            y = phi * y + rng.gauss(0.0, step_sigma)

        # 叠加高频抖动 Jitter
        jx = rng.gauss(0.0, sigma_jitter_m) if sigma_jitter_m > 0 else 0.0
        jy = rng.gauss(0.0, sigma_jitter_m) if sigma_jitter_m > 0 else 0.0

        off_x = x + jx
        off_y = y + jy

        # 硬限幅硬约束：最大偏离原点不得超过 max_offset_m
        tot_r = math.hypot(off_x, off_y)
        if tot_r > max_offset_m:
            s_clamp = max_offset_m / max(1e-9, tot_r)
            off_x *= s_clamp
            off_y *= s_clamp

        lon_i, lat_i = coords[i]
        lat_rad = math.radians(lat_i)
        meters_per_lon = METERS_PER_DEG_LAT * max(0.01, math.cos(lat_rad))

        d_lon = off_x / meters_per_lon
        d_lat = off_y / METERS_PER_DEG_LAT

        new_lon = round(lon_i + d_lon, 8)
        new_lat = round(lat_i + d_lat, 8)
        new_coords.append((new_lon, new_lat))
        pts[i]["point"] = f"{new_lon:.8f},{new_lat:.8f}"

    if rebuild_mileage:
        cum_m = 0.0
        first_m = pts[0].get("runMileage")
        is_float_mileage = isinstance(first_m, (int, float))

        for i in range(n):
            if i > 0:
                step_m = haversine_distance(new_coords[i - 1][1], new_coords[i - 1][0],
                                            new_coords[i][1], new_coords[i][0])
                cum_m += step_m

            formatted_m = round(cum_m, 2)
            pts[i]["runMileage"] = formatted_m if is_float_mileage else str(formatted_m)

        final_km = round(cum_m / 1000.0, 2)
        orig_rec = data.get("data", {}).get("recordMileage")
        if isinstance(orig_rec, str):
            data["data"]["recordMileage"] = f"{final_km:.2f}"
        else:
            data["data"]["recordMileage"] = final_km

    return data


if __name__ == '__main__':
    print("=== 真实 GPS 定位漂移工具 (基于一阶高斯-马尔可夫模型 FOGM) ===")
    print("在保持原始跑步路线骨架与跑道走向的前提下，添加平缓低频游走漂移与步态微抖动。")
    pos_choice = input("你需要修改哪一个校区任务表？(1.翡翠湖 2.屯溪路 3.宣城 4.其他): ").strip()

    path = ("./tasks_fch/" if pos_choice == "1"
            else "./tasks_txl/" if pos_choice == "2"
            else "./tasks_xc/" if pos_choice == "3"
            else "./tasks_else/")
    chi = input("输入要修改的文件序号 (例如 0 对应 tasklist_0.json): ").strip()
    file_path = os.path.join(path, f"tasklist_{chi}.json" if chi else "tasklist_0.json")

    if not os.path.isfile(file_path):
        print(f"文件不存在: {file_path}")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    pts_count = len(task_data.get("data", {}).get("pointsList", []))
    old_m = task_data.get("data", {}).get("recordMileage")
    print(f"已加载: {file_path}，包含 {pts_count} 个点，当前里程: {old_m} km")

    # 执行 FOGM 漂移
    add_drift(task_data)

    new_m = task_data.get("data", {}).get("recordMileage")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(task_data, f, indent=4, ensure_ascii=False)

    print(f"漂移完成并写回 {file_path}！新里程: {new_m} km")
