# -*- coding: utf-8 -*-
"""Yun2Face 网页控制台 —— 照片与标注的增删改查 + 一键预检。

设计取舍
--------
- **零新依赖**：只用 Python 标准库 http.server + 项目已有的 Pillow/opencv。
  网页端是原生 JS，没有构建步骤、没有 node_modules。
- **只监听 127.0.0.1**：这是本机工具，不做对外服务；所有写操作都限制在
  `--photos` / `--annos` 两个数据目录内，并做文件名白名单校验（防目录穿越）。
- **复用同一套规则**：预检调用 face_check.check_photo（其规则来自 core/rules.py），
  不在网页层重新实现任何阈值。
- **是否重构路径布局**：不改。数据仍按项目要求的 `<root>/<学号>/<文件名>` 放置，
  所以网页里改完的标注可以直接给项目用。

启动：
    python web_app.py                 # 默认 8765，自动打开浏览器
    python web_app.py --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import configparser
import glob
import hashlib
import io
import itertools
import json
import mimetypes
import os
import posixpath
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_LIB = os.path.join(HERE, "lib")
# lib/ may contain Windows bundled wheels; only use it on Windows.
if os.name == "nt" and os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.append(_LIB)

import core  # noqa: E402
import face_check  # noqa: E402
import task_tools  # noqa: E402
import web as route_web  # noqa: E402
from PIL import Image  # noqa: E402

WEB_DIR = os.path.join(HERE, "web")
CACHE_DIR = os.path.join(HERE, "out", "web_cache")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
# 学号/目录名：不含点号，避免出现类似 ".." 的段
NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}$")
# 文件名：允许点号（扩展名），但显式禁止 "." ".." 与路径分隔符（见 _safe_name）
FILE_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff. ]{1,120}$")
STEM_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff ]{1,96}$")

# 全局：检测器与文件写入的并发保护（Pillow/cv2 的模块级对象不是线程安全的）
_LOCK = threading.RLock()

CFG = {
    # 云端部署使用项目根目录下的轻量账号资料目录。
    "config_root": os.path.join(HERE, "students_cofig"),
    "photos": os.path.join(HERE, "sutdents_face"),
    "annos": os.path.join(HERE, "students_face_json"),
    # 任务表库：默认指向当前项目的打表目录（--tasks 可改）
    "tasks": os.path.join(HERE, "tasks_fch"),
    "split_count": 10,
    "strides": 0.8,
    # 运行控制台
    "project": HERE,
    # 兼容旧参数，但默认不再依赖外部 Windows 项目目录。
    "projects_root": HERE,
    "project_glob": "students_cofig*",
    "python": "",
    "dry_home": os.path.join(HERE, "dry_run_home.json"),
}

RUN_DATA_DIR = os.path.join(HERE, "web_data", "runs")
RUNS_FILE = os.path.join(RUN_DATA_DIR, "runs.json")
SCHEDULES_FILE = os.path.join(RUN_DATA_DIR, "schedules.json")
HISTORY_DIR = os.path.join(HERE, "students_history")

TASK_FOLDER_RE = re.compile(r"^tasks_[A-Za-z0-9_\-]{1,32}$")

# ---------------------------------------------------------------- 运行控制台
# 每个 run：{id, argv, proc, lines, status, started, ended, returncode, mode, note}
RUNS: Dict[str, Dict[str, Any]] = {}
_RUN_SEQ = itertools.count(1)
RUN_LINE_CAP = 3000          # 每个 run 保留的最大行数（防内存爆）


def _persist_run(run: Dict[str, Any]) -> None:
    """持久化运行元数据和日志；不写入 proc 等不可序列化对象。"""
    with _LOCK:
        os.makedirs(RUN_DATA_DIR, exist_ok=True)
        record = {k: v for k, v in run.items() if k not in {"proc"}}
        record["lines"] = list(run.get("lines") or [])[-RUN_LINE_CAP:]
        records = {}
        if os.path.isfile(RUNS_FILE):
            try:
                with open(RUNS_FILE, "r", encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
                try:
                    records = json.loads(raw) or {}
                except json.JSONDecodeError:
                    # 兼容旧版本并发写入留下的尾部残片，尽量恢复前面的完整对象。
                    records, _ = json.JSONDecoder().raw_decode(raw.lstrip())
            except Exception:
                records = {}
        records[str(run["id"])] = record
        tmp = RUNS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, RUNS_FILE)


def _load_persisted_runs() -> None:
    if not os.path.isfile(RUNS_FILE):
        return
    try:
        with open(RUNS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        try:
            records = json.loads(raw) or {}
        except json.JSONDecodeError:
            records, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        for rid, record in records.items():
            record["id"] = rid
            record["proc"] = None
            record["lines"] = list(record.get("lines") or [])[-RUN_LINE_CAP:]
            if record.get("status") == "running":
                record["status"] = "stopped"
                record["verdict"] = "网页服务重启时中断"
            RUNS[rid] = record
        nums = [int(str(r).lstrip("r")) for r in RUNS if str(r).lstrip("r").isdigit()]
        global _RUN_SEQ
        _RUN_SEQ = itertools.count(max(nums, default=0) + 1)
    except Exception:
        return


def _mask_line(line: str) -> str:
    """日志脱敏：token / sign 之类的值不原样显示。"""
    line = re.sub(r'(?i)("?token"?\s*[:=]\s*")([^"]{4,})(")',
                  lambda m: m.group(1) + m.group(2)[:4] + "***" + m.group(3), line)
    line = re.sub(r"(?i)(token为[:：]\s*)(\S{4})\S*", r"\1\2***", line)
    return line


def _reader(run_id: str, stream) -> None:
    """后台线程：逐行读子进程输出，带脱敏与容量上限。"""
    run = RUNS.get(run_id)
    if run is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            # tqdm 用 \r 覆盖同一行，这里拆开，避免日志被压成一行
            for piece in text.split("\r"):
                if piece.strip():
                    run["lines"].append(_mask_line(piece))
        if len(run["lines"]) > RUN_LINE_CAP:
            run["lines"] = run["lines"][-RUN_LINE_CAP:]
        _persist_run(run)
    except Exception as exc:  # noqa: BLE001
        run["lines"].append(f"[读输出异常] {type(exc).__name__}: {exc}")
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _watcher(run_id: str) -> None:
    """后台线程：等进程结束，判定结果。"""
    run = RUNS.get(run_id)
    if run is None:
        return
    proc = run["proc"]
    rc = proc.wait()
    run["returncode"] = rc
    run["ended"] = time.time()
    tail = "\n".join(run["lines"][-40:])

    # 从输出里识别项目自己打印的终止原因。要区分两类：
    #   (a) 项目**按策略主动停止**（业务拒绝/人脸核验/输入预检）——不是脚本崩溃，
    #       是它拒绝在条件不满足时继续，退出码非 0 属预期行为；
    #   (b) 真正的脚本异常（Traceback 且没有上面的策略标记）。
    POLICY_STOP = (
        ("[dry-run 停止门]", "按策略停止：任务要求人脸核验，但未提供人脸源"),
        ("[业务拒绝]", "服务端业务拒绝"),
        ("[人脸停止]", "人脸窗口未确认"),
        ("[停止]", "按策略停止"),
        ("[失败]", "结果未知/解码失败"),
        ("照片源预检失败", "人脸照片未通过预检"),
        ("视频源预检失败", "视频未通过预检"),
    )
    for key, label in POLICY_STOP:
        if key in tail:
            run["verdict"] = label
            run["policy_stop"] = True
            break
    if not run.get("verdict"):
        if "offline_client_compatibility" in tail:
            run["verdict"] = "离线演练完成"
        elif "Traceback" in tail:
            run["verdict"] = "脚本异常（见输出末尾 Traceback）"
        elif rc == 0:
            run["verdict"] = "正常结束"

    # 状态：用户手动停止 > 按策略停止 > 退出码
    if run.get("stopped_by_user"):
        run["status"] = "stopped"
    elif run.get("policy_stop"):
        run["status"] = "stopped"
    elif rc == 0:
        run["status"] = "done"
    else:
        run["status"] = "failed"
    _persist_run(run)
    # 单表运行结束就清掉临时目录（失败/被停止也一样）。
    _cleanup_task_pick(run.get("pick_dir"))


def _python_exe() -> str:
    exe = CFG.get("python") or sys.executable
    return exe


def _main_argv(opts: Dict[str, Any]) -> List[str]:
    """把前端选项翻译成 main.py 的参数。绝不产生需要交互输入的命令行。

    **每个项目（学号）用它自己那份 main.py 与 config.ini**：opts["project"] 决定一切，
    不再有一个全局 project。人脸源用「学号 + 文件名」表达，这里解析成绝对路径。
    """
    # 代码和任务表统一来自主项目，账号目录只提供 config.ini 与人脸素材。
    proj = os.path.abspath(CFG["project"])
    argv = [_python_exe(), "-X", "utf8", os.path.join(proj, "main.py")]
    argv += ["-f", os.path.abspath(opts["config"])]
    if opts.get("mode") == "dry":
        argv += ["--dry-run", "--dry-home", os.path.abspath(CFG["dry_home"])]
    else:
        argv += ["-a"]
    if opts.get("task_dir"):
        argv += ["-t", os.path.abspath(opts["task_dir"])]
    if opts.get("drift"):
        argv += ["-d"]

    stu = opts.get("face_student")
    if stu:
        stu = _safe_name(stu)
        photo = photo_path(stu, opts["face_photo"])            # 已含白名单校验
        if not os.path.isfile(photo):
            raise ValueError(f"人脸照片不存在: {photo}")
        argv += ["--face-photo", photo]
        stem = _safe_name(opts.get("face_stem") or "", STEM_RE)
        det = anno_path(stu, stem)
        if not os.path.isfile(det):
            raise ValueError(f"该照片还没有标注文件: {det}（先在『人脸素材』页生成标注）")
        argv += ["--face-detection", det]

    if opts.get("mode") != "dry":
        # 学号项目的 main.py 支持业务 code=401 自动续期；网页运行必须开启
        # 配置写回，否则新 token 只存在本次进程内。
        argv += ["--login-retries", "2", "--login-retry-delay", "3",
                 "--reauth-save-token"]
    return argv


def _task_pick_dir(run_id: str) -> str:
    """单表运行的临时目录：只放本次选中的那张任务表。"""
    return os.path.join(HERE, "out", "task_pick", str(run_id))


def _cleanup_task_pick(pick_dir: Optional[str]) -> None:
    if not pick_dir:
        return
    import shutil
    shutil.rmtree(pick_dir, ignore_errors=True)


def _prepare_task_dir(proj: str, folder: str, want: str, run_id: str):
    """解析传给 -t 的任务表目录，返回 (task_dir, note, pick_dir)。

    main.py 在 -a 下会对 `-t` 目录里的 json 做 random.choice。因此**指定单表时
    不能直接把整个 tasks_* 目录传进去**，否则实际跑的可能是别的表。做法与旧
    面板 web.py 一致：把选中的表复制到一个只含它的临时目录，再把 -t 指向那里。
    """
    if not folder:
        return "", "", None
    if not TASK_FOLDER_RE.match(folder):
        raise ValueError(f"非法任务表目录名: {folder!r}")
    tdir = os.path.abspath(os.path.join(proj, folder))
    if os.path.commonpath([tdir, proj]) != proj:
        raise ValueError(f"任务表目录越出项目范围: {folder!r}")
    if not os.path.isdir(tdir):
        raise ValueError(f"任务表目录不存在: {tdir}")
    names = sorted(f for f in os.listdir(tdir) if f.lower().endswith(".json"))
    if not names:
        raise ValueError(f"任务表目录里没有 json: {tdir}")
    if not want:
        note = f"目录内有 {len(names)} 个表，项目将随机挑选" if len(names) > 1 else ""
        return tdir, note, None
    if want not in names:
        raise ValueError(f"该目录里没有 {want}（现有: {names}）")
    if len(names) == 1:
        return tdir, "", None

    import shutil
    pick_dir = _task_pick_dir(run_id)
    os.makedirs(pick_dir, exist_ok=True)
    for old in os.listdir(pick_dir):          # run_id 唯一，正常不会有残留
        os.remove(os.path.join(pick_dir, old))
    src = os.path.join(tdir, want)
    dst = os.path.join(pick_dir, want)
    shutil.copyfile(src, dst)
    if os.path.getsize(src) != os.path.getsize(dst):
        _cleanup_task_pick(pick_dir)
        raise ValueError(f"任务表复制校验失败: {src} → {dst}")
    return pick_dir, f"已锁定任务表 {want}（单表目录，项目不会随机换表）", pick_dir


def start_run(opts: Dict[str, Any]) -> Dict[str, Any]:
    """启动一个 main.py 实例。

    - **项目由 opts["project"] 决定**（一个项目=一个账号=一份 config + 自己的 tasks_* 目录）。
    - **指定 task_file 时把该表复制到单表目录再 -t**，避免 main.py 随机换表。
    - 未指定 task_file 时按项目自身行为从目录里随机挑一张，并在 note 里如实说明。
    """
    import subprocess

    with _LOCK:
        account_dir = os.path.abspath(opts.get("project") or CFG["project"])
        proj = os.path.abspath(CFG["project"])
        if not os.path.isfile(os.path.join(proj, "main.py")):
            raise ValueError(f"主项目目录里没有 main.py: {proj}")
        cfgp = os.path.abspath(opts["config"])
        if not os.path.isfile(cfgp):
            raise ValueError(f"配置文件不存在: {cfgp}")
        notes = []
        if opts.get("mode") != "dry":
            renewal = ensure_real_credentials(cfgp)
            if renewal.get("renewed"):
                notes.append("已自动续期凭据；" + str(renewal.get("reason", "")))

        # 先分配 run_id（单表临时目录要用它），校验失败时只浪费一个编号。
        run_id = f"r{next(_RUN_SEQ)}"
        folder = (opts.get("task_folder") or "").strip()
        task_dir, task_note, pick_dir = _prepare_task_dir(
            proj, folder, str(opts.get("task_file") or ""), run_id)
        if task_note:
            notes.append(task_note)

        try:
            argv = _main_argv(dict(opts, task_dir=task_dir))
        except Exception:
            _cleanup_task_pick(pick_dir)
            raise

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"          # 否则日志会攒到进程结束才出来
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)            # 用项目自己的环境，别混入本工具的 lib
        try:
            proc = subprocess.Popen(
                argv, cwd=proj, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except Exception:
            _cleanup_task_pick(pick_dir)
            raise
        note = "；".join(x for x in notes if x)
        RUNS[run_id] = {
            "id": run_id, "argv": argv, "proc": proc, "lines": [],
            "status": "running", "started": time.time(), "ended": None,
            "returncode": None, "mode": opts.get("mode", "real"),
            "task_file": opts.get("task_file"), "task_folder": folder,
            "config": cfgp, "project": os.path.basename(account_dir),
            "student": opts.get("student") or "",
            "account_dir": account_dir,
            "note": note, "verdict": "", "pick_dir": pick_dir,
        }
        threading.Thread(target=_reader, args=(run_id, proc.stdout), daemon=True).start()
        threading.Thread(target=_watcher, args=(run_id,), daemon=True).start()
        _persist_run(RUNS[run_id])
        return {"id": run_id, "argv": argv, "note": note}


def run_view(run_id: str, since: int = 0) -> Dict[str, Any]:
    run = RUNS.get(run_id)
    if run is None:
        raise FileNotFoundError(f"没有这个运行: {run_id}")
    lines = run["lines"]
    return {"id": run_id, "status": run["status"], "verdict": run.get("verdict", ""),
            "returncode": run["returncode"], "mode": run["mode"],
            "task_file": run.get("task_file"), "task_folder": run.get("task_folder"),
            "project": run.get("project"), "config": run.get("config"),
            "started": run["started"], "ended": run["ended"],
            "elapsed": round((run["ended"] or time.time()) - run["started"], 1),
            "total_lines": len(lines), "since": since,
            "lines": lines[since:], "argv": " ".join(run["argv"])}


def stop_run(run_id: str, force: bool = False) -> Dict[str, Any]:
    run = RUNS.get(run_id)
    if run is None:
        raise FileNotFoundError(f"没有这个运行: {run_id}")
    proc = run["proc"]
    run["stopped_by_user"] = True
    if proc is None:
        run["status"] = "stopped"
        _persist_run(run)
        return {"id": run_id, "status": "stopped"}
    if proc.poll() is None:
        if force:
            proc.kill()
        else:
            proc.terminate()
        # 给 5 秒优雅退出，超时再强杀
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
    return {"id": run_id, "status": RUNS[run_id]["status"]}


def list_runs() -> List[Dict[str, Any]]:
    out = []
    for rid, run in RUNS.items():
        out.append({"id": rid, "status": run["status"], "verdict": run.get("verdict", ""),
                    "mode": run["mode"], "task_file": run.get("task_file"),
                    "task_folder": run.get("task_folder"),
                    "project": run.get("project"), "config": run.get("config"),
                    "note": run.get("note", ""),
                    # 实例列表要显示"运行开始时间"：老记录可能没有 started 字段，
                    # 用 .get 兼容，前端把 None 显示成 "—"。
                    "started": run.get("started"), "ended": run.get("ended"),
                    "elapsed": round((run.get("ended") or time.time()) -
                                     (run.get("started") or time.time()), 1),
                    "lines": len(run["lines"]), "returncode": run["returncode"]})
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


SCHEDULES: Dict[str, Dict[str, Any]] = {}
_SCHEDULE_SEQ = itertools.count(1)
# 定时任务错过计划时间后的宽限：只有晚到 15 分钟内才补跑；更晚（服务停机/任务停用
# 期间错过的）直接跳过并顺延到下一次，避免凌晨/重启后把过期真实任务全补跑一遍。
SCHEDULE_GRACE_SEC = 15 * 60
# 同一轮有多个任务到点时，相邻启动之间留几秒，降低并发登录/建记录的冲击。
SCHEDULE_STAGGER_SEC = 3.0
# 定时任务固定按“北京时间（UTC+8）”解释，**不跟随服务器本地时区**。
# 云服务器/容器默认常常是 UTC：如果直接用服务器本地时间，设 07:10 会在北京时间
# 15:10 触发、19:20 会在次日 03:20 触发，必然被学校服务端以"不在跑步时间"拒绝。
# 需要别的时区时用环境变量 YUN_SCHEDULE_TZ_OFFSET_MIN（分钟，东八区=480）覆盖。
def _schedule_tz_offset_minutes() -> int:
    try:
        value = int(os.environ.get("YUN_SCHEDULE_TZ_OFFSET_MIN", "480"))
    except (TypeError, ValueError):
        value = 480
    return value if -12 * 60 <= value <= 14 * 60 else 480


SCHEDULE_TZ_OFFSET_MIN = _schedule_tz_offset_minutes()
SCHEDULE_TZ = timezone(timedelta(minutes=SCHEDULE_TZ_OFFSET_MIN))


def schedule_timezone_info() -> Dict[str, Any]:
    """定时时区信息（供页面显示，避免“下次时间”与服务器时区不一致的困惑）。"""
    total = SCHEDULE_TZ_OFFSET_MIN
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total), 60)
    server = datetime.now().astimezone().utcoffset() or timedelta(0)
    server_min = int(server.total_seconds() // 60)
    server_sign = "+" if server_min >= 0 else "-"
    s_hh, s_mm = divmod(abs(server_min), 60)
    return {
        "offset_minutes": total,
        "label": f"UTC{sign}{hh:02d}:{mm:02d}",
        "server_offset_minutes": server_min,
        "server_label": f"UTC{server_sign}{s_hh:02d}:{s_mm:02d}",
        "matches_server": server_min == total,
    }


def _persist_schedules() -> None:
    os.makedirs(RUN_DATA_DIR, exist_ok=True)
    tmp = SCHEDULES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(SCHEDULES, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, SCHEDULES_FILE)


def _load_schedules() -> None:
    global _SCHEDULE_SEQ
    if not os.path.isfile(SCHEDULES_FILE):
        return
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as fh:
            SCHEDULES.update(json.load(fh) or {})
        now = time.time()
        changed = False
        for item in SCHEDULES.values():
            # 兼容旧版“间隔分钟”记录，迁移为每天 07:30。
            if not item.get("time_of_day"):
                item["time_of_day"] = "07:30"
                changed = True
            # 纠正“按服务器本地时区算出来”的旧 next_run：
            # 例如服务器是 UTC 时，07:10 被存成 UTC 07:10（=北京时间 15:10）。
            # 判断标准：next_run 落到定时时区后的小时/分钟必须等于设定值。
            if not _schedule_next_run_matches(item):
                item["next_run"] = _next_daily(item.get("time_of_day", "07:30"), now)
                changed = True
        nums = [int(str(k).lstrip("s")) for k in SCHEDULES if str(k).lstrip("s").isdigit()]
        _SCHEDULE_SEQ = itertools.count(max(nums, default=0) + 1)
        if changed:
            _persist_schedules()
    except Exception:
        return


def list_schedules() -> List[Dict[str, Any]]:
    out = list(SCHEDULES.values())
    out.sort(key=lambda x: x.get("created", 0), reverse=True)
    return out


def _validate_time_of_day(value, default: str = "07:30") -> str:
    """校验每天运行时间，返回规范化的 HH:MM（非法值抛 ValueError）。"""
    raw = str(value or default).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise ValueError(f"每天运行时间格式应为 HH:MM，收到: {raw!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"每天运行时间超出范围: {raw!r}")
    return f"{hour:02d}:{minute:02d}"


def _clean_schedule_options(raw) -> Dict[str, Any]:
    """定时任务的 options 必须是对象；mode 只允许 dry/real。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("定时任务选项(options)必须是对象")
    options = dict(raw)
    mode = options.get("mode")
    if mode is not None and mode not in ("dry", "real"):
        raise ValueError(f"未知运行模式: {mode!r}")
    return options


