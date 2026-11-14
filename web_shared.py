# -*- coding: utf-8 -*-
"""Web 共享服务层：校区任务表查询、底图发现、路线合成预览、计划管理及配置脱敏。

供 web_app.py 控制台与路线工具集中调用。
"""
from __future__ import annotations

import configparser
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import yun_route  # 轨迹合成引擎（deform）

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
ROUTES_DIR = os.path.join(BASE_DIR, "examples", "routes")
MASK = "__MASKED__"          # 返回前端的敏感值占位
KEEP = "__KEEP__"            # 前端未改动敏感值的回传哨兵
SENSITIVE_KEYS = {"password", "token", "cipherkey", "cipherkeyencrypted"}

# 面板自产的"发送素材"：work_dir 已被 .gitignore 忽略，不污染仓库。
SEND_DIR = os.path.join(BASE_DIR, "work_dir", "web_send")
DRILL_FIXTURE = os.path.join(SEND_DIR, "dry_home_drill.json")

# 选项卡最近一次成功预览对应的发送计划：/api/run 只跑它，保证"看到什么就发什么"
PLAN: Dict[str, Any] = {"argv": None, "dry": [], "detail": "", "cwd": BASE_DIR}


# ------------------------------------------------------------------ helpers
def list_campuses(base_dir: str = BASE_DIR) -> Dict[str, List[str]]:
    out = {}
    if not os.path.isdir(base_dir):
        return out
    for d in sorted(os.listdir(base_dir)):
        if d.startswith("tasks_") and os.path.isdir(os.path.join(base_dir, d)):
            files = sorted(f for f in os.listdir(os.path.join(base_dir, d))
                           if f.endswith(".json"))
            if files:
                out[d] = files
    return out


def list_geojsons(routes_dir: str = ROUTES_DIR) -> List[str]:
    """examples/routes/ 下可作形变来源的 GeoJSON（排除 fence* 围栏等非轨迹文件）。"""
    if not os.path.isdir(routes_dir):
        return []
    return sorted(f for f in os.listdir(routes_dir)
                  if f.endswith(".geojson") and not f.lower().startswith("fence"))


def list_sources(base_dir: str = BASE_DIR, routes_dir: str = ROUTES_DIR) -> List[str]:
    """形变方案的"任选底图 json"清单：任务表 + 任意 GeoJSON（仓库相对路径）。

    与 yun_route.extract_points 的能力对应：tasklist（data.pointsList）、
    GeoJSON LineString/FeatureCollection、裸坐标列表都能直接当形变来源。
    """
    out = []
    for campus, files in list_campuses(base_dir).items():
        out.extend(f"{campus}/{f}" for f in files)
    out.extend(f"examples/routes/{f}" for f in list_geojsons(routes_dir))
    return out


def _source_path(rel: str, base_dir: str = BASE_DIR, routes_dir: str = ROUTES_DIR) -> Optional[str]:
    """把清单里的仓库相对路径解析为绝对路径；不在清单内一律拒绝（防目录穿越）。"""
    rel = (rel or "").replace("\\", "/")
    if rel not in list_sources(base_dir, routes_dir):
        return None
    return os.path.join(base_dir, *rel.split("/"))


def _task_file_points(path: str) -> Tuple[List[List[float]], float]:
    """tasklist 原始点列 -> ([[lon,lat],...], km)。'point' 字段格式 'lon,lat'。"""
    with open(path, encoding="utf-8-sig") as f:
        j = json.load(f)
    d = j.get("data") or j
    rows = d.get("pointsList") or []
    pts = [[float(x) for x in r["point"].split(",")] for r in rows]
    # 注意：里程字段实测可能是字符串（'2214' 米 / '3' 公里，格式跨表不统一），
    # 防御性处理：>100 视为米，否则视为公里（审计点：单位启发式有歧义风险）
    raw_km = float(rows[-1].get("runMileage") or 0) if rows else 0.0
    km = raw_km / 1000.0 if raw_km > 100 else raw_km
    return pts, km


# ------------------------------------------- 发送计划（预览 ↔ 命令，同一份事实）
def _rel(path: str, base_dir: str = BASE_DIR) -> str:
    """绝对路径 → 仓库相对路径（正斜杠）：命令可读，main.py 按调用者 cwd(仓库根)解析。"""
    return os.path.relpath(path, base_dir).replace("\\", "/")


