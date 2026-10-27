# -*- coding: utf-8 -*-
"""Yun2Face —— 云运动人脸输入「上传前预检」独立工具。

它做的事与 yunForNewVersion 项目里 `main.build_face_runner` 的照片预检**完全同一套规则**：
    解码 → EXIF 摆正 → 检测标注匹配（内容哈希绑定）→ 取景质量门 → APK 压缩链 → 报告
并把项目预检里"通过/不通过"背后的每个数字都摊开给你看（占宽、俯仰、偏航、翻滚、余量）。

用法
----
    python face_check.py rules                          # 打印全部阈值
    python face_check.py check  图.jpg [标注.json]       # 单张照片（无标注则自动检测）
    python face_check.py gen    文件夹 [--out out]      # 批量照片：检查 + 产出标注 JSON
    python face_check.py video  视频.mp4 [标注.json]     # 视频源（--face-video 分支）
    python face_check.py show   图.jpg [标注.json]       # 画框+五点+门限线，存图目视核对

退出码：0=全部通过；1=存在不通过；2=输入/环境错误。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 检测模型所在依赖目录（可选）。缺失时基础检查仍可用。
_LIB = os.path.join(HERE, "lib")
# lib/ may contain Windows bundled wheels; only use it on Windows.
if os.name == "nt" and os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.append(_LIB)

import core  # noqa: E402
from PIL import Image  # noqa: E402

MODEL_PATH = os.path.join(HERE, "models", "yunet.onnx")

# 检测用分档宽度：YuNet 固定 640 输入，直接把 4000px 图喂进去会退化
# （实测同图 1024 宽检出 1 张 0.78，3888 宽变 6 张错位框）。统一分档后按比例还原。
DETECT_SCALES = (1024, 1600, 2400)
DETECT_MIN_SCORE = 0.6

# ---------------------------------------------------------------- 环境探测
def detect_available() -> tuple[bool, str]:
    """返回 (是否可用, 说明)。"""
    if not os.path.isfile(MODEL_PATH):
        return False, f"缺少检测模型 {MODEL_PATH}"
    try:
        import cv2  # noqa: F401
    except Exception as exc:
        return False, f"opencv 不可用：{type(exc).__name__}: {exc}"
    try:
        import numpy  # noqa: F401
    except Exception as exc:
        return False, f"numpy 不可用：{type(exc).__name__}: {exc}"
    return True, "可用"


# ---------------------------------------------------------------- 检测
def _detector():
    import cv2
    return cv2.FaceDetectorYN.create(MODEL_PATH, "", (320, 320), DETECT_MIN_SCORE, 0.3, 5000)


def detect_face(path: str):
    """产出一份可用的检测标注（box+五点+分数）。

    返回 dict 或 None。点为项目约定顺序：0=左眼 1=右眼 2=鼻尖 3=左嘴角 4=右嘴角。
    """
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise core.FaceInputError(f"图像解码失败（不是图片或已损坏）: {path}")
    return _detect_bgr(img)


def detect_frame_array(frame):
    """对一帧（PIL.Image 或 HxWx3 RGB/BGR ndarray）做检测。视频逐帧预检用。

    与 detect_face 共用同一套分档与还原逻辑，避免两条检测路径分叉。
    """
    import numpy as np
    from PIL import Image

    if isinstance(frame, Image.Image):
        arr = np.asarray(frame.convert("RGB"))[:, :, ::-1]   # RGB -> BGR
    else:
        arr = np.asarray(frame)
        if arr.ndim != 3 or arr.shape[2] != 3:
            raise core.FaceInputError(f"帧格式不支持: shape={getattr(arr, 'shape', None)}")
    return _detect_bgr(arr)


def _detect_bgr(img):
    """分档检测 + 坐标还原到原图。

    实测规律（同一张图，原始检测分数/框稳定性）：
      - 大图（>1024 宽）**必须缩到 1024**：3888 宽直喂 YuNet 会退化
        （原图 0.62/框错位 → 缩 1024 后 0.79/框正确）。
      - 小图（≤1024 宽）**必须用原生宽度**：720 宽的帧缩到 640 会检不出；
        也不宜放大（720→1024/1600 会让框漂移：占宽 0.55→0.43→0.34）。
    因此按尺寸分流，而不是一套档位走到底。
    """
    import cv2

    H, W = img.shape[:2]
    if W <= DETECT_SCALES[0]:
        candidates = [W]                                   # 小图：原生宽度，不缩放
    else:
        candidates = [s for s in DETECT_SCALES if s < W]    # 大图：只往下缩
    for target_w in candidates:
        s = target_w / float(W)
        if s == 1.0:
            work = img
        else:
            work = cv2.resize(img, (target_w, int(round(H * s))),
                              interpolation=cv2.INTER_AREA)
        wh, ww = work.shape[:2]
        det = _detector()
        det.setInputSize((ww, wh))
        ok, faces = det.detect(work)
        if not ok or faces is None or len(faces) == 0:
            continue
        f = sorted(faces, key=lambda x: -float(x[-1]))[0]
        if float(f[14]) < DETECT_MIN_SCORE:
            continue
        inv = 1.0 / s if s else 1.0
        x1, y1 = float(f[0]) * inv, float(f[1]) * inv
        x2, y2 = (float(f[0]) + float(f[2])) * inv, (float(f[1]) + float(f[3])) * inv
        lm = [(float(f[4 + 2 * i]) * inv, float(f[5 + 2 * i]) * inv) for i in range(5)]
        eyes = sorted(lm[0:2], key=lambda p: p[0])
        mouth = sorted(lm[3:5], key=lambda p: p[0])
        pts = [eyes[0], eyes[1], lm[2], mouth[0], mouth[1]]
        return {"box": [x1, y1, x2, y2], "points": [list(p) for p in pts],
                "score": float(f[14]), "space": "image",
                "detect_scale": target_w, "faces_found": int(len(faces))}
    return None


def detection_to_json(photo: str, det: dict) -> dict:
    """把检测结果包装成项目要求的标注格式（含内容哈希绑定与坐标声明）。"""
    return {
        "box": [round(v, 2) for v in det["box"]],
        "points": [[round(x, 2), round(y, 2)] for x, y in det["points"]],
        "score": round(det["score"], 4),
        "space": "image",
        "source_sha256": core.sha256_file(photo),
        "bind_apply": {"after_exif": True, "mirrored": False},
    }


def _annotate_binding_error(exc, bundle, raw: bytes, photo: str, ann_path: str):
    """把项目的绑定报错翻译成"问题在哪、怎么修"。

    最常见的坑：照片被移动/改名，标注里的 `source_path` 记的是旧路径——
    内容哈希其实仍然匹配，但项目对"不一致的 source_path"是硬拒绝。
    """
    import hashlib
    declared = getattr(bundle, "source_path", None)
    want = getattr(bundle, "source_sha256", None)
    got = hashlib.sha256(raw).hexdigest()
    if declared and want and want == got and os.path.abspath(declared) != os.path.abspath(photo):
        return core.FaceInputError(
            f"{exc}\n"
            f"      → 诊断：标注里的路径记录【过期】，但内容哈希仍然匹配"
            f"（{got[:12]}… = {want[:12]}…），说明就是同一张图、只是被移动/改名了。\n"
            f"        记录路径：{declared}\n"
            f"        当前照片：{os.path.abspath(photo)}\n"
            f"      → 修法（三选一）：\n"
            f"        1) 本工具加 --fix-paths：自动删掉过期的 source_path 字段"
            f"（内容身份仍由 sha256 保证）\n"
            f"        2) 手工删掉 {ann_path} 里的 \"source_path\" 一行\n"
            f"        3) 用 gen 重新生成标注（放在照片旁边就不会再漂移）"
        )
    if want is None:
        return core.FaceInputError(
            f"{exc}\n      → 诊断：标注缺少 source_sha256（内容哈希）。"
            "项目要求标注必须绑定到具体文件内容，请重新生成标注"
        )
    if want != got:
        return core.FaceInputError(
            f"{exc}\n      → 诊断：标注绑定的内容与当前照片【不是同一张】"
            f"（标注 {want[:12]}… ≠ 照片 {got[:12]}…）。\n"
            f"        这份标注属于别的图，必须针对当前照片重新检测/标注。"
        )
    return exc


def _try_fix_stale_source_path(ann_path: str, bundle, raw: bytes, photo: str):
    """内容哈希匹配、仅路径记录不一致时，删掉 source_path 字段并写回。返回说明或 None。

    只在这一个条件下动手：**sha256 必须相等**。哈希不等说明是错源标注，绝不修改。
    """
    import hashlib
    declared = getattr(bundle, "source_path", None)
    want = getattr(bundle, "source_sha256", None)
    got = hashlib.sha256(raw).hexdigest()
    if not (declared and want == got) or os.path.abspath(declared) == os.path.abspath(photo):
        return None
    try:
        with open(ann_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    if "source_path" not in data:
        return None
    data.pop("source_path")
    try:
        with open(ann_path + ".bak_pathfix", "w", encoding="utf-8") as fh:
            json.dump(json.load(open(ann_path, encoding="utf-8")), fh,
                      ensure_ascii=False, indent=2)
        with open(ann_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        return None
    return (f"删除过期 source_path={declared!r}（内容哈希一致，备份 {ann_path}.bak_pathfix）")


# ---------------------------------------------------------------- 单张检查
def check_photo(photo: str, detection_json: str | None = None,
                auto_detect: bool = True, want_upload: bool = False,
                fix_paths: bool = False) -> dict:
    """完整预检，返回结构化结果（不抛异常，失败也带原因）。

    与项目 `build_face_runner` 的照片分支同序同规则：
      读文件 → 标注绑定校验（sha256）→ bind_apply 校验 → EXIF 摆正
      → 检测匹配 → 取景质量门 → 压缩链（q100 → 宽>720 缩 720/q90 → 超 150KB 走阶梯）

    fix_paths：标注里的 `source_path` 与实际照片路径不一致时，**若内容哈希仍匹配**，
    自动删掉该字段并重试。路径字符串一移动就过期，而内容身份由 sha256 保证——
    与项目"以内容哈希为准"的立场一致，不放松校验强度。
    """
    res: dict = {"photo": photo, "ok": False, "stage": "init", "errors": [], "warnings": []}
    raw = open(photo, "rb").read()
    res["file_bytes"] = len(raw)
    res["sha256"] = core.sha256_file(photo)

    with Image.open(io.BytesIO(raw)) as im:
        W, H = im.size
        ori = im.getexif().get(274, None)
    res["size"] = [W, H]
    res["exif_orientation"] = ori
    res["exif_rotation_applied"] = core.exif_rotation(raw)
    if res["exif_rotation_applied"]:
        res["warnings"].append(
            f"EXIF Orientation={ori} → 管线会旋转 {res['exif_rotation_applied']}°，"
            "标注坐标必须按摆正后的图像给出")

    # ---- 标注来源 ----
    bundle = None
    if detection_json:
        res["stage"] = "bundle"
        bundle = core.load_detection_bundle(detection_json)
        try:
            core.verify_photo_binding(bundle, raw, photo)
        except core.FaceInputError as exc:
            fixed = (_try_fix_stale_source_path(detection_json, bundle, raw, photo)
                     if fix_paths else None)
            if fixed:
                res["warnings"].append(f"已自动修复标注的路径记录（内容哈希一致）：{fixed}")
                bundle = core.load_detection_bundle(detection_json)
                core.verify_photo_binding(bundle, raw, photo)
            else:
                raise _annotate_binding_error(exc, bundle, raw, photo, detection_json)
        _check_bind_apply(bundle)
        res["detection_source"] = detection_json
        res["bound"] = True
    elif auto_detect:
        res["stage"] = "detect"
        can, why = detect_available()
        if not can:
            res["errors"].append(f"无标注文件，且自动检测不可用：{why}")
            return res
        det = detect_face(photo)
        if det is None:
            res["errors"].append("自动检测未找到人脸（可换一张，或手工提供标注 JSON）")
            return res
        bundle = core.DetectionBundle(static=core.FaceDetection(
            box=tuple(det["box"]), points=[tuple(p) for p in det["points"]],
            score=det["score"], space="image"))
        res["detection_source"] = "auto(YuNet)"
        res["bound"] = False
        res["detection_raw"] = det
    else:
        res["errors"].append("需要标注文件（--no-detect 时不会自动检测）")
        return res

    if bundle.static is None:
        res["errors"].append("标注里没有静态检测（static/旧版单标注）")
        return res

    # ---- 取景质量门（原图坐标系上复算，便于看每个数字）----
    res["stage"] = "gate"
    img = Image.open(io.BytesIO(raw))
    img.load()
    det_obj = bundle.static
    metrics = _raw_metrics(det_obj, W, H)
    res["metrics"] = metrics
    gate = core.quality_gate(img, det_obj, None)
    res["gate_ok"] = gate.ok
    res["gate_reason"] = gate.reason
    if not gate.ok:
        res["errors"].append(f"取景质量门未通过：{gate.reason}")
        return res

    # ---- 压缩链（与项目预检同一函数）----
    res["stage"] = "compress"
    source = core.PhotoSource(photo, mirror=False)
    loaded, meta = source.load()
    runner_like = _RunnerLike(source, bundle.static)
    face = runner_like.build_face_image(loaded, meta)
    res["upload_bytes"] = len(face.data)
    res["upload_steps"] = list(face.steps)
    with Image.open(io.BytesIO(face.data)) as o:
        res["upload_size"] = list(o.size)
    res["base64_len"] = len(face.base64)
    res["base64_no_wrap"] = "\n" not in face.base64
    if want_upload:
        res["_upload_bytes_data"] = face.data

    res["ok"] = True
    res["stage"] = "done"
    return res


class _RunnerLike:
    """复刻 FaceRunner 的预处理段（不加任何网络/状态机逻辑）。

    直接用项目规则里的 build_face_image 语义：中间态 JPEG q95 → 质量门 → process_face_image。
    """

    def __init__(self, source, detection):
        self.source = source
        self.detection = detection

    def build_face_image(self, image, meta):
        img_bytes = core.rules.save_jpeg(image, 95)
        gate = core.quality_gate(image, self.detection, None)
        if not gate.ok:
            raise core.FaceInputError(f"取景质量门未通过: {gate.reason}")
        out = core.process_face_image(img_bytes, branch="compare", mirror_input=False,
                                      apply_exif=False)
        out.steps.insert(0, f"source=photo gate=pitch={gate.pitch:.3f} "
                            f"yaw={gate.yaw:.3f} roll={gate.roll_deg:.2f}")
        return out


def _check_bind_apply(bundle) -> None:
    """与 main._check_bind_apply 同规则：标注必须声明坐标系且与预处理一致。"""
    ba = bundle.bind_apply or {}
    if "after_exif" not in ba or "mirrored" not in ba:
        raise core.FaceInputError(
            '标注缺少 bind_apply 坐标约定（需 {"after_exif":bool,"mirrored":bool}）')
    if bool(ba.get("after_exif")) is not True:
        raise core.FaceInputError(
            "标注 bind_apply.after_exif=false：管线始终按 EXIF 摆正后处理，拒绝")
    if bool(ba.get("mirrored")) is not False:
        raise core.FaceInputError(
            "标注 bind_apply.mirrored 与本工具的默认不镜像不一致：坐标系矛盾，拒绝")


def _raw_metrics(det: core.FaceDetection, W: int, H: int) -> dict:
    """把门限的每个中间量算出来（与 check_framing 同公式），便于解释与调参。"""
    x1, y1, x2, y2 = det.box
    (p0x, p0y), (p1x, p1y), (p2x, p2y), (p3x, p3y), (p4x, p4y) = det.points
    import math
    eye_mid_y = (p0y + p1y) / 2.0
    w = x2 - x1
    return {
        "box": [round(v, 1) for v in det.box],
        "face_px": [round(w, 1), round(y2 - y1, 1)],
        "score": round(det.score, 4),
        "width_ratio": round(w / float(W), 4),
        "top": round(y1 / H, 4),
        "bottom": round(y2 / H, 4),
        "left": round(x1 / W, 4),
        "right": round(x2 / W, 4),
        "yaw": round(abs(core.java_div(p2x - (p0x + p1x) / 2.0, p1x - p0x)), 4),
        "pitch": round(core.java_div(p2y - eye_mid_y,
                                     ((p3y + p4y) / 2.0) - eye_mid_y), 4),
        "roll_deg": round(abs(math.degrees(math.atan2(p1y - p0y, p1x - p0x))), 3),
    }


def headroom(res: dict) -> dict:
    """余量分析：缩小多少能过占比上限、裁多少能过底边限制。"""
    m = res.get("metrics")
    if not m:
        return {}
    W, H = res["size"]
    out = {}
    if m["width_ratio"] > 0.65:
        out["缩小百分比可过占比上限"] = round((1 - 0.65 / m["width_ratio"]) * 100, 1)
    if m["width_ratio"] < 0.40:
        out["需要放大百分比才到占比下限"] = round((0.40 / m["width_ratio"] - 1) * 100, 1)
    if m["bottom"] > 0.80:
        out["需上移（框底超出图高的比例）"] = round((m["bottom"] - 0.80) * 100, 2)
    if m["top"] < 0.20:
        out["需下移（框顶不足图高的比例）"] = round((0.20 - m["top"]) * 100, 2)
    return out


# ---------------------------------------------------------------- 输出
def render(res: dict, verbose: bool = True) -> str:
    L = []
    L.append("=" * 92)
    L.append(f"文件: {res['photo']}")
    L.append(f"  SHA256 : {res.get('sha256', '')}")
    if "size" in res:
        L.append(f"  尺寸   : {res['size'][0]}x{res['size'][1]}  "
                 f"文件 {res.get('file_bytes', 0) / 1024:.1f} KB  "
                 f"EXIF方向={res.get('exif_orientation')} "
                 f"（管线旋转 {res.get('exif_rotation_applied')}°）")
    if "detection_source" in res:
        L.append(f"  标注   : {res['detection_source']}"
                 f"{'（内容哈希绑定已校验）' if res.get('bound') else '（未绑定来源）'}")
    m = res.get("metrics")
    if m:
        L.append(f"  人脸框 : {m['box']}  人脸尺寸 {m['face_px']}px  分数 {m['score']}")
        L.append("")
        L.append(f"  {'检查项':<10}{'实测':>10}   {'要求':<16}{'判定'}")
        L.append("  " + "-" * 74)
        rows = [
            ("占宽", f"{m['width_ratio']:.3f}", "0.400 ~ 0.650", 0.40 <= m["width_ratio"] <= 0.65),
            ("top", f"{m['top']:.3f}", ">= 0.200", m["top"] >= 0.20),
            ("bottom", f"{m['bottom']:.3f}", "<= 0.800", m["bottom"] <= 0.80),
            ("left", f"{m['left']:.3f}", ">= 0.100", m["left"] >= 0.10),
            ("right", f"{m['right']:.3f}", "<= 0.900", m["right"] <= 0.90),
            ("偏航", f"{m['yaw']:.3f}", "<= 0.150", m["yaw"] <= 0.15),
            ("俯仰", f"{m['pitch']:.3f}", "0.300 ~ 0.800", 0.30 <= m["pitch"] <= 0.80),
            ("翻滚", f"{m['roll_deg']:.2f}°", "<= 15°", m["roll_deg"] <= 15.0),
            ("分数", f"{m['score']:.3f}", ">= 0.600", m["score"] >= 0.60),
        ]
        for name, val, req, ok in rows:
            L.append(f"  {name:<10}{val:>10}   {req:<16}{'✔' if ok else '✘ 越界'}")
    if res.get("upload_bytes"):
        L.append("")
        L.append(f"  上传结果: {res['upload_size'][0]}x{res['upload_size'][1]}  "
                 f"{res['upload_bytes']} B（上限 {core.MAX_BYTES}）  "
                 f"base64 {res['base64_len']} 字符"
                 f"{'（无换行 ✔）' if res.get('base64_no_wrap') else '（含换行 ✘）'}")
        if verbose:
            for s in res.get("upload_steps", []):
                L.append(f"      · {s}")
    hr = headroom(res)
    if hr:
        L.append("")
        L.append("  余量分析（差多少才达标）:")
        for k, v in hr.items():
            L.append(f"      · {k}: {v}")
    for w in res.get("warnings", []):
        L.append(f"  ⚠ {w}")
    for e in res.get("errors", []):
        L.append(f"  ✘ {e}")
    L.append("")
    verdict = "✅ 通过（可交给项目预检）" if res["ok"] else f"❌ 不通过（阶段: {res.get('stage')}）"
    L.append(f"  结论: {verdict}")
    L.append("=" * 92)
    return "\n".join(L)


def render_show(photo: str, bundle, out_path: str) -> str:
    """画框 + 五点 + 门限参考线，用于目视核对。"""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.fromfile(photo, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise core.FaceInputError(f"图像解码失败: {photo}")
    H, W = img.shape[:2]
    s = min(1.0, 1024.0 / W)
    vis = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA) if s < 1 else img
    vh, vw = vis.shape[:2]

    box = [v * s for v in bundle.static.box]
    x1, y1, x2, y2 = [int(v) for v in box]
    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 3)
    for (px, py) in bundle.static.points:
        cv2.circle(vis, (int(px * s), int(py * s)), 6, (0, 255, 0), -1)
    # 门限参考线：top 0.20 / bottom 0.80（蓝），left 0.10 / right 0.90（青）
    for frac in (0.20, 0.80):
        cv2.line(vis, (0, int(vh * frac)), (vw, int(vh * frac)), (255, 0, 0), 2)
    for frac in (0.10, 0.90):
        cv2.line(vis, (int(vw * frac), 0), (int(vw * frac), vh), (255, 255, 0), 2)
    # 占宽 0.40~0.65 参考竖线（品红）
    cx = (x1 + x2) / 2.0
    for half in (0.40, 0.65):
        edge = cx - vw * half / 2.0
        cv2.line(vis, (int(edge), 0), (int(edge), vh), (255, 0, 255), 1)
        cv2.line(vis, (int(cx + vw * half / 2.0), 0), (int(cx + vw * half / 2.0), vh),
                 (255, 0, 255), 1)
    cv2.imwrite(out_path, vis, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out_path


# ---------------------------------------------------------------- CLI
def cmd_rules(_args) -> int:
    print("人脸输入「上传前预检」全部阈值（规则代码：core/rules.py）")
    print("=" * 92)
    print(core.thresholds_table())
    print()
    ok, why = detect_available()
    print(f"自动检测（YuNet）: {'可用' if ok else '不可用 — ' + why}")
    return 0


def cmd_check(args) -> int:
    det = args.detection
    if det and not os.path.isfile(det):
        print(f"标注文件不存在: {det}")
        return 2
    try:
        res = check_photo(args.photo, det, auto_detect=not args.no_detect,
                          want_upload=bool(args.save_upload),
                          fix_paths=bool(args.fix_paths))
    except core.FaceInputError as exc:
        print(f"✘ 预检失败: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"✘ 未预期错误: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2
    print(render(res, verbose=not args.quiet))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in res.items() if not k.startswith("_")},
                      fh, ensure_ascii=False, indent=2)
        print(f"结果 JSON 已写入: {args.json_out}")
    if args.save_upload and res.get("_upload_bytes_data"):
        p = args.save_upload
        with open(p, "wb") as fh:
            fh.write(res["_upload_bytes_data"])
        print(f"实际上传图已写入: {p}")
    return 0 if res["ok"] else 1


def cmd_gen(args) -> int:
    folder = args.folder
    if not os.path.isdir(folder):
        print(f"目录不存在: {folder}")
        return 2
    # --in-place：标注写在照片旁边（照片整体移动时标注跟着走，不会出现路径漂移）
    out_dir = folder if args.in_place else (args.out or os.path.join(HERE, "out"))
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp")))
    if not files:
        print(f"目录下没有图片: {folder}")
        return 2

    passed, failed = [], []
    print(f"共 {len(files)} 张，输出目录 {out_dir}\n")
    for name in files:
        photo = os.path.join(folder, name)
        stem = os.path.splitext(name)[0]
        try:
            det = detect_face(photo)
        except core.FaceInputError as exc:
            print(f"{name:<14} ✘ 解码失败: {exc}")
            failed.append(name)
            continue
        if det is None:
            print(f"{name:<14} ✘ 未检出人脸")
            failed.append(name)
            continue
        ann = detection_to_json(photo, det)
        ann_path = os.path.join(out_dir, stem + ".json")
        with open(ann_path, "w", encoding="utf-8") as fh:
            json.dump(ann, fh, ensure_ascii=False, indent=2)
        res = check_photo(photo, ann_path, auto_detect=False)
        m = res.get("metrics") or {}
        tag = "✅ 通过" if res["ok"] else "❌ 不通过"
        detail = (f"占宽{m.get('width_ratio', float('nan')):.3f} "
                  f"俯仰{m.get('pitch', float('nan')):.3f} "
                  f"偏航{m.get('yaw', float('nan')):.3f} "
                  f"翻滚{m.get('roll_deg', float('nan')):.2f}°") if m else ""
        upload = f" 上传{res['upload_bytes']}B/{res['upload_size'][0]}x{res['upload_size'][1]}" \
            if res.get("upload_bytes") else ""
        print(f"{name:<14} {tag}  {detail}{upload}")
        if not res["ok"]:
            for e in res["errors"]:
                print(f"{'':<14}    ✘ {e}")
            failed.append(name)
        else:
            passed.append(name)
        if args.preview:
            try:
                bundle = core.load_detection_bundle(ann_path)
                render_show(photo, bundle, os.path.join(out_dir, f"preview_{stem}.jpg"))
            except Exception:  # noqa: BLE001
                pass

    print("\n" + "=" * 92)
    print(f"通过 {len(passed)}/{len(files)}；不通过 {len(failed)}/{len(files)}")
    if failed:
        print("不通过：" + ", ".join(failed))
    print(f"标注 JSON 与预览图在: {out_dir}")
    print("=" * 92)
    return 0 if not failed else 1


def cmd_video(args) -> int:
    """视频源预检（--face-video 分支）。"""
    import video_preflight as vp

    try:
        res = vp.check_video(
            args.video, args.detection, mirror=args.mirror,
            frame_step=args.frame_step, max_frames=args.max_frames,
            auto_detect=not args.no_detect, want_upload=bool(args.save_upload),
            probe_frames=args.probe_frames)
    except core.FaceInputError as exc:
        print(f"✘ 视频预检失败: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"✘ 未预期错误: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2

    print(render_video(res))
    if args.probe_frames and res.get("per_frame"):
        print(f"\n抽样帧明细（frame_step={res.get('frame_step')}，共 {len(res['per_frame'])} 帧）：")
        print(f"  {'帧号':>6} {'检测':<5}{'分数':<8}{'占宽':<9}{'俯仰':<8}"
              f"{'偏航':<8}{'翻滚':<8}{'门':<6}原因")
        print("  " + "-" * 86)
        for r in res["per_frame"]:
            if not r.get("detected"):
                print(f"  {r['frame']:>6} {'无':<5}{'-':<8}{'-':<9}{'-':<8}{'-':<8}{'-':<8}{'-':<6}")
                continue
            print(f"  {r['frame']:>6} {'有':<5}{r['score']:<8.3f}{r['width_ratio']:<9.3f}"
                  f"{r['pitch']:<8.3f}{r['yaw']:<8.3f}{r['roll_deg']:<8.2f}"
                  f"{'✔' if r.get('gate_ok') else '✘':<6}{r.get('gate_reason', '')}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in res.items() if not k.startswith("_")},
                      fh, ensure_ascii=False, indent=2)
        print(f"\n结果 JSON 已写入: {args.json_out}")
    if args.out_annotation and res.get("_annotation"):
        os.makedirs(os.path.dirname(os.path.abspath(args.out_annotation)), exist_ok=True)
        with open(args.out_annotation, "w", encoding="utf-8") as fh:
            json.dump(res["_annotation"], fh, ensure_ascii=False, indent=2)
        print(f"选中帧标注已写入: {args.out_annotation}"
              "（含 frames + static + source_sha256，可直接给项目 --face-detection）")
    if args.save_upload and res.get("_upload_bytes_data"):
        with open(args.save_upload, "wb") as fh:
            fh.write(res["_upload_bytes_data"])
        print(f"实际上传图已写入: {args.save_upload}")
    return 0 if res["ok"] else 1


def render_video(res: dict) -> str:
    L = ["=" * 92, f"视频: {res['video']}"]
    if "sha256" in res:
        L.append(f"  SHA256 : {res['sha256']}")
        L.append(f"  文件   : {res.get('file_bytes', 0) / 1024:.1f} KB")
    vi = res.get("video_info")
    if vi:
        L.append(f"  规格   : {vi['width']}x{vi['height']}  {vi['fps']} fps  "
                 f"{vi['frame_count']} 帧  时长 {vi.get('duration_s')}s  编码 {vi.get('fourcc')!r}")
    L.append(f"  抽帧   : frame_step={res.get('frame_step')}  期望每 {res.get('frame_step')} 帧取 1 帧"
             f"  镜像={res.get('mirror')}")
    if "detection_source" in res:
        L.append(f"  标注   : {res['detection_source']}"
                 f"{'（内容哈希绑定已校验）' if res.get('bound') else '（未绑定来源）'}"
                 f"  标注帧数={res.get('frames_annotated')}")
    if "chosen_frame" in res:
        L.append(f"  选中帧 : #{res['chosen_frame']}（按帧号升序、第一个过门；"
                 f"已尝试 {res.get('frames_tried')} 帧）")
    m = res.get("metrics")
    if m:
        L.append(f"  人脸框 : {m['box']}  人脸尺寸 {m['face_px']}px  分数 {m['score']}")
        L.append("")
        L.append(f"  {'检查项':<10}{'实测':>10}   {'要求':<16}{'判定'}")
        L.append("  " + "-" * 74)
        rows = [
            ("占宽", f"{m['width_ratio']:.3f}", "0.400 ~ 0.650", 0.40 <= m["width_ratio"] <= 0.65),
            ("top", f"{m['top']:.3f}", ">= 0.200", m["top"] >= 0.20),
            ("bottom", f"{m['bottom']:.3f}", "<= 0.800", m["bottom"] <= 0.80),
            ("left", f"{m['left']:.3f}", ">= 0.100", m["left"] >= 0.10),
            ("right", f"{m['right']:.3f}", "<= 0.900", m["right"] <= 0.90),
            ("偏航", f"{m['yaw']:.3f}", "<= 0.150", m["yaw"] <= 0.15),
            ("俯仰", f"{m['pitch']:.3f}", "0.300 ~ 0.800", 0.30 <= m["pitch"] <= 0.80),
            ("翻滚", f"{m['roll_deg']:.2f}°", "<= 15°", m["roll_deg"] <= 15.0),
            ("分数", f"{m['score']:.3f}", ">= 0.600", m["score"] >= 0.60),
        ]
        for name, val, req, ok in rows:
            L.append(f"  {name:<10}{val:>10}   {req:<16}{'✔' if ok else '✘ 越界'}")
    if res.get("upload_bytes"):
        L.append("")
        L.append(f"  上传结果: {res['upload_size'][0]}x{res['upload_size'][1]}  "
                 f"{res['upload_bytes']} B（上限 {core.MAX_BYTES}）  "
                 f"base64 {res['base64_len']} 字符"
                 f"{'（无换行 ✔）' if res.get('base64_no_wrap') else '（含换行 ✘）'}")
        for s in res.get("upload_steps", []):
            L.append(f"      · {s}")
    for w in res.get("warnings", []):
        L.append(f"  ⚠ {w}")
    for e in res.get("errors", []):
        L.append(f"  ✘ {e}")
    L.append("")
    L.append("  结论: " + ("✅ 通过（可交给项目 --face-video 预检）" if res["ok"]
                          else f"❌ 不通过（阶段: {res.get('stage')}）"))
    L.append("=" * 92)
    return "\n".join(L)


def cmd_show(args) -> int:
    if not detect_available()[0] and not args.detection:
        print(f"✘ 画图需要 opencv：{detect_available()[1]}")
        return 2
    det = args.detection
    if det:
        bundle = core.load_detection_bundle(det)
        core.verify_photo_binding(bundle, open(args.photo, "rb").read(), args.photo)
    else:
        d = detect_face(args.photo)
        if d is None:
            print("✘ 未检出人脸，无法画图")
            return 1
        bundle = core.DetectionBundle(static=core.FaceDetection(
            box=tuple(d["box"]), points=[tuple(p) for p in d["points"]],
            score=d["score"], space="image"))
    out = args.out or os.path.join(HERE, "out",
                                   "preview_" + os.path.splitext(os.path.basename(args.photo))[0] + ".jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    render_show(args.photo, bundle, out)
    print(f"预览图已写入: {out}")
    print("红线=人脸框  绿点=五点  蓝线=上下限(0.20/0.80)  青线=左右限(0.10/0.90)  品红=占宽 0.40/0.65")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="face_check.py",
        description="云运动人脸输入「上传前预检」独立工具（规则与项目一致）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("rules", help="打印全部阈值与说明")
    sp.set_defaults(func=cmd_rules)

    sp = sub.add_parser("check", help="检查单张图片")
    sp.add_argument("photo")
    sp.add_argument("detection", nargs="?", default=None, help="标注 JSON（省略则自动检测）")
    sp.add_argument("--no-detect", action="store_true", help="不自动检测，必须有标注")
    sp.add_argument("--quiet", action="store_true", help="不打印压缩链细节")
    sp.add_argument("--json-out", default=None, help="把结果写成 JSON")
    sp.add_argument("--save-upload", default=None, help="把实际会提交的 JPEG 存到该路径")
    sp.add_argument("--fix-paths", dest="fix_paths", action="store_true",
                    help="标注里 source_path 过期但内容哈希一致时，自动删掉该字段并重试")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("gen", help="批量：检查整个目录并产出标注 JSON")
    sp.add_argument("folder")
    sp.add_argument("--out", default=None, help="输出目录（默认 out/）")
    sp.add_argument("--in-place", dest="in_place", action="store_true",
                    help="标注直接写在照片旁边（照片一起移动时不会出现路径漂移，推荐）")
    sp.add_argument("--preview", action="store_true", help="同时生成带参考线的预览图")
    sp.set_defaults(func=cmd_gen)

    sp = sub.add_parser("video", help="检查视频源（--face-video 分支：逐帧标注 + 选帧 + 压缩链）")
    sp.add_argument("video")
    sp.add_argument("detection", nargs="?", default=None,
                    help="逐帧标注 JSON（需含 frames）；省略则用 YuNet 自动逐帧检测")
    sp.add_argument("--frame-step", dest="frame_step", type=int, default=1,
                    help="每 N 帧取 1 帧（默认 1，与项目默认一致）")
    sp.add_argument("--max-frames", dest="max_frames", type=int, default=2000,
                    help="最多读取帧数（默认 2000，与项目默认一致）")
    sp.add_argument("--mirror", action="store_true",
                    help="源本身是镜像预览时开启；标注 bind_apply.mirrored 必须同为 true")
    sp.add_argument("--no-detect", action="store_true", help="不自动逐帧检测，必须有标注")
    sp.add_argument("--probe-frames", action="store_true", help="打印每个抽样帧的门限明细")
    sp.add_argument("--out-annotation", default=None,
                    help="把选中帧标注写成 JSON（可直接给项目 --face-detection）")
    sp.add_argument("--json-out", default=None, help="把结果写成 JSON")
    sp.add_argument("--save-upload", default=None, help="把实际会提交的 JPEG 存到该路径")
    sp.set_defaults(func=cmd_video)

    sp = sub.add_parser("show", help="画框+五点+门限参考线")
    sp.add_argument("photo")
    sp.add_argument("detection", nargs="?", default=None)
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
