# -*- coding: utf-8 -*-
"""跑步任务表（打表数据）的校验与生成。

依据（与项目 `main.py` 的实际行为一一对应，不自行发明）
------------------------------------------------------
项目把任务表交给 `_build_split_body` 时按"批"派生汇总，因此表内必须自洽：

| 派生量 | 公式（main.py:600-626） | 对任务表的要求 |
|---|---|---|
| `mileage` | 批内 末点−首点 的 `runMileage`（米） | 全表 **非递减** |
| `times` | 批内 末点−首点 的 `runTime`（秒） | 全表 **非递减** |
| `StepNumber` | 末点−首点 的 `runStep`；为负 → **直接报错停止** | 全表 **非递减** |
| `speeds` | `times/60 ÷ (mileage/1000)`，仅当 times>0 且 mileage>10m | 需要真实位移与时间 |
| `runSteps` | `StepNumber ÷ (times/60)` | — |
| `strides` | `mileage ÷ StepNumber` | `runStep` 不能全为 0（否则要靠 config strides 合成） |

点字段还必须能过 `_project_card_point` 的投影：9 个字段（isFence/isMock/point/
runMileage/runStatus/runStep/runTime/speed/ts）里，**runMileage/runTime/runStep 必须
是有限数值**（非数值会被拒绝，不会静默按 0 处理）。

生成手法沿用项目两个工具的口径：
- `tools/pace_changer.change_pace`：配速缩放 factor（**只允许 factor ≤ 1，即不能变快**，
  超出会打印"已经超越目标时间，停止放缩"），同步缩 duration / 步频 / 每点 speed、
  runTime，并把 runStep 除以 factor（步数随配速变快而减少）。
- `tools/drift.add_drift`：给所有坐标加同一个 ±1e-9 度的微小偏移，再按坐标重算里程。

**已知偏差（本模块主动修正，会在报告里标出）**：项目的 `add_drift` 只更新
`recordMileage`，**不动每点 runMileage**，于是表内会出现"末点 runMileage ≠ recordMileage"。
本模块在生成时把每点 runMileage 也按新坐标重算，保持表内自洽。
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
from typing import Any, Dict, List, Optional, Tuple

EARTH_RADIUS = 6371000.0


# ---------------------------------------------------------------- 基础
def haversine(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS * 2 * math.asin(math.sqrt(a))


def parse_point(s: str) -> Tuple[float, float]:
    lon, lat = str(s).split(",")[:2]
    return float(lon), float(lat)


def _num(v, what: str, idx: int) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"第 {idx} 点的 {what} 不是数值：{v!r}（项目会拒绝，不会按 0 处理）")
    if not math.isfinite(f):
        raise ValueError(f"第 {idx} 点的 {what} 不是有限数：{v!r}")
    return f


def load_task(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    if not isinstance(d, dict) or not isinstance(d.get("data"), dict):
        raise ValueError("任务表结构不对：需要顶层 {code,data}，data 为对象")
    if not isinstance(d["data"].get("pointsList"), list):
        raise ValueError("任务表缺少 data.pointsList")
    return d


def task_summary(path: str) -> Dict[str, Any]:
    """列表用摘要（不做重校验，快）。"""
    d = load_task(path)
    data = d["data"]
    pts = data["pointsList"]
    out = {
        "file": os.path.basename(path),
        "path": path,
        "bytes": os.path.getsize(path),
        "points": len(pts),
        "mileage": data.get("recordMileage"),
        "duration": data.get("duration"),
        "pace": data.get("recodePace"),
        "cadence": data.get("recodeCadence"),
        "raType": data.get("raType"),
        "raId": data.get("raId"),
        "studentId": data.get("studentId"),
        "studentName": data.get("studentName"),
        "fence": len(data.get("manageList") or []),
        "start": (pts[0] or {}).get("point") if pts else None,
        "end": (pts[-1] or {}).get("point") if pts else None,
    }
    if out["duration"] and out["mileage"]:
        # recordMileage 单位是公里，duration 是秒
        out["avg_speed_kmh"] = round(float(out["mileage"]) / (float(out["duration"]) / 3600), 2)
    return out


def curves(path: str, max_points: int = 400) -> Dict[str, Any]:
    """给前端画曲线用的降采样序列。"""
    d = load_task(path)
    pts = d["data"]["pointsList"]
    n = len(pts)
    step = max(1, n // max_points)
    sample = pts[::step]
    def col(key, cast=float, default=0.0):
        out = []
        for p in sample:
            try:
                out.append(cast(p.get(key, default)))
            except (TypeError, ValueError):
                out.append(None)
        return out
    return {
        "t": col("runTime"),
        "mileage": col("runMileage"),
        "speed": col("speed"),
        "step": col("runStep"),
        "points_used": len(sample),
        "points_total": n,
    }


# ---------------------------------------------------------------- 校验
def validate_task(path: str, split_count: int = 10, strides_m: float = 0.8,
                  speed_range: Tuple[float, float] = (3.0, 12.0)) -> Dict[str, Any]:
    """按项目实际消费方式做一致性校验。返回 {ok, errors[], warnings[], stats}。

    只报错、不改数据：任何一项 error 都会让项目在派生汇总时失败或产生矛盾数据。
    """
    errors: List[str] = []
    warnings: List[str] = []
    stats: Dict[str, Any] = {}
    try:
        d = load_task(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [str(exc)], "warnings": [], "stats": {}}
    data = d["data"]
    pts = data["pointsList"]
    stats["points"] = len(pts)

    if len(pts) < split_count:
        warnings.append(f"点数 {len(pts)} 少于一批（split_count={split_count}），"
                        "将全部作为尾批发送")

    # --- 逐点字段（收集，不提前返回：一张表可能同时有格式错和单调性错）---
    KEYS = ("isFence", "isMock", "point", "runMileage", "runStatus",
            "runStep", "runTime", "speed", "ts")
    bad_field = []
    field_errors: List[str] = []
    for i, p in enumerate(pts):
        if not isinstance(p, dict):
            field_errors.append(f"第 {i} 点不是对象")
            continue
        extra = [k for k in p if k not in KEYS]
        if extra and len(bad_field) < 2:
            bad_field.append(f"第 {i} 点多出字段 {extra}（项目会裁剪，仅提示）")
        if not p.get("point"):
            field_errors.append(f"第 {i} 点缺少 point")
        for key in ("runMileage", "runTime", "runStep", "speed"):
            try:
                _num(p.get(key, 0), key, i)
            except ValueError as exc:
                if len(field_errors) < 8:
                    field_errors.append(str(exc))
        try:
            lon, lat = parse_point(p.get("point", ""))
            if not (60.0 < lon < 150.0 and 3.0 < lat < 55.0):
                warnings.append(f"第 {i} 点坐标不在中国境内范围：{p.get('point')!r}")
        except Exception:  # noqa: BLE001
            field_errors.append(f"第 {i} 点坐标格式不是 '经度,纬度'：{p.get('point')!r}")
        if len(field_errors) >= 8:
            field_errors.append("……更多同类字段问题已省略")
            break
    errors.extend(field_errors)
    warnings.extend(bad_field)

    # --- 单调性（项目按批取差值，非单调会让 mileage/times/StepNumber 变负或倒退）---
    # 即使上面有字段错也继续算：能算的项都要报出来，便于一次改完。
    def series(key):
        vals = []
        for i, p in enumerate(pts):
            try:
                vals.append(_num(p.get(key, 0), key, i))
            except ValueError:
                vals.append(None)
        return vals

    for key, label in (("runMileage", "里程"), ("runTime", "时间"), ("runStep", "步数")):
        s = series(key)
        if any(v is None for v in s):
            continue                      # 有非数值，单调性无意义
        drops = [(i, s[i - 1], s[i]) for i in range(1, len(s)) if s[i] < s[i - 1]]
        if drops:
            i, a, b = drops[0]
            errors.append(f"{label}({key}) 在第 {i} 点回退：{a} → {b}"
                          f"（全表必须非递减，共 {len(drops)} 处）")
        stats[f"{key}_range"] = [s[0], s[-1]]

    m, t, st, sp = (series("runMileage"), series("runTime"),
                    series("runStep"), series("speed"))
    if all(v is not None for v in m):
        stats["mileage_delta"] = round(m[-1] - m[0], 2)
    stats["duration"] = data.get("duration")
    stats["recodePace"] = data.get("recodePace")
    stats["recodeCadence"] = data.get("recodeCadence")
    stats["recordMileage"] = data.get("recordMileage")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "stats": stats}

    # --- 与表头字段的一致性 ---
    # 单位说明：recordMileage 是**公里**，点列 runMileage 是**米**（实测同一张表
    # recordMileage=1.78 而点列末值 1778.8）——比较前必须换算，否则会误报。
    try:
        rec_m = float(data.get("recordMileage")) * 1000.0
        delta_m = m[-1] - m[0]
        if abs(delta_m - rec_m) > max(10.0, rec_m * 0.02):
            warnings.append(f"表头 recordMileage={data.get('recordMileage')} km "
                            f"({rec_m:.0f} m) 与点列里程差 {delta_m:.0f} m 不一致"
                            "（项目按点列差值算 mileage，表头仅供参考）")
    except (TypeError, ValueError):
        warnings.append("表头缺少/非法 recordMileage")
    try:
        dur = float(data.get("duration"))
        if abs(dur - t[-1]) > max(2.0, dur * 0.02):
            warnings.append(f"表头 duration={dur:g} 与末点 runTime={t[-1]:g} 不一致；"
                            "脚本按 duration/点数 计算每批等待时间，建议对齐")
    except (TypeError, ValueError):
        warnings.append("表头缺少/非法 duration（脚本用它算等待节奏）")

    # --- 批内派生量的合理性（真正发给服务器的东西）---
    if st[-1] - st[0] <= 0:
        warnings.append("runStep 全为 0：项目将用 config 的 strides（步幅）合成累计步数；"
                        "若 strides 也未配置会直接报错停止")
    elif strides_m > 0:
        derived = (m[-1] - m[0]) / (st[-1] - st[0])
        stats["derived_stride_m"] = round(derived, 3)
        if not (0.4 <= derived <= 1.6):
            warnings.append(f"由点列推出的步幅 {derived:.2f} m/步 不常见"
                            f"（config strides={strides_m}）；step 与里程可能不匹配")
    zero_mileage_batches = 0
    for i in range(0, len(pts), split_count):
        chunk = pts[i:i + split_count]
        if len(chunk) < 2:
            continue
        dm = _num(chunk[-1]["runMileage"], "runMileage", 0) - _num(chunk[0]["runMileage"], "runMileage", 0)
        dt = _num(chunk[-1]["runTime"], "runTime", 0) - _num(chunk[0]["runTime"], "runTime", 0)
        if dm <= 10.0 or dt <= 0:
            zero_mileage_batches += 1
    if zero_mileage_batches:
        warnings.append(f"有 {zero_mileage_batches} 个批次的里程差 ≤10m 或时间差 ≤0，"
                        "该批 speeds 会被算成 0（项目公式如此）")

    sp_lo, sp_hi = min(sp), max(sp)
    stats["speed_range"] = [sp_lo, sp_hi]
    stats["speed_over_12"] = sum(1 for v in sp if v > 12.0)
    if sp_lo <= 0:
        errors.append(f"存在 speed ≤ 0 的点（最小 {sp_lo}）：配速必须为正")
    elif not (speed_range[0] <= sp_lo and sp_hi <= speed_range[1]):
        warnings.append(f"速度范围 {sp_lo}~{sp_hi} 落在常见区间 {speed_range[0]}~"
                        f"{speed_range[1]} 之外（仅提示，服务端阈值未知）")
    # speed 是**瞬时速度 km/h**（GPS 原始抖动，非配速，也非表头 recodePace）。
    # 项目发的报文体里含 speeds 汇总字段，所以个别尖峰本身不致命，只提示。
    if stats["speed_over_12"] > max(3, len(sp) * 0.02):
        warnings.append(f"有 {stats['speed_over_12']} 个点 speed > 12 km/h"
                        "（GPS 尖峰；脚本自身不改这些值，只影响观感与 speeds 汇总）")
    if len(set(round(v, 3) for v in sp)) == 1:
        warnings.append("所有点速度完全相同（缺少自然波动，可能被判异常）")

    # --- 围栏打卡点 ---
    ml = data.get("manageList")
    if not isinstance(ml, list) or not ml:
        errors.append("manageList 为空/缺失：打表任务需要围栏打卡点")
    else:
        stats["fence"] = len(ml)
        for j, item in enumerate(ml):
            if not isinstance(item, dict) or "point" not in item or "index" not in item:
                errors.append(f"manageList[{j}] 缺少 point/index 字段")
                break
        else:
            idxs = [item.get("index") for item in ml]
            if len(set(map(str, idxs))) != len(idxs):
                warnings.append("manageList 的 index 有重复")
            if str(data.get("isMorning", "")).upper() not in ("R0",):
                pass  # isMorning 与打卡无关，不判

    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


# ---------------------------------------------------------------- 生成
def generate_variant(src: str, dst: str, seed: Optional[int] = None,
                     target_pace: Optional[float] = None,
                     drift_deg: Optional[float] = None,
                     rebuild_mileage: bool = True) -> Dict[str, Any]:
    """由源表生成一个新表：可选配速缩放 + 坐标微小漂移 + 里程按新坐标重算。

    - 配速缩放沿用 `tools/pace_changer.change_pace` 的口径：factor = 目标/当前，
      **factor > 1 直接拒绝**（与项目一样不允许"改快"）。
    - 漂移沿用 `tools/drift.add_drift` 的口径：全表同向加一个小偏移。
    - rebuild_mileage：把每点 runMileage 按新坐标累积重算，使表内自洽
      （修正项目 add_drift 只改表头、不改点列的已知不一致）。
    """
    d = load_task(src)
    out = copy.deepcopy(d)
    data = out["data"]
    pts = data["pointsList"]
    rng = random.Random(seed)
    notes: List[str] = []
    stats: Dict[str, Any] = {}

    # 1) 坐标漂移
    if drift_deg is None:
        drift = rng.uniform(-1e-9, 1e-9)
    else:
        drift = float(drift_deg) * rng.choice((-1, 1)) if drift_deg < 0 else float(drift_deg)
    if drift:
        coords = []
        for p in pts:
            lon, lat = parse_point(p["point"])
            coords.append((lon + drift, lat + drift))
            p["point"] = f"{lon + drift},{lat + drift}"
        notes.append(f"坐标整体漂移 {drift:+.3e} 度（≈{abs(drift) * 111000:.2f} 米）")
        stats["drift_deg"] = drift

    # 2) 配速缩放（口径同 pace_changer）
    if target_pace:
        cur = float(data.get("recodePace") or 0)
        if cur <= 0:
            raise ValueError("源表 recodePace 非法，无法按配速缩放")
        factor = float(target_pace) / cur
        if factor > 1:
            raise ValueError(
                f"目标配速 {target_pace} 比当前 {cur} 更快"
                f"（配速数字越小越快；factor={factor:.3f} > 1）。项目 pace_changer 的口径是"
                "只允许变慢，超出会直接停止放缩。请把目标配速设得更大（例如 9.0），"
                "或直接沿用原表")
        data["recodePace"] = round(float(target_pace), 2)
        data["duration"] = int(factor * float(data["duration"]))
        data["recodeCadence"] = int(float(data["recodeCadence"]) / factor)
        for p in pts:
            p["speed"] = round(float(p["speed"]) * factor, 2)
            p["runTime"] = str(int(float(p["runTime"]) * factor))
            p["runStep"] = str(int(float(p["runStep"]) / factor))
        notes.append(f"配速 {cur} → {target_pace}（factor={factor:.4f}）："
                     f"时长 {data['duration']}s、步频 {data['recodeCadence']}")
        stats["pace_factor"] = factor

    # 3) 里程按新坐标重算（表内自洽）
    if rebuild_mileage and (drift or target_pace):
        prev = None
        cum = 0.0
        for i, p in enumerate(pts):
            lon, lat = parse_point(p["point"])
            if prev is not None:
                cum += haversine(prev[1], prev[0], lat, lon)
            prev = (lon, lat)
            p["runMileage"] = str(cum)
        data["recordMileage"] = round(cum / 10) / 100
        notes.append(f"每点 runMileage 与表头 recordMileage 已按新坐标重算"
                     f"（{data['recordMileage']} km）")
        stats["recordMileage"] = data["recordMileage"]

    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return {"ok": True, "path": dst, "notes": notes, "stats": stats,
            "summary": task_summary(dst)}