def _dump_json(path: str, obj: Any) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def _drill_fixture(fixture_path: str = DRILL_FIXTURE) -> str:
    """离线演练夹具（面板自产，放宽步频/里程区间）。

    判据来自假服务端回包：仓库自带 dry_run_home.json 是打表夹具（raDislikes=3、
    6~7 km），拿它演练合成轨迹必被 validate_generated_route 拒；这份只压链路，
    不冒充真实任务区间（真跑的区间由 getHomeRunInfo 下发，离线无从得知）。
    """
    return _dump_json(fixture_path, {
        "code": 200, "msg": "offline drill fixture（放宽区间，仅演练链路）",
        "data": {"cralist": [{"id": 900001, "schoolId": "100", "raType": "T1",
                              "raRunArea": "A", "raDislikes": 0,
                              "raSingleMileageMin": 1.0, "raSingleMileageMax": 20.0,
                              "raCadenceMin": 1, "raCadenceMax": 1000,
                              "runFaceStatus": "N", "points": ""}]}})


def _plan_base(campus: str, task: str, base_dir: str = BASE_DIR, send_dir: str = SEND_DIR) -> Dict[str, Any]:
    """打表回放：把所选任务表放进"独占目录"，让 -a 不可能随机选到别的表。

    main.py 的 -t/--task_path 交给 do_by_points_map(random_choose=True)，内部是
    random.choice(os.listdir(目录))：直接把 tasks_fch 传进去，预览的 tasklist_0
    和真正发送的可能是任意一张表。故把所选表复制到只含该表的目录。
    """
    src = os.path.join(base_dir, campus, task)
    pick_dir = os.path.join(send_dir, "task_pick")
    os.makedirs(pick_dir, exist_ok=True)
    for old in os.listdir(pick_dir):
        os.remove(os.path.join(pick_dir, old))
    dst = os.path.join(pick_dir, task)
    shutil.copyfile(src, dst)
    with open(src, "rb") as a, open(dst, "rb") as b:
        if a.read() != b.read():
            raise ValueError("任务表复制校验失败：" + _rel(dst, base_dir))
    return {"argv": ["-a", "-t", _rel(pick_dir, base_dir)], "dry": ["--dry-run"], "cwd": base_dir,
            "detail": f"{campus}/{task} → {_rel(pick_dir, base_dir)}/（单表目录，-a 不会随机选表）"}


def _plan_deform(cfg: dict, preview_task: dict, base_dir: str = BASE_DIR, send_dir: str = SEND_DIR) -> Dict[str, Any]:
    """合成轨迹：把预览用的 cfg 原样落盘，只引用这一份 cfg。

    自检：用 CLI 的同一入口（yun_route.generate，base_dir=cfg 所在目录）复算一遍，
    与预览逐点比对；不一致就报错，而不是给出一条"看起来对"的命令。
    """
    cfg_path = _dump_json(os.path.join(send_dir, "route_cfg.json"), cfg)
    again = yun_route.generate(cfg_path)
    a, b = preview_task["data"]["pointsList"], again["data"]["pointsList"]
    same = (len(a) == len(b) and a[0]["point"] == b[0]["point"]
            and a[-1]["point"] == b[-1]["point"]
            and (preview_task["data"]["recordMileage"], preview_task["data"]["duration"])
            == (again["data"]["recordMileage"], again["data"]["duration"]))
    if not same:
        raise ValueError("预览与命令不一致（cfg 落盘后复算结果不同）：已拒绝生成命令")
    md = again["metadata"]
    drill_path = os.path.join(send_dir, "dry_home_drill.json")
    return {"argv": ["-a", "--route-config", _rel(cfg_path, base_dir)],
            "dry": ["--dry-run", "--dry-home", _rel(_drill_fixture(drill_path), base_dir)], "cwd": base_dir,
            "detail": f"{_rel(cfg_path, base_dir)} ｜ {md['source']} seed={md['seed']} "
                      f"第 {md['attempt']} 次抽中 ｜ {len(b)} 点 "
                      f"{again['data']['recordMileage']:.4f} km"}