def create_schedule(body: Dict[str, Any]) -> Dict[str, Any]:
    """新建定时任务（保存到 SCHEDULES 并落盘）。"""
    now = time.time()
    sid = f"s{next(_SCHEDULE_SEQ)}"
    tod = _validate_time_of_day(body.get("time_of_day"))
    name = str(body.get("name") or "").strip()[:80] or sid
    item = {
        "id": sid, "name": name, "time_of_day": tod,
        "enabled": bool(body.get("enabled", True)), "created": now,
        "next_run": _next_daily(tod, now),
        "options": _clean_schedule_options(body.get("options")),
        "last_run": None, "last_started": None, "last_error": "",
    }
    SCHEDULES[sid] = item
    _persist_schedules()
    return item


def update_schedule(body: Dict[str, Any]) -> Dict[str, Any]:
    """修改已保存的定时任务：名称/时间/模式与任务选项/启用状态。

    - 只改传入的字段，未传的字段保持原值；
    - options 做浅合并，允许一次只改其中几项；
    - 时间变更后立即重算 next_run，编辑后不再保留旧的 last_error。
    """
    sid = str(body.get("id") or "")
    item = SCHEDULES.get(sid)
    if item is None:
        raise KeyError(sid)
    now = time.time()
    if "name" in body:
        name = str(body.get("name") or "").strip()[:80]
        item["name"] = name or sid
    if "time_of_day" in body:
        tod = _validate_time_of_day(body.get("time_of_day"))
        item["time_of_day"] = tod
        item["next_run"] = _next_daily(tod, now)
    else:
        # 只改名/换表时保留原来的一次性安排（含宽限内即将触发的）；
        # 只有当 next_run 已经过期到不可能再补跑时，才顺延到下一次。
        current = float(item.get("next_run", 0) or 0)
        if current <= 0 or now - current > SCHEDULE_GRACE_SEC:
            item["next_run"] = _next_daily(item.get("time_of_day", "07:30"), now)
    if "enabled" in body:
        item["enabled"] = bool(body.get("enabled"))
    if "options" in body:
        merged = dict(item.get("options") or {})
        merged.update(_clean_schedule_options(body.get("options")))
        item["options"] = merged
    item["last_error"] = ""
    item["updated"] = now
    _persist_schedules()
    return item


def _refresh_next_run(item: Dict[str, Any], now: float | None = None) -> None:
    """把 next_run 重排到下一个未来时间点（启用/修改时间时用，绝不补跑）。"""
    now = time.time() if now is None else now
    item["next_run"] = _next_daily(item.get("time_of_day", "07:30"), now)


def set_schedule_enabled(sid: str, enabled: bool) -> Dict[str, Any]:
    """启用/停用定时任务。

    **启用时一定重算 next_run**：停用期间错过的运行不能补跑，否则在凌晨/半夜
    重新启用会把过期的真实任务立刻触发一次。
    """
    item = SCHEDULES.get(str(sid))
    if item is None:
        raise KeyError(sid)
    item["enabled"] = bool(enabled)
    if item["enabled"]:
        _refresh_next_run(item)
        item["last_error"] = ""
    _persist_schedules()
    return item


def _schedule_step(now: float | None = None, *, stagger: float = 0.0) -> int:
    """处理一轮到点的定时任务，返回发生状态变化的条数（调用方据此落盘）。

    - 晚到不超过 SCHEDULE_GRACE_SEC：照常执行；
    - 晚到超过宽限：判定为错过，跳过本次并把 next_run 顺延到下一个未来时间；
    - 同一轮多个任务：相邻启动之间 sleep(stagger) 秒，错峰启动。
    """
    now = time.time() if now is None else now
    changed = 0
    fired = 0
    for item in list(SCHEDULES.values()):
        if not item.get("enabled", True):
            continue
        next_run = float(item.get("next_run", 0) or 0)
        if next_run > now:
            continue
        if now - next_run > SCHEDULE_GRACE_SEC:
            # 服务停机或任务停用期间错过的运行：跳过，不补跑。
            item["next_run"] = _next_daily(item.get("time_of_day", "07:30"), now)
            item["last_skipped"] = now
            item["skipped_count"] = int(item.get("skipped_count", 0) or 0) + 1
            changed += 1
            continue
        try:
            if fired and stagger > 0:
                time.sleep(stagger)
            result = start_run(dict(item.get("options") or {}))
            item["last_run"] = result.get("id")
            item["last_started"] = now
            item["last_fired"] = now
            item["next_run"] = _next_daily(item.get("time_of_day", "07:30"), now)
            item["last_error"] = ""
            fired += 1
        except Exception as exc:  # noqa: BLE001
            item["last_error"] = str(exc)[:300]
            item["next_run"] = now + 300     # 启动失败：5 分钟后重试
        changed += 1
    return changed


def _schedule_loop() -> None:
    while True:
        try:
            if _schedule_step(stagger=SCHEDULE_STAGGER_SEC):
                _persist_schedules()
        except Exception:  # noqa: BLE001
            # 定时线程不能因为一次异常退出，否则之后所有任务都不再触发。
            traceback.print_exc()
        time.sleep(10)


def _parse_time_of_day(value, default=(7, 30)) -> tuple:
    """HH:MM → (hour, minute)，非法值回退到默认（不抛异常，供调度/迁移使用）。"""
    try:
        hh, mm = [int(x) for x in str(value).split(":", 1)]
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:  # noqa: BLE001
        pass
    return default


def _next_daily(value: str, now: float | None = None) -> float:
    """计算下一个 **定时时区（默认北京时间 UTC+8）** 的 HH:MM 运行点。

    这里刻意不用 `datetime.fromtimestamp()` 的服务器本地时区：容器/云服务器常为
    UTC，会让 07:10 实际变成北京时间 15:10。改为显式绑定时区，服务器时区不再影响。
    """
    now_ts = time.time() if now is None else now
    hh, mm = _parse_time_of_day(value)
    now_dt = datetime.fromtimestamp(now_ts, SCHEDULE_TZ)
    target = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target.timestamp() <= now_ts:
        target += timedelta(days=1)
    return target.timestamp()


def _schedule_next_run_matches(item: Dict[str, Any]) -> bool:
    """next_run 落在定时时区后，小时/分钟是否等于设定时间。

    服务器时区变化（或历史版本用本地时区计算）会让 07:10 变成北京时间 15:10，
    此时小时/分钟不匹配，启动时需要按定时时区重排。
    """
    try:
        next_run = float(item.get("next_run") or 0)
    except (TypeError, ValueError):
        return False
    if next_run <= 0:
        return False
    local = datetime.fromtimestamp(next_run, SCHEDULE_TZ)
    return (local.hour, local.minute) == _parse_time_of_day(item.get("time_of_day", "07:30"))


# ---------------------------------------------------------------- 登录（真实运行的前置）
def is_auth_failure(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    text = str(getattr(exc, "msg", "") or exc).lower()
    return (code in {401, "401", 403, "403"} or status in {401, 403}
            or any(word in text for word in ("token", "unauthor", "auth", "登录", "凭据", "过期")))


def _load_main_module():
    """加载项目根 main.py，避开 core/yun_http.py 的人脸 stub。"""
    import importlib
    core_dir = os.path.join(HERE, "core")
    while core_dir in sys.path:
        sys.path.remove(core_dir)
    if HERE in sys.path:
        sys.path.remove(HERE)
    sys.path.insert(0, HERE)
    root_yun_http = os.path.abspath(os.path.join(HERE, "yun_http.py"))
    loaded = sys.modules.get("yun_http")
    if loaded and os.path.abspath(getattr(loaded, "__file__", "")) != root_yun_http:
        del sys.modules["yun_http"]
    was_loaded = "main" in sys.modules
    module = importlib.import_module("main")
    return importlib.reload(module) if was_loaded else module


def persist_login_result(config_name: str, result: tuple) -> None:
    """把 Login.main 返回的凭据写回指定学号配置。"""
    token, device_id, device_name, uuid_value, sys_edition = result
    conf = configparser.ConfigParser()
    conf.read(config_name, encoding="utf-8")
    if not conf.has_section("User"):
        conf.add_section("User")
    values = {
        "token": token,
        "device_id": device_id,
        "device_name": device_name,
        "uuid": uuid_value,
        "sys_edition": sys_edition,
    }
    for key, value in values.items():
        conf.set("User", key, str(value or ""))
    with open(config_name, "w", encoding="utf-8") as fh:
        conf.write(fh)


def do_login(config_name: str, save_token: bool = False) -> Dict[str, Any]:
    """在项目环境里跑一次登录并把新 token 写回配置。

    为什么要它：真实运行要求 config 里有 token，否则子进程会停在"是否登录"的
    交互询问上（我们以 stdin=DEVNULL 启动，会直接 EOF 失败）。这里用子进程调用
    项目自己的 tools/Login.py，输入用管道喂进去，不修改项目代码。

    注意：登录会让手机 App 的会话失效——这是同账号多客户端的固有代价。
    """
    proj = CFG["project"]
    cfgp = os.path.abspath(config_name)
    if not os.path.isfile(cfgp):
        return {"ok": False, "error": f"配置文件不存在: {cfgp}"}
    snippet = (
        "import sys, configparser;"
        "sys.path.insert(0, r'%s');"
        "from tools.Login import Login;"
        "cfg = configparser.ConfigParser(); cfg.read(r'%s', encoding='utf-8');"
        "dn = cfg.get('User','device_name',fallback='') or 'Xiaomi';"
        "se = cfg.get('User','sys_edition',fallback='') or '14';"
        "import builtins; _n=[0];\n"
        "def _in(p=''):\n"
        "    _n[0]+=1\n"
        "    return dn if _n[0]==1 else se\n"
        "builtins.input=_in\n"
        "r = Login.main(r'%s')\n"
        "import base64, json\n"
        "print('LOGIN_DATA', base64.b64encode(json.dumps(r).encode()).decode() if r else '-')\n"
        "print('LOGIN_OK' if r else 'LOGIN_FAIL')\n"
        "print('TOKEN', (r[0][:4]+'***') if r else '-')\n"
    ) % (proj, cfgp, cfgp)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    try:
        p = subprocess.run([_python_exe(), "-X", "utf8", "-c", snippet],
                           cwd=proj, env=env, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "登录超时（300s）"}
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    ok = "LOGIN_OK" in out
    result = None
    for line in out.splitlines():
        if line.startswith("LOGIN_DATA "):
            try:
                result = tuple(json.loads(__import__("base64").b64decode(line.split(" ", 1)[1]).decode()))
            except Exception:
                result = None
    if ok and result and save_token:
        persist_login_result(cfgp, result)
    return {"ok": ok and result is not None, "saved": bool(ok and result and save_token),
            "log": _mask_line("\n".join(out.strip().splitlines()[-12:])),
            "error": "" if ok else (err.strip().splitlines() or ["登录失败"])[-1][:200]}


def ensure_real_credentials(config_path: str) -> dict:
    """真实运行前探测凭据；过期或为空时自动登录并写回配置。"""
    conf = configparser.ConfigParser()
    conf.read(config_path, encoding="utf-8")
    token = (conf.get("User", "token", fallback="") or "").strip()
    if not token:
        result = do_login(config_path, save_token=True)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "登录失败，无法获得凭据")
        return {"renewed": True, "reason": "token为空"}
    try:
        module = _load_main_module()
        module.set_args(config_path)
        # 与 Yun_For_New 构造时相同的准入请求，覆盖真实启动前的凭据校验。
        module.default_client().post_json("/run/getHomeRunInfo", "")
        return {"renewed": False}
    except Exception as exc:  # noqa: BLE001
        if not is_auth_failure(exc):
            return {"renewed": False, "probe_error": str(exc)[:240]}
        result = do_login(config_path, save_token=True)
        if not result.get("ok"):
            raise ValueError("凭据已过期，自动续期失败：" + (result.get("error") or "登录失败"))
        return {"renewed": True, "reason": "凭据已过期"}


def discover_projects() -> List[Dict[str, Any]]:
    """扫描 students_cofig，把每个学号的 config.ini 作为一个账号。

    约定（本机现状就是如此）：
        <projects_root>/yunForNewVersion-<学号>/   ← 各学号的独立项目
        <projects_root>/yunForNewVersion-develop/   ← 本项目那份（JW250246）
    兼容按学校再分一层：<projects_root>/<学校>/yunForNewVersion-<学号>/

    每个项目对应用户自己的一份 config.ini 与自己的 tasks_* 目录——
    这就是"每个学号用自己文件夹里的东西"，不需要前端再猜。
    """
    root = CFG["config_root"]
    out: List[Dict[str, Any]] = []
    seen = set()
    # 云端布局：students_cofig/<学号>/config.ini
    cands = []
    if os.path.isdir(root):
        for d in sorted(glob.glob(os.path.join(root, "*"))):
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "config.ini")):
                cands.append((d, ""))
    for d, school in cands:
        d = os.path.abspath(d)
        if d in seen or not os.path.isdir(d):
            continue
        # 账号目录是轻量资料包，不再要求复制 main.py / tasks_*。
        ini = os.path.join(d, "config.ini")
        if not os.path.isfile(ini):
            continue
        seen.add(d)
        name = os.path.basename(d)
        rec: Dict[str, Any] = {
            "name": name, "path": d, "school_dir": school,
            # 账号：优先取 config 里的 [Login] username，取不到就用目录名后缀
            "student": "", "school": "", "host": "", "device": "",
            "token": False, "task_dirs": [], "error": "",
        }
        tail = name.split("yunForNewVersion-", 1)[-1]
        if tail and tail != name:
            rec["student"] = tail
        elif NAME_RE.match(name):
            rec["student"] = name
        if os.path.isfile(ini):
            try:
                conf = configparser.ConfigParser()
                conf.read(ini, encoding="utf-8")
                rec["student"] = (conf.get("Login", "username", fallback="")
                                  or rec["student"])
                rec["school"] = conf.get("Yun", "school_name", fallback="")
                rec["host"] = conf.get("Yun", "school_host", fallback="")
                rec["device"] = conf.get("User", "device_name", fallback="")
                rec["token"] = bool((conf.get("User", "token", fallback="") or "").strip())
                rec["config"] = ini
            except Exception as exc:  # noqa: BLE001
                rec["error"] = f"config 读取失败: {type(exc).__name__}: {exc}"[:140]
        else:
            rec["error"] = "该目录下没有 config.ini"
        # 所有账号共享主项目的任务表目录。
        for sub in sorted(os.listdir(CFG["project"])):
            if TASK_FOLDER_RE.match(sub) and os.path.isdir(os.path.join(CFG["project"], sub)):
                n = len([f for f in os.listdir(os.path.join(CFG["project"], sub))
                         if f.lower().endswith(".json")])
                rec["task_dirs"].append({"name": sub, "files": n})
        out.append(rec)
    # 默认项目排最前（它就是 JW250246 那份）
    def key(r):
        return (r["path"] != os.path.abspath(CFG["project"]), r["student"], r["name"])
    return sorted(out, key=key)


def list_configs() -> List[Dict[str, Any]]:
    """可用的配置文件（含 token 是否有值，避免点了真实运行才失败）。

    来源 = 每个已发现项目自己的 config.ini（一个项目=一个账号）。
    另外兼容：项目根下额外的 *.ini（多实例同一份代码的情况）。
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for proj in discover_projects():
        ini = proj.get("config")
        if ini and os.path.isfile(ini):
            seen.add(os.path.abspath(ini))
            out.append({
                "name": f"{proj['student'] or proj['name']} / config.ini",
                "path": ini, "project": proj["path"], "student": proj["student"],
                "task_dirs": proj["task_dirs"],
                "token": proj["token"], "username": proj["student"],
                "school": proj["school"], "host": proj["host"],
            })
    # 兜底：当前 --project 下其它 *.ini（比如手写的 config_2.ini）
    proj = CFG["project"]
    if os.path.isdir(proj):
        for name in sorted(os.listdir(proj)):
            if not name.lower().endswith(".ini"):
                continue
            p = os.path.abspath(os.path.join(proj, name))
            if p in seen:
                continue
            rec = {"name": name, "path": p, "project": proj, "student": "",
                   "task_dirs": []}
            try:
                conf = configparser.ConfigParser()
                conf.read(p, encoding="utf-8")
                rec.update({
                    "token": bool((conf.get("User", "token", fallback="") or "").strip()),
                    "username": conf.get("Login", "username", fallback=""),
                    "school": conf.get("Yun", "school_name", fallback=""),
                    "host": conf.get("Yun", "school_host", fallback=""),
                })
            except Exception as exc:  # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {exc}"[:120]
            out.append(rec)
    return out


# ---------------------------------------------------------------- 工具
def _safe_name(name: str, pat=NAME_RE) -> str:
    """校验单段路径名，拒绝空/穿越/非法字符。"""
    name = (name or "").strip()
    if not name or not pat.match(name):
        raise ValueError(f"非法名称: {name!r}")
    if name in (".", "..") or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法名称: {name!r}")
    return name


def _account_dir(stu: str) -> str | None:
    stu = _safe_name(stu)
    for project in discover_projects():
        if project.get("student") == stu:
            return project.get("path")
    return None


def _account_config(stu: str) -> str:
    """返回 students_cofig/<学号>/config.ini。"""
    account = os.path.join(CFG["config_root"], _safe_name(stu))
    path = os.path.join(account, "config.ini")
    if not os.path.isfile(path):
        raise ValueError(f"学号配置不存在: {path}")
    return path


def _config_account_path(stu: str) -> str:
    account = _account_dir(stu)
    if not account:
        raise ValueError(f"没有找到学号项目: {stu}")
    path = os.path.join(account, "config.ini")
    if not os.path.isfile(path):
        raise ValueError(f"学号目录缺少 config.ini: {stu}")
    return path


def _legacy_photo_path(stu: str, name: str) -> str:
    return os.path.join(CFG["photos"], _safe_name(stu), name)


def photo_path(stu: str, name: str) -> str:
    """照片绝对路径。文件名必须是单段、白名单字符、且扩展名是图片。"""
    fname = _safe_name(name, FILE_RE)
    if not fname.lower().endswith(PHOTO_EXT):
        raise ValueError(f"不是受支持的图片扩展名: {fname!r}（{PHOTO_EXT}）")
    return os.path.join(CFG["photos"], _safe_name(stu), fname)


def anno_path(stu: str, stem: str) -> str:
    stem = _safe_name(stem, STEM_RE) + ".json"
    return os.path.join(CFG["annos"], _safe_name(stu), stem)


def list_students() -> list:
    out = []
    seen = set()
    for root in (CFG["photos"], CFG["annos"]):
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            if os.path.isdir(os.path.join(root, d)) and NAME_RE.match(d):
                seen.add(d)
    # 项目目录本身也作为学号来源，即使该学号还没有照片/标注目录，
    # 也可以先在网页里选中它再上传人脸素材。
    for project in discover_projects():
        student = (project.get("student") or "").strip()
        if student and NAME_RE.match(student):
            seen.add(student)
    for d in sorted(seen):
        images = _list_images(d)
        out.append({
            "student": d,
            "photos": len(images),
            "annos": len([im for im in images if im.get("annotation", {}).get("exists")]),
        })
    return out


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _annotation_status(ap: str, photo_sha: str | None) -> dict:
    """标注文件状态：缺 / 坏 / 哈希不符 / 含过期路径 / 正常。"""
    if not os.path.isfile(ap):
        return {"exists": False, "state": "missing"}
    try:
        with open(ap, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "state": "broken", "detail": str(exc)[:120]}
    want = d.get("source_sha256")
    state = "ok"
    detail = ""
    if not want:
        state, detail = "unbound", "缺 source_sha256"
    elif photo_sha and want != photo_sha:
        state, detail = "mismatch", f"内容不符（{want[:10]}…）"
    stale = d.get("source_path")
    if stale:
        state = state if state != "ok" else "stale_path"
        detail = (detail + " " if detail else "") + f"含路径记录 {stale}"
    return {"exists": True, "state": state, "detail": detail.strip(),
            "has_source_path": bool(stale),
            "source_sha256": want, "bind_apply": d.get("bind_apply")}


def _list_images(stu: str) -> list:
    roots = [os.path.join(CFG["photos"], _safe_name(stu))]
    items = []
    seen_names = set()
    for pdir in roots:
      if not os.path.isdir(pdir):
        continue
      for name in sorted(os.listdir(pdir)):
        if name in seen_names:
            continue
        seen_names.add(name)
        if not name.lower().endswith(PHOTO_EXT):
          continue
        stem = os.path.splitext(name)[0]
        if not STEM_RE.match(stem):
          continue
        p = os.path.join(pdir, name)
        rec = {"name": name, "stem": stem, "bytes": os.path.getsize(p)}
        try:
            with Image.open(p) as im:
                rec["size"] = list(im.size)
                rec["exif_orientation"] = im.getexif().get(274, None)
        except Exception as exc:  # noqa: BLE001
            rec["size"] = None
            rec["error"] = f"无法解码: {exc}"[:120]
        sha = None
        if rec.get("size"):
            sha = _sha256_file(p)
            rec["sha256"] = sha
        rec["annotation"] = _annotation_status(anno_path(stu, stem), sha)
        items.append(rec)
    return items


def _thumb(stu: str, name: str, max_side: int = 420) -> bytes:
    """生成缩略图（带磁盘缓存；原图很大，直接发给浏览器会卡）。"""
    p = photo_path(stu, name)
    st = os.stat(p)
    key = f"{stu}__{name}__{int(st.st_mtime)}__{st.st_size}__{max_side}.jpg"
    cache = os.path.join(CACHE_DIR, key)
    if os.path.isfile(cache):
        with open(cache, "rb") as fh:
            return fh.read()
    os.makedirs(CACHE_DIR, exist_ok=True)
    with Image.open(p) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    data = buf.getvalue()
    with open(cache, "wb") as fh:
        fh.write(data)
    return data


# ---------------------------------------------------------------- 业务动作
def do_check(stu: str, name: str, fix_paths: bool = False) -> dict:
    p = photo_path(stu, name)
    stem = os.path.splitext(name)[0]
    ap = anno_path(stu, stem)
    det = ap if os.path.isfile(ap) else None
    with _LOCK:
        try:
            res = face_check.check_photo(p, det, auto_detect=(det is None),
                                         want_upload=False, fix_paths=fix_paths)
        except core.FaceInputError as exc:
            return {"ok": False, "photo": name, "stage": "input",
                    "errors": [str(exc)], "metrics": None}
    res.pop("_upload_bytes_data", None)
    return res


def do_autodetect(stu: str, name: str, save: bool = True) -> dict:
    """重新用 YuNet 检测并写标注（会覆盖已有标注，调用方需确认）。"""
    p = photo_path(stu, name)
    stem = os.path.splitext(name)[0]
    ap = anno_path(stu, stem)
    with _LOCK:
        det = face_check.detect_face(p)
    if det is None:
        return {"ok": False, "errors": ["自动检测未找到人脸"]}
    ann = face_check.detection_to_json(p, det)
    if save:
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w", encoding="utf-8") as fh:
            json.dump(ann, fh, ensure_ascii=False, indent=2)
    return {"ok": True, "annotation": ann, "saved": save}


def do_preview(stu: str, name: str) -> bytes:
    """带门限参考线的预览图（复用 face_check.render_show 的绘制约定）。"""
    p = photo_path(stu, name)
    stem = os.path.splitext(name)[0]
    ap = anno_path(stu, stem)
    with _LOCK:
        if os.path.isfile(ap):
            bundle = core.load_detection_bundle(ap)
        else:
            det = face_check.detect_face(p)
            if det is None:
                raise core.FaceInputError("未检出人脸，无法生成预览")
            bundle = core.DetectionBundle(static=core.FaceDetection(
                box=tuple(det["box"]), points=[tuple(q) for q in det["points"]],
                score=det["score"], space="image"))
        out = os.path.join(CACHE_DIR, f"preview__{stu}__{stem}.jpg")
        os.makedirs(CACHE_DIR, exist_ok=True)
        face_check.render_show(p, bundle, out)
        with open(out, "rb") as fh:
            return fh.read()


# ---------------------------------------------------------------- 任务表
TASK_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff. ]{1,120}\.json$", re.IGNORECASE)


def task_path(name: str) -> str:
    """任务表绝对路径（全局库，用 --tasks 指定的那个目录）。"""
    n = (name or "").strip()
    if not n or "/" in n or "\\" in n or ".." in n or not TASK_RE.match(n):
        raise ValueError(f"非法任务表名: {name!r}")
    return os.path.join(CFG["tasks"], n)


def task_path_in(project_dir: str, folder: str, name: str) -> str:
    """某个项目、某个 tasks_* 目录下的任务表路径（每个学号用自己那份）。"""
    proj = os.path.abspath(project_dir or CFG["project"])
    fd = (folder or "").strip()
    if not TASK_FOLDER_RE.match(fd or ""):
        raise ValueError(f"非法任务表目录名: {folder!r}")
    base = os.path.abspath(os.path.join(proj, fd))
    # 目录必须在项目内，防穿越
    if os.path.commonpath([base, proj]) != proj:
        raise ValueError(f"任务表目录越出项目范围: {folder!r}")
    n = (name or "").strip()
    if not n or "/" in n or "\\" in n or ".." in n or not TASK_RE.match(n):
        raise ValueError(f"非法任务表名: {name!r}")
    return os.path.join(base, n)


def list_tasks_for(project_dir: str, folder: str) -> List[Dict[str, Any]]:
    """列出主项目共享 tasks_* 目录下的任务表。"""
    proj = os.path.abspath(CFG["project"])
    fd = (folder or "").strip()
    if not TASK_FOLDER_RE.match(fd or ""):
        raise ValueError(f"非法任务表目录名: {folder!r}")
    d = os.path.join(proj, fd)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(".json"):
            continue
        try:
            out.append(task_tools.task_summary(os.path.join(d, name)))
        except Exception as exc:  # noqa: BLE001
            out.append({"file": name, "path": os.path.join(d, name),
                        "error": f"{type(exc).__name__}: {exc}"[:140]})
    return out


def list_tasks() -> list:
    d = CFG["tasks"]
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(".json"):
            continue
        try:
            out.append(task_tools.task_summary(os.path.join(d, name)))
        except Exception as exc:  # noqa: BLE001
            out.append({"file": name, "path": os.path.join(d, name),
                        "error": f"{type(exc).__name__}: {exc}"[:140]})
    return out


def task_detail(name: str, with_validation: bool = True) -> dict:
    p = task_path(name)
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    out = {"summary": task_tools.task_summary(p), "curves": task_tools.curves(p)}
    if with_validation:
        out["validation"] = task_tools.validate_task(
            p, split_count=CFG["split_count"], strides_m=CFG["strides"])
    # 管理字段（供编辑面板）
    d = task_tools.load_task(p)
    data = d["data"]
    out["manageList"] = data.get("manageList") or []
    out["header"] = {k: data.get(k) for k in
                     ("recordMileage", "duration", "recodePace", "recodeCadence",
                      "raType", "raId", "studentId", "studentName", "schoolId")}
    return out


def history_filename(record: dict) -> str:
    rid = _safe_name(str(record.get("id") or "record"), FILE_RE)
    return f"history_{rid}.json"


def next_history_filename(root: str) -> str:
    """返回共享任务目录中下一个 tasklist_N.json 文件名。"""
    highest = -1
    if os.path.isdir(root):
        for name in os.listdir(root):
            match = re.fullmatch(r"tasklist_(\d+)\.json", name, re.IGNORECASE)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"tasklist_{highest + 1}.json"


def history_save_path(root: str, filename: str):
    filename = _safe_name(filename, TASK_RE)
    path = os.path.abspath(os.path.join(root, filename))
    base = os.path.abspath(root)
    if os.path.commonpath([path, base]) != base:
        raise ValueError("历史记录保存路径越界")
    return __import__("pathlib").Path(path)


def _history_client(student: str):
    cfg_path = _control_config_path(student)
    module = _load_main_module()
    module.set_args(cfg_path)
    return module.default_client()


def history_terms(student: str) -> list:
    response = _history_client(student).post_json("/run/listXnYearXqByStudentId", "")
    if response.get("code") != 200:
        raise ValueError(response.get("msg") or "获取学期失败")
    return response.get("data") or []


def history_runs(student: str, table_name: str) -> list:
    response = _history_client(student).post_json(
        "/run/crsReocordInfoList", json.dumps({"tableName": table_name}))
    if response.get("code") != 200:
        raise ValueError(response.get("msg") or "获取跑步记录失败")
    rows = []
    for month in (response.get("data") or {}).get("rank", []) or []:
        rows.extend(month.get("rankList") or [])
    return rows


def history_detail(student: str, table_name: str, record_id: str) -> dict:
    text = _history_client(student).post(
        "/run/crsReocordInfo", json.dumps({"id": str(record_id), "tableName": table_name}))
    result = json.loads(text)
    if result.get("code") != 200:
        raise ValueError(result.get("msg") or "获取历史详情失败")
    return result


def save_history_record(student: str, record: dict, filename: str = "") -> dict:
    data = record.get("data") or {}
    if not isinstance(data.get("pointsList"), list) or not data.get("pointsList"):
        raise ValueError("历史记录缺少 pointsList，不能保存为任务 JSON")
    filename = next_history_filename(CFG["tasks"])
    path = history_save_path(CFG["tasks"], filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"文件已存在：{path.name}")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "filename": path.name,
            "points": len(data["pointsList"]), "mileage": data.get("recordMileage")}


# ---------------------------------------------------------------- 当前项目路线 / 配置桥接
def route_meta() -> dict:
    """返回当前项目的校区任务表与可供形变的底图清单。"""
    return {"campuses": route_web.list_campuses(),
            "sources": route_web.list_sources()}


def route_preview(query: dict) -> dict:
    """调用 yunForNewVersion 原有路线引擎，网页层不重复实现算法。"""
    return route_web.build_preview(query)


def _term_dates(term: dict):
    match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*-\s*"
                      r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
                      str(term.get("sjd") or ""))
    if not match:
        return None
    values = [int(x) for x in match.groups()]
    return date(values[0], values[1], values[2]), date(values[3], values[4], values[5])


def select_current_term(terms: list, today: date | None = None) -> dict:
    """选择当前学期；不在日期范围内时回退服务端返回的第一项。"""
    if not terms:
        raise ValueError("服务端没有返回学期")
    today = today or date.today()
    for term in terms:
        bounds = _term_dates(term)
        if bounds and bounds[0] <= today <= bounds[1]:
            return term
    return terms[0]


def _run_is_morning(run: dict) -> bool:
    flag = str(run.get("isMorning") or "").strip().upper()
    if flag in {"Y", "YES", "1", "TRUE"}:
        return True
    if flag in {"N", "NO", "0", "FALSE"}:
        return False
    raw = str(run.get("recordEndTime") or run.get("recordStartTime") or "")
    match = re.search(r"\s(\d{1,2}):\d{2}", raw)
    return bool(match and int(match.group(1)) < 12)


def summarize_semester_runs(term: dict, runs_response: dict, today: date | None = None) -> dict:
    """把 crsReocordInfoList 回包汇总为网页统计口径。"""
    rows = []
    for month in (runs_response.get("data") or {}).get("rank", []) or []:
        rows.extend(month.get("rankList") or [])
    qualified_values = {"1", "Y", "YES", "TRUE", "QUALIFIED"}
    qualified = sum(1 for run in rows
                    if str(run.get("isQualified") or "").strip().upper() in qualified_values)
    return {
        "semester": term.get("key") or term.get("sjd") or "当前学期",
        "total": len(rows),
        "morning": sum(1 for run in rows if _run_is_morning(run)),
        "evening": sum(1 for run in rows if not _run_is_morning(run)),
        "qualified": qualified,
        "unqualified": len(rows) - qualified,
    }


def fetch_student_statistics(student: str) -> dict:
    cfg_path = _control_config_path(student)
    # 使用 history.py/main.py 长期存在的入口，兼容服务器上旧版 yun_http.py。
    # core 为人脸预检提供了同名 yun_http stub，导入 main 前必须恢复主项目模块。
    main_module = _load_main_module()
    main_module.set_args(cfg_path)
    client = main_module.default_client()
    terms_response = client.post_json("/run/listXnYearXqByStudentId", "")
    if terms_response.get("code") != 200:
        raise ValueError(terms_response.get("msg") or "获取学期失败")
    term = select_current_term(terms_response.get("data") or [])
    runs_response = client.post_json(
        "/run/crsReocordInfoList", json.dumps({"tableName": term.get("value") or ""}))
    if runs_response.get("code") != 200:
        raise ValueError(runs_response.get("msg") or "获取跑步记录失败")
    result = summarize_semester_runs(term, runs_response)
    result["student"] = student
    result["table_name"] = term.get("value") or ""
    return result


def _control_config_path(student: str = "") -> str:
    """当前配置页使用的配置文件：学号配置优先，空学号回退主配置。"""
    if student:
        student = _safe_name(student)
        path = os.path.join(CFG["config_root"], student, "config.ini")
    else:
        path = os.path.join(CFG["project"], "config.ini")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return path


def control_config(student: str = "") -> dict:
    cp = configparser.ConfigParser()
    path = _control_config_path(student)
    cp.read(path, encoding="utf-8")
    return {sec: {key: (route_web.MASK if key.lower() in route_web.SENSITIVE_KEYS and value
                        else value) for key, value in cp[sec].items()}
            for sec in cp.sections()}


def save_control_config(payload: dict, student: str = "") -> None:
    path = _control_config_path(student)
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    for sec, kv in (payload or {}).items():
        if not sec or sec.lower() == "default":
            continue
        if not cp.has_section(sec):
            cp.add_section(sec)
        for key, value in (kv or {}).items():
            if value in (route_web.KEEP, route_web.MASK):
                continue
            cp[sec][str(key).strip()] = "" if value is None else str(value)
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Yun2FaceWeb/1.0"

    def log_message(self, fmt, *args):   # 静音默认访问日志，只留错误
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("  [http] " + (fmt % args) + "\n")

    # ---- 响应助手 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype="image/jpeg", code=200, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---- 路由 ----
    def do_GET(self):   # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        try:
            if path.startswith("/api/"):
                return self._api_get(path, q)
            if path.startswith("/file/") or path.startswith("/thumb/") or path.startswith("/preview/"):
                return self._media(path, q)
            return self._static(path)
        except ValueError as exc:
            self._err(exc, 400)
        except FileNotFoundError as exc:
            self._err(f"文件不存在: {exc}", 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._err(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._body_json()
        except Exception as exc:  # noqa: BLE001
            return self._err(f"请求体不是合法 JSON: {exc}", 400)
        try:
            return self._api_post(u.path, body)
        except ValueError as exc:
            self._err(exc, 400)
        except FileNotFoundError as exc:
            self._err(f"文件不存在: {exc}", 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._err(f"{type(exc).__name__}: {exc}", 500)

    # ---- API: GET ----
    def _api_get(self, path, q):
        if path == "/api/history/terms":
            student = (q.get("student") or [""])[0]
            try:
                return self._json({"ok": True, "student": student, "terms": history_terms(student)})
            except Exception as exc:
                return self._err(f"获取学期失败：{type(exc).__name__}: {exc}", 502)
        if path == "/api/history/runs":
            student = (q.get("student") or [""])[0]
            table = (q.get("table") or [""])[0]
            try:
                return self._json({"ok": True, "student": student, "table": table,
                                   "runs": history_runs(student, table)})
            except Exception as exc:
                return self._err(f"获取历史记录失败：{type(exc).__name__}: {exc}", 502)
        if path == "/api/history/detail":
            student = (q.get("student") or [""])[0]
            table = (q.get("table") or [""])[0]
            record_id = (q.get("id") or [""])[0]
            try:
                return self._json({"ok": True, **history_detail(student, table, record_id)})
            except Exception as exc:
                return self._err(f"获取历史详情失败：{type(exc).__name__}: {exc}", 502)
        if path == "/api/statistics":
            student = (q.get("student") or [""])[0]
            if not student:
                return self._err("请先选择学号", 400)
            try:
                return self._json({"ok": True, **fetch_student_statistics(student)})
            except Exception as exc:  # noqa: BLE001
                return self._err(f"统计获取失败：{type(exc).__name__}: {exc}", 502)
        if path == "/api/control-config":
            student = (q.get("student") or [""])[0]
            return self._json({"ok": True, "student": student,
                               "path": _control_config_path(student),
                               "config": control_config(student)})
        if path == "/api/route-meta":
            return self._json({"ok": True, **route_meta()})
        if path == "/api/routes":
            try:
                out = route_preview(q)
            except Exception as exc:  # noqa: BLE001
                out = {"error": f"{type(exc).__name__}: {exc}"}
            if out.get("error"):
                route_web._set_plan({})
            else:
                route_web._set_plan(out.get("command") or {})
            return self._json(out)
        if path == "/api/config":
            return self._json({"ok": True, "photos": CFG["photos"], "annos": CFG["annos"],
                               "tasks": CFG["tasks"], "project_tasks": CFG.get("project_tasks"),
                               "split_count": CFG["split_count"], "strides": CFG["strides"],
                               "detect_available": face_check.detect_available(),
                               "thresholds": core.THRESHOLDS,
                               "max_bytes": core.MAX_BYTES, "max_width": core.MAX_WIDTH})
        if path == "/api/students":
            return self._json({"ok": True, "students": list_students()})
        if path == "/api/tasks":
            # 兼容两种用法：
            #   不带 project → 旧的全局任务表库（--tasks）
            #   带 project [+folder] → 某个学号项目自己的 tasks_* 目录
            proj = (q.get("project") or [""])[0]
            folder = (q.get("folder") or [""])[0]
            if proj:
                return self._json({"ok": True, "project": proj, "folder": folder,
                                   "tasks": list_tasks_for(proj, folder),
                                   "split_count": CFG["split_count"],
                                   "strides": CFG["strides"]})
            return self._json({"ok": True, "dir": CFG["tasks"], "tasks": list_tasks(),
                               "split_count": CFG["split_count"], "strides": CFG["strides"]})
        if path == "/api/projects":
            return self._json({"ok": True, "root": CFG["projects_root"],
                               "projects": discover_projects()})
        if path == "/api/runs":
            return self._json({"ok": True, "runs": list_runs()})
        if path == "/api/schedules":
            return self._json({"ok": True, "schedules": list_schedules(),
                               "time_zone": schedule_timezone_info()})
        if path == "/api/run":
            rid = (q.get("id") or [""])[0]
            since = int((q.get("since") or ["0"])[0])
            return self._json({"ok": True, **run_view(rid, since)})
        if path == "/api/configs":
            return self._json({"ok": True, "configs": list_configs(),
                               "project": CFG["project"],
                               "python": _python_exe(),
                               "dry_home": CFG["dry_home"],
                               "dry_home_exists": os.path.isfile(CFG["dry_home"])})
        if path == "/api/task":
            name = (q.get("name") or [""])[0]
            return self._json({"ok": True, **task_detail(name)})
        if path == "/api/images":
            stu = (q.get("student") or [""])[0]
            return self._json({"ok": True, "student": stu, "images": _list_images(stu)})
        if path == "/api/annotation":
            stu, stem = (q.get("student") or [""])[0], (q.get("stem") or [""])[0]
            ap = anno_path(stu, stem)
            if not os.path.isfile(ap):
                return self._json({"ok": True, "exists": False})
            with open(ap, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            p = None
            for ext in PHOTO_EXT:
                cand = os.path.join(CFG["photos"], _safe_name(stu), stem + ext)
                if os.path.isfile(cand):
                    p = cand
                    break
            size = None
            if p:
                with Image.open(p) as im:
                    size = list(im.size)
            return self._json({"ok": True, "exists": True, "annotation": d,
                               "path": ap, "photo_size": size})
        return self._err(f"未知接口: {path}", 404)

    # ---- API: POST ----
    def _api_post(self, path, body):
        act = body.get("action")
        stu = body.get("student", "")
        name = body.get("name", "")
        stem = body.get("stem") or (os.path.splitext(name)[0] if name else "")

        if path == "/api/control-config":
            try:
                student = str(body.get("student") or "")
                save_control_config(body.get("config") or {}, student)
                return self._json({"ok": True, "student": student,
                                   "path": _control_config_path(student)})
            except Exception as exc:  # noqa: BLE001
                return self._err(str(exc))

        if path == "/api/history/save":
            try:
                result = save_history_record(str(body.get("student") or ""),
                                             body.get("record") or {},
                                             str(body.get("filename") or ""))
                return self._json({"ok": True, **result})
            except Exception as exc:
                return self._err(str(exc))

        if path == "/api/runs":
            if act == "start":
                try:
                    return self._json({"ok": True, **start_run(body)})
                except ValueError as exc:
                    return self._err(str(exc))
            if act == "stop":
                return self._json({"ok": True, **stop_run(body.get("id", ""),
                                                          force=bool(body.get("force")))})
            if act == "clear":
                with _LOCK:
                    for rid in [k for k, v in RUNS.items() if v["status"] != "running"]:
                        RUNS.pop(rid, None)
                    os.makedirs(RUN_DATA_DIR, exist_ok=True)
                    with open(RUNS_FILE, "w", encoding="utf-8") as fh:
                        json.dump({}, fh)
                return self._json({"ok": True, "runs": list_runs()})
            if act == "login":
                return self._json(do_login(body.get("config", ""),
                                           save_token=bool(body.get("save_token"))))
            return self._err("未知 runs 动作")

        if path == "/api/schedules":
            if act == "create":
                try:
                    item = create_schedule(body)
                except ValueError as exc:
                    return self._err(str(exc))
                return self._json({"ok": True, "schedule": item})
            if act == "update":
                try:
                    item = update_schedule(body)
                except KeyError:
                    return self._err("定时任务不存在", 404)
                except ValueError as exc:
                    return self._err(str(exc))
                return self._json({"ok": True, "schedule": item})
            if act == "toggle":
                sid = str(body.get("id") or "")
                if sid not in SCHEDULES:
                    return self._err("定时任务不存在", 404)
                item = set_schedule_enabled(sid, bool(body.get("enabled")))
                return self._json({"ok": True, "schedule": item})
            if act == "delete":
                sid = str(body.get("id") or "")
                SCHEDULES.pop(sid, None)
                _persist_schedules()
                return self._json({"ok": True})
            if act == "run_now":
                sid = str(body.get("id") or "")
                item = SCHEDULES.get(sid)
                if not item:
                    return self._err("定时任务不存在", 404)
                result = start_run(dict(item.get("options") or {}))
                item["last_run"] = result.get("id")
                item["last_started"] = time.time()
                _persist_schedules()
                return self._json({"ok": True, **result})
            return self._err("未知 schedules 动作")

        if path == "/api/students":
            if act == "create":
                d = _safe_name(body.get("student", ""))
                account_dir = os.path.join(CFG["config_root"], d)
                config_path = os.path.join(account_dir, "config.ini")
                if os.path.isfile(config_path):
                    return self._err(f"学号已存在: {d}")
                template = os.path.join(CFG["project"], "config.ini")
                if not os.path.isfile(template):
                    return self._err(f"主项目缺少 config.ini 模板: {template}")
                import shutil
                os.makedirs(account_dir, exist_ok=True)
                shutil.copy2(template, config_path)
                for root in (CFG["photos"], CFG["annos"]):
                    os.makedirs(os.path.join(root, d), exist_ok=True)
                return self._json({"ok": True, "student": d, "config": config_path})
            if act == "rename":
                old = _safe_name(body.get("student", ""))
                new = _safe_name(body.get("new_student", ""))
                for root in (CFG["config_root"], CFG["photos"], CFG["annos"]):
                    o, n = os.path.join(root, old), os.path.join(root, new)
                    if os.path.isdir(o):
                        if os.path.exists(n):
                            return self._err(f"目标已存在: {n}")
                        os.rename(o, n)
                return self._json({"ok": True, "student": new})
            if act == "delete":
                d = _safe_name(body.get("student", ""))
                removed = []
                for root in (CFG["config_root"], CFG["photos"], CFG["annos"]):
                    t = os.path.join(root, d)
                    if os.path.isdir(t):
                        import shutil
                        shutil.rmtree(t)
                        removed.append(t)
                return self._json({"ok": True, "removed": removed})
            return self._err("未知 students 动作")

        if path == "/api/upload":
            # 形如 data:image/jpeg;base64,xxxx 或纯 base64
            import base64
            data_url = body.get("data", "")
            if "," in data_url and data_url.strip().startswith("data:"):
                data_url = data_url.split(",", 1)[1]
            raw = base64.b64decode(data_url)
            d = _safe_name(stu)
            fname = _safe_name(name, FILE_RE)
            if not fname.lower().endswith(PHOTO_EXT):
                return self._err(f"只接受图片: {PHOTO_EXT}")
            pdir = os.path.join(CFG["photos"], d)
            os.makedirs(pdir, exist_ok=True)
            dst = os.path.join(pdir, fname)
            if os.path.exists(dst) and not body.get("overwrite"):
                return self._err(f"已存在: {fname}（勾选覆盖或改名）")
            with open(dst, "wb") as fh:
                fh.write(raw)
            return self._json({"ok": True, "path": dst, "bytes": len(raw)})

        if path == "/api/annotation":
            ap = anno_path(stu, stem)
            if act == "save":
                ann = body.get("annotation")
                if not isinstance(ann, dict):
                    return self._err("annotation 必须是对象")
                # 内容绑定：缺 source_sha256 或与照片不符时拒绝保存（不允许无主标注）
                p = photo_path(stu, name or (stem + ".jpg"))
                if os.path.isfile(p):
                    sha = _sha256_file(p)
                    if not ann.get("source_sha256"):
                        ann["source_sha256"] = sha
                    elif ann["source_sha256"] != sha:
                        return self._err("source_sha256 与当前照片不符，拒绝保存"
                                         "（请先重新检测生成标注）")
                ann.setdefault("space", "image")
                ann.setdefault("bind_apply", {"after_exif": True, "mirrored": False})
                os.makedirs(os.path.dirname(ap), exist_ok=True)
                with open(ap, "w", encoding="utf-8") as fh:
                    json.dump(ann, fh, ensure_ascii=False, indent=2)
                return self._json({"ok": True, "path": ap})
            if act == "delete":
                if os.path.isfile(ap):
                    os.remove(ap)
                    return self._json({"ok": True, "removed": ap})
                return self._json({"ok": True, "removed": None})
            if act == "drop_source_path":
                if not os.path.isfile(ap):
                    return self._err("标注不存在")
                with open(ap, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                removed = d.pop("source_path", None)
                with open(ap, "w", encoding="utf-8") as fh:
                    json.dump(d, fh, ensure_ascii=False, indent=2)
                return self._json({"ok": True, "removed": removed})
            return self._err("未知 annotation 动作")

        if path == "/api/photo":
            if act == "delete":
                p = photo_path(stu, name)
                if os.path.isfile(p):
                    os.remove(p)
                ap = anno_path(stu, stem)
                gone = os.path.isfile(ap)
                if gone:
                    os.remove(ap)
                return self._json({"ok": True, "photo_removed": p,
                                   "annotation_removed": gone})
            if act == "rename":
                new_name = _safe_name(body.get("new_name", ""), FILE_RE)
                if not new_name.lower().endswith(PHOTO_EXT):
                    return self._err(f"新文件名必须是图片: {PHOTO_EXT}")
                p = photo_path(stu, name)
                dst = os.path.join(CFG["photos"], _safe_name(stu), new_name)
                if os.path.exists(dst):
                    return self._err(f"目标已存在: {new_name}")
                os.rename(p, dst)
                # 标注跟着改名（内容绑定是哈希，不受影响）
                old_ap = anno_path(stu, stem)
                new_stem = os.path.splitext(new_name)[0]
                if os.path.isfile(old_ap):
                    os.rename(old_ap, anno_path(stu, new_stem))
                return self._json({"ok": True, "name": new_name})
            return self._err("未知 photo 动作")

        if path == "/api/task":
            name = body.get("name", "")
            p = task_path(name)
            if act == "validate":
                return self._json({"ok": True, **task_tools.validate_task(
                    p, split_count=CFG["split_count"], strides_m=CFG["strides"])})
            if act == "copy":
                new_name = body.get("new_name") or TASK_RE.sub("", name)
                if not str(new_name).lower().endswith(".json"):
                    new_name = str(new_name) + ".json"
                dst = task_path(str(new_name))
                if os.path.exists(dst):
                    return self._err(f"已存在: {new_name}")
                import shutil as _sh
                _sh.copy2(p, dst)
                return self._json({"ok": True, "path": dst,
                                   "summary": task_tools.task_summary(dst)})
            if act == "rename":
                new_name = body.get("new_name", "")
                dst = task_path(new_name)
                if os.path.exists(dst):
                    return self._err(f"已存在: {new_name}")
                os.rename(p, dst)
                return self._json({"ok": True, "path": dst})
            if act == "delete":
                os.remove(p)
                return self._json({"ok": True, "removed": p})
            if act == "generate":
                # 由源表派生新表：配速缩放 + 坐标漂移 + 里程重算
                new_name = body.get("new_name") or ""
                if not new_name:
                    return self._err("需要 new_name（新任务表文件名）")
                if not str(new_name).lower().endswith(".json"):
                    new_name = str(new_name) + ".json"
                dst = task_path(str(new_name))
                if os.path.exists(dst) and not body.get("overwrite"):
                    return self._err(f"已存在: {new_name}（勾选覆盖或改名）")
                try:
                    r = task_tools.generate_variant(
                        p, dst, seed=body.get("seed"),
                        target_pace=body.get("target_pace"),
                        drift_deg=body.get("drift_deg"),
                        rebuild_mileage=bool(body.get("rebuild_mileage", True)))
                except ValueError as exc:
                    return self._err(str(exc))
                r["validation"] = task_tools.validate_task(
                    dst, split_count=CFG["split_count"], strides_m=CFG["strides"])
                return self._json(r)
            if act == "upload":
                data_url = body.get("data", "")
                if "," in data_url and data_url.strip().startswith("data:"):
                    data_url = data_url.split(",", 1)[1]
                import base64
                raw = base64.b64decode(data_url)
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    if not isinstance(obj.get("data", {}).get("pointsList"), list):
                        return self._err("不是任务表：缺少 data.pointsList")
                except Exception as exc:  # noqa: BLE001
                    return self._err(f"不是合法 JSON: {exc}")
                fname = _safe_name(body.get("name", ""), TASK_RE)
                dst = task_path(fname)
                if os.path.exists(dst) and not body.get("overwrite"):
                    return self._err(f"已存在: {fname}")
                with open(dst, "wb") as fh:
                    fh.write(raw)
                return self._json({"ok": True, "path": dst,
                                   "summary": task_tools.task_summary(dst)})
            if act == "to_project":
                # 复制到项目打表目录，供 main.py -t 直接使用
                dst_dir = CFG.get("project_tasks") or ""
                if not dst_dir:
                    return self._err("未配置项目任务目录（启动时用 --project-tasks 指定）")
                os.makedirs(dst_dir, exist_ok=True)
                target = os.path.join(dst_dir, os.path.basename(p))
                if os.path.exists(target) and not body.get("overwrite"):
                    return self._err(f"项目目录已存在同名文件: {target}")
                import shutil as _sh
                _sh.copy2(p, target)
                return self._json({"ok": True, "path": target})
            return self._err("未知 task 动作")

        if path == "/api/check":
            fix = bool(body.get("fix_paths"))
            return self._json(do_check(stu, name, fix_paths=fix))

        if path == "/api/autodetect":
            return self._json(do_autodetect(stu, name, save=body.get("save", True)))

        if path == "/api/batch":
            kind = body.get("kind")            # "autodetect" | "check"
            images = body.get("images") or []
            results = []
            for it in images:
                nm = it.get("name") if isinstance(it, dict) else str(it)
                try:
                    if kind == "autodetect":
                        r = do_autodetect(stu, nm, save=True)
                        results.append({"name": nm, "ok": r.get("ok", False),
                                        "errors": r.get("errors", [])})
                    else:
                        r = do_check(stu, nm, fix_paths=bool(body.get("fix_paths")))
                        results.append({"name": nm, "ok": r.get("ok", False),
                                        "reason": (r.get("errors") or [""])[0],
                                        "upload_bytes": r.get("upload_bytes"),
                                        "metrics": r.get("metrics")})
                except Exception as exc:  # noqa: BLE001
                    results.append({"name": nm, "ok": False,
                                    "errors": [f"{type(exc).__name__}: {exc}"]})
            return self._json({"ok": True, "kind": kind, "results": results})

        return self._err(f"未知接口: {path}", 404)

    # ---- 媒体 ----
    def _media(self, path, q):
        # 形如 /thumb/<学号>/<文件名> —— 正好三段，且每段都必须过白名单校验，
        # 否则会出现目录穿越（../）。校验统一交给 photo_path/_safe_name。
        parts = [p for p in path.strip("/").split("/") if p != ""]
        if len(parts) != 3:
            return self._err(f"路径必须是 /<类型>/<学号>/<文件名>，实得 {len(parts)} 段", 400)
        kind, stu, name = parts
        if kind == "thumb":
            return self._bytes(_thumb(stu, name), cache=True)
        if kind == "preview":
            return self._bytes(do_preview(stu, name))
        if kind != "file":
            return self._err(f"未知类型: {kind}", 404)
        p = photo_path(stu, name)
        with open(p, "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(p)[0] or "image/jpeg"
        return self._bytes(data, ctype, cache=True)

    # ---- 静态文件 ----
    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith(".."):
            return self._err("非法路径", 403)
        f = os.path.join(WEB_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(f):
            return self._err(f"未找到: {path}", 404)
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(f, "rb") as fh:
            self._bytes(fh.read(), ctype)


def _pick_port(start: int, tries: int = 20) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit(f"{start}~{start + tries} 全部被占用，请用 --port 指定其它端口")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Yun2Face 网页控制台（本机工具，只监听 127.0.0.1）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址；云服务器可用 0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config-root", default=CFG["config_root"], help="学号配置目录，默认项目根/students_cofig")
    ap.add_argument("--photos", default=CFG["photos"], help="照片根目录")
    ap.add_argument("--annos", default=CFG["annos"], help="标注根目录")
    ap.add_argument("--tasks", default=CFG["tasks"], help="任务表库目录（默认项目的打表目录）")
    ap.add_argument("--project-tasks", default=os.path.join(HERE, "tasks_fch"),
                    help="「送入项目」的目标目录（main.py -t 用的那个）")
    ap.add_argument("--project", default=CFG["project"],
                    help="默认项目根（yunForNewVersion 的一份检出）")
    ap.add_argument("--projects-root", default=CFG["projects_root"],
                    help="项目扫描目录：约定 <root>/yunForNewVersion-<学号>/ 为各账号的独立项目，"
                         "也兼容 <root>/<学校>/yunForNewVersion-<学号>/")
    ap.add_argument("--python", default="", help="跑 main.py 用的 python（默认当前解释器）")
    ap.add_argument("--dry-home", dest="dry_home", default=CFG["dry_home"],
                    help="离线演练用的 getHomeRunInfo fixture")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    CFG["photos"] = os.path.abspath(args.photos)
    CFG["annos"] = os.path.abspath(args.annos)
    CFG["tasks"] = os.path.abspath(args.tasks)
    CFG["project_tasks"] = os.path.abspath(args.project_tasks)
    CFG["project"] = os.path.abspath(args.project)
    CFG["projects_root"] = os.path.abspath(args.projects_root)
    CFG["python"] = args.python
    CFG["dry_home"] = os.path.abspath(args.dry_home)
    CFG["config_root"] = os.path.abspath(args.config_root)
    for k in ("config_root", "photos", "annos", "tasks"):
        if not os.path.isdir(CFG[k]):
            os.makedirs(CFG[k], exist_ok=True)
    _load_persisted_runs()
    # 上次异常退出可能留下单表临时目录；启动时按已落盘的运行记录清理。
    for _run in list(RUNS.values()):
        _cleanup_task_pick(_run.get("pick_dir"))
    _load_schedules()
    threading.Thread(target=_schedule_loop, daemon=True).start()

    port = _pick_port(args.port)
    url = f"http://{args.host}:{port}/"
    ok, why = face_check.detect_available()
    print("=" * 74)
    print("  Yun2Face 网页控制台")
    print(f"  地址    : http://{args.host}:{port}/")
    print(f"  照片根  : {CFG['photos']}")
    print(f"  标注根  : {CFG['annos']}")
    print(f"  配置根  : {CFG['config_root']}")
    print(f"  任务表库: {CFG['tasks']}")
    print(f"  送入项目: {CFG['project_tasks']}")
    print(f"  默认项目: {CFG['project']}")
    tz = schedule_timezone_info()
    print(f"  定时时区: {tz['label']}（定时任务 HH:MM 按此时区解释）；"
          f"服务器本地: {tz['server_label']}"
          + ("" if tz["matches_server"] else "  ⚠ 与定时时区不同，已按定时时区计算"))
    projs = discover_projects()
    print(f"  项目扫描: {CFG['projects_root']}  →  发现 {len(projs)} 个账号项目")
    for p in projs:
        mark = "✔token" if p["token"] else "⚠无token"
        tds = ",".join(f"{d['name']}({d['files']})" for d in p["task_dirs"]) or "无任务表目录"
        print(f"      {p['student'] or p['name']:<12} {mark:<9} {p['school'] or '':<14} {tds}")
    print(f"  运行用  : {_python_exe()}")
    if not os.path.isfile(os.path.join(CFG["project"], "main.py")):
        print("  ⚠ 项目根下没有 main.py：运行控制台会不可用（用 --project 指定正确路径）")
    print(f"  自动检测: {'可用' if ok else '不可用 — ' + why}")
    print("  停止    : 在本窗口按 Ctrl+C")
    print("=" * 74)
    srv = ThreadingHTTPServer((args.host, port), Handler)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