def _cadence_gate_warn(cadence: Optional[float], config_path: str = CONFIG_PATH) -> str:
    """按本地 config.ini 的步频偏移，提前算出"真跑会不会被区间拒"。

    validate_generated_route 判据：raCadenceMin+偏移 ≤ 合成步频 ≤ raCadenceMax+偏移，
    偏移取自 config.ini（本仓库 +30/−150）。区间被压窄到需要 ≥180 spm 宽度时，
    典型任务区间（如 120~190）必然判负——真跑会在 start 之前中止，dry-run 不受影响。
    """
    if cadence is None:
        return ""
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    try:
        lo = int(cp.get("Run", "cadence_min_offset"))
        hi = int(cp.get("Run", "cadence_max_offset"))
    except Exception:
        return ""
    need_min, need_max = cadence - lo, cadence - hi
    if need_max - need_min <= 70:          # 70 ≈ 常见任务区间宽度（120~190），不算异常
        return ""
    return (f"步频闸门提示：本次合成步频 {cadence:.1f} spm；按本地 config.ini 偏移 "
            f"({lo:+d}/{hi:+d})，服务端必须 raCadenceMin ≤ {need_min:.0f} 且 "
            f"raCadenceMax ≥ {need_max:.0f}（区间宽 ≥ {need_max - need_min:.0f} spm）才放行。"
            f"若学校下发 120~190，真跑会被 validate_generated_route 拒（步频不在允许范围），"
            f"需调小 cadence_max_offset 的收缩量")


def _set_plan(plan: dict) -> None:
    """把预览结果登记为可执行的计划。"""
    PLAN["argv"] = list(plan.get("argv") or []) or None
    PLAN["dry"] = list(plan.get("dry") or [])
    PLAN["detail"] = plan.get("detail", "")
    PLAN["cwd"] = plan.get("cwd", BASE_DIR)


def build_preview(qs: dict, base_dir: str = BASE_DIR) -> dict:
    scheme = (qs.get("scheme") or ["base"])[0]
    if scheme == "base":
        campus = (qs.get("campus") or ["tasks_fch"])[0]
        task = (qs.get("task") or [""])[0]
        meta = list_campuses(base_dir)
        if campus not in meta:
            return {"error": "未知校区目录: " + campus}
        if task not in meta[campus]:
            task = meta[campus][0]
        ref, km = _task_file_points(os.path.join(base_dir, campus, task))
        return {"ref": ref, "send": ref,
                "ref_name": f"参考=即将发送：{campus}/{task}（打表原样上传）",
                "send_name": "打表原轨迹", "send_km": round(km, 3), "note": "",
                "command": _plan_base(campus, task, base_dir), "warn": ""}

    if scheme == "deform":
        # 轨迹合成（deform）：5.5m 弧长重采样 + 法向 OU 游走 + 二维相关漂移 +
        # 稀疏衰减跳变，再按 ±10% 抽签配速/步频/总里程并做一致性采样。
        # 与 main.py --route-config 共用 yun_route.generate_cfg 同一实现。
        src = (qs.get("src") or [""])[0]
        src_path = _source_path(src, base_dir)
        if src_path is None:
            return {"error": "未知底图：" + (src or "(空)")}
        try:
            seed = int((qs.get("seed") or ["31337"])[0])
            interval = int((qs.get("interval") or ["2"])[0])
        except ValueError:
            return {"error": "seed / 采样间隔必须是整数"}
        cfg = {"source_json": src_path, "coordinate_system": "GCJ-02",
               "seed": seed, "sample_seconds": interval}
        for key, qk, label in (("min_distance_m", "min", "最短里程"), ("max_distance_m", "max", "最长里程")):
            raw = (qs.get(qk) or [""])[0].strip()
            if raw:
                try:
                    cfg[key] = float(raw)
                except ValueError:
                    return {"error": f"{label}必须是数字（留空=不限制）"}
        try:
            task = yun_route.generate_cfg(cfg, base_dir)
        except ValueError as exc:
            return {"error": str(exc), "window_hint": "可放宽最短/最长里程，或改 seed 重抽"}
        md, data = task["metadata"], task["data"]
        points, ref_metrics = yun_route.load_source(src_path)
        rows = data["pointsList"]
        try:
            command = _plan_deform(cfg, task, base_dir)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"ref": [[p[0], p[1]] for p in points],
                "send": [[float(x) for x in r["point"].split(",")] for r in rows],
                "ref_name": f"参考原轨迹：{src}",
                "send_name": f"合成任务（seed={md['seed']}，第 {md['attempt']} 次抽中）",
                "send_km": data["recordMileage"],
                "metrics": {
                    "ref_len_km": round(ref_metrics["length_m"]/1000.0, 3),
                    "ref_pace": None if ref_metrics["pace_min_km"] is None else round(ref_metrics["pace_min_km"], 2),
                    "ref_cadence": None if ref_metrics["cadence_spm"] is None else round(ref_metrics["cadence_spm"], 1),
                    "send_len_km": round(data["recordMileage"], 3),
                    "send_pace": round(data["recodePace"], 2),
                    "send_cadence": round(data["recodeCadence"], 1),
                    "chord_km": round(md["geometry_chord_m"]/1000.0, 3),
                    "interval_s": md["interval_s"], "step_m": md["step_m"],
                    "points": len(rows), "attempt": md["attempt"],
                    "window": md["distance_window_m"],
                },
                "note": f"输入 {len(points)} 点 → 输出 {len(rows)} 点",
                "command": command, "warn": _cadence_gate_warn(data["recodeCadence"], CONFIG_PATH)}

    return {"error": "未知 scheme: " + scheme}


def read_config_masked(config_path: str = CONFIG_PATH) -> dict:
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    out = {}
    for sec in cp.sections():
        out[sec] = {k: (MASK if k.lower() in SENSITIVE_KEYS and cp[sec][k]
                        else cp[sec][k]) for k in cp[sec]}
    return out


def save_config(payload: dict, config_path: str = CONFIG_PATH) -> None:
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")   # 先读原值，__KEEP__ 直接跳过
    for sec, kv in (payload or {}).items():
        sec = str(sec).strip()
        if not sec or sec.lower() == "DEFAULT":
            continue
        if not cp.has_section(sec):
            cp.add_section(sec)
        for k, v in kv.items():
            if v in (KEEP, MASK):   # 纵深防御：掩码占位串永远不许落盘
                continue
            cp[sec][str(k).strip()] = "" if v is None else str(v)
    with open(config_path, "w", encoding="utf-8") as f:
        cp.write(f)   # 已验证 config.ini 无注释，重写无损


# ------------------------------------------------------------------ 地图配置管理
MAP_CONFIG_FILE = "map_config.json"
DEFAULT_MAP_CONFIG: Dict[str, Any] = {
    "provider": "amap",          # "amap" | "baidu" | "tencent" | "osm"
    "map_type": "normal",        # "normal" (标准) | "satellite" (卫星) | "hybrid" (混合) | "dark" (暗色)
    "amap_key": "",              # 高德 Web API Key
    "amap_security_code": "",    # 高德安全密钥 (可选)
    "baidu_ak": "",              # 百度地图浏览器端 AK
    "tencent_key": "",           # 腾讯地图 WebService / GL Key
    "osm_tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "fallback_canvas": True,     # 地图提供商不可用时回退为 Canvas
}


def get_map_config(base_dir: str = BASE_DIR) -> Dict[str, Any]:
    """读取地图提供商配置。若未设置 amap_key，兼容回退读取 config.ini 中的 map_key。"""
    cfg = dict(DEFAULT_MAP_CONFIG)
    cfg_file = os.path.join(base_dir, MAP_CONFIG_FILE)
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
        except Exception:
            pass

    # 若尚未配置 amap_key，自动继承原有 config.ini [User] map_key
    if not cfg.get("amap_key"):
        ini_file = os.path.join(base_dir, "config.ini")
        if os.path.isfile(ini_file):
            try:
                cp = configparser.ConfigParser()
                cp.read(ini_file, encoding="utf-8")
                key = cp.get("User", "map_key", fallback="").strip()
                if key:
                    cfg["amap_key"] = key
                    cfg["inherited_from_ini"] = True
            except Exception:
                pass
    return cfg


def save_map_config(payload: Dict[str, Any], base_dir: str = BASE_DIR) -> Dict[str, Any]:
    """持久化保存地图配置到 map_config.json。"""
    cfg = get_map_config(base_dir)
    cfg.pop("inherited_from_ini", None)
    allowed_keys = {
        "provider", "map_type", "amap_key", "amap_security_code",
        "baidu_ak", "tencent_key", "osm_tile_url", "fallback_canvas",
    }
    for k in allowed_keys:
        if k in payload:
            cfg[k] = payload[k]
    cfg_file = os.path.join(base_dir, MAP_CONFIG_FILE)
    with open(cfg_file, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg

