# -*- coding: utf-8 -*-
"""视频源预检：与项目 `main.build_face_runner` 的视频分支同序同规则。

项目侧的次序（逐条复刻，不自行发明）：
    1. 视频文件存在
    2. 标注必须提供 `frames`（逐帧映射）——静态单标注不能冒充"每帧都检测到"
    3. `bind_apply` 坐标声明必须与镜像设置一致
    4. 标注必须带 `source_sha256`，且与视频文件内容哈希一致（换视频沿用旧帧号必被拒）
    5. 抽帧依赖 opencv 必须可用
    6. 真实选帧链预检：按 frame_step 升序抽帧 → 逐帧标注匹配 → 取景质量门
       → 取"第一个合格帧" → 走与照片相同的压缩链
    7. 全部帧都不合格 → 失败（不宣称通过）

抽帧与选择规则来自项目 `VideoFrameSource`（按帧号升序、每 frame_step 帧取样、
取第一个过门的帧），本模块直接调用它，不重写。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import core


def video_probe(path: str) -> Dict[str, Any]:
    """读取视频基本信息（分辨率/帧率/帧数/时长/编码）。打不开则抛 FaceInputError。"""
    try:
        import cv2
    except ImportError as exc:
        raise core.FaceInputError(
            "读取视频信息需要 opencv：pip install opencv-python（或用 face_check.ps1 启动器）"
        ) from exc

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise core.FaceInputError(f"视频无法打开: {path}")
    try:
        info = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "fps": round(float(cap.get(cv2.CAP_PROP_FPS) or 0.0), 3),
            "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
        }
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        info["fourcc"] = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00")
        if info["fps"] > 0 and info["frame_count"] > 0:
            info["duration_s"] = round(info["frame_count"] / info["fps"], 2)
        else:
            info["duration_s"] = None
    finally:
        cap.release()
    return info


def _check_bind_apply(bundle, mirrored: bool) -> None:
    """与项目 main._check_bind_apply 同规则。"""
    ba = bundle.bind_apply or {}
    if "after_exif" not in ba or "mirrored" not in ba:
        raise core.FaceInputError(
            '视频标注缺少 bind_apply 坐标约定（需 {"after_exif":bool,"mirrored":bool}）')
    if bool(ba.get("after_exif")) is not True:
        raise core.FaceInputError(
            "视频标注 bind_apply.after_exif=false：帧坐标必须按摆正后声明，拒绝")
    if bool(ba.get("mirrored")) is not mirrored:
        raise core.FaceInputError(
            f"视频标注 bind_apply.mirrored={ba.get('mirrored')!r} 与实际镜像设置"
            f"{mirrored!r} 不一致：坐标系矛盾，拒绝")


def check_video(video: str, detection_json: Optional[str] = None,
                mirror: bool = False, frame_step: int = 1,
                max_frames: int = 2000, auto_detect: bool = True,
                want_upload: bool = False,
                probe_frames: bool = False) -> Dict[str, Any]:
    """视频预检主入口。返回结构化结果（失败也带原因与阶段）。

    auto_detect=True 且未给标注时：用 YuNet 逐帧检测并**自动生成** frames 标注，
    再按同一流程预检——省掉手工逐帧标注，但检测模型与项目的 RetinaFace 不同源。
    """
    res: Dict[str, Any] = {"video": video, "ok": False, "stage": "init",
                           "errors": [], "warnings": [],
                           "mirror": mirror, "frame_step": max(1, int(frame_step))}
    if not os.path.isfile(video):
        res["errors"].append(f"视频文件不存在: {video}")
        return res

    res["sha256"] = core.sha256_file(video)
    res["file_bytes"] = os.path.getsize(video)

    # ---- 1/2/3/4 步：标注与绑定 ----
    res["stage"] = "bundle"
    bundle = None
    if detection_json:
        if not os.path.isfile(detection_json):
            res["errors"].append(f"标注文件不存在: {detection_json}")
            return res
        bundle = core.load_detection_bundle(detection_json)
        res["detection_source"] = detection_json
        if not bundle.frames:
            res["errors"].append(
                "视频源需要逐帧检测标注（标注 JSON 的 frames 帧号映射）；"
                "静态单标注不能冒充每帧检测")
            return res
        _check_bind_apply(bundle, mirror)
        want = bundle.source_sha256
        if not want:
            res["errors"].append("视频标注缺少 source_sha256（内容哈希）绑定："
                                 "换视频沿用同帧号标注必须被拒绝")
            return res
        if want != res["sha256"]:
            res["errors"].append(
                f"视频标注 source_sha256 与实际视频不符（{res['sha256'][:12]}… != "
                f"{want[:12]}…）：错源标注，拒绝当作检测成功")
            return res
        res["bound"] = True
        res["frames_annotated"] = len(bundle.frames)
    else:
        can, why = _detect_available()
        if not auto_detect or not can:
            res["errors"].append(
                "视频源需要逐帧标注（frames）；未提供且自动检测不可用："
                + (why if not can else "已指定 --no-detect"))
            return res
        res["stage"] = "auto-annotate"
        frames, per_frame = auto_annotate_video(video, frame_step=frame_step,
                                               max_frames=max_frames, mirror=mirror)
        res["per_frame"] = per_frame
        if not frames:
            res["errors"].append("自动逐帧检测在所有抽样帧上都没找到合格人脸")
            return res
        bundle = core.DetectionBundle(frames=frames, source_sha256=res["sha256"],
                                      bind_apply={"after_exif": True, "mirrored": mirror})
        res["detection_source"] = f"auto(YuNet, {len(frames)} 帧)"
        res["bound"] = False
        res["frames_annotated"] = len(frames)

    # ---- 5 步：抽帧依赖 ----
    res["stage"] = "opencv"
    try:
        import cv2  # noqa: F401
    except ImportError:
        res["errors"].append("视频抽帧需要 opencv：pip install opencv-python")
        return res

    try:
        res["video_info"] = video_probe(video)
    except core.FaceInputError as exc:
        res["errors"].append(str(exc))
        return res

    # ---- 6 步：真实选帧链预检（调用项目的 VideoFrameSource）----
    res["stage"] = "select"
    detection = dict(bundle.frames)          # 项目也是 dict，且**带帧号**匹配
    source = core.rules.VideoFrameSource(video, frame_step=frame_step,
                                         max_frames=max_frames, mirror=mirror)

    def gate_for_video(image, frame_index=None):
        """与项目 FaceRunner._gate_for_video 同规则：逐帧检测 → 取景门 + 分数门。"""
        size = (image.width, image.height)
        pw, ph = size
        det = detection.get(frame_index) if isinstance(detection, dict) else detection
        if det is None:
            return core.GateResult(False, "该帧无检测标注（未标注帧一律视为无检测）")
        rect, pts = core.detection_to_preview(det, size, (pw, ph))
        out = core.check_framing(rect, pts, pw, ph)
        if out.ok and det.score < core.MANUAL_SHOT_MIN_SCORE:
            return core.GateResult(False, f"检测分数 {det.score} 低于 "
                                          f"{core.MANUAL_SHOT_MIN_SCORE}（手动拍摄门）")
        return out

    try:
        img, meta = source.select(gate_for_video)
    except core.FaceInputError as exc:
        res["errors"].append(f"视频源预检失败（不存在解码成功且标注/质量门通过的帧）：{exc}")
        if probe_frames and res.get("per_frame"):
            res["warnings"].append("已列出各抽样帧的门限结果，见 per_frame")
        return res

    res["chosen_frame"] = meta.get("frame_index")
    res["frames_tried"] = meta.get("frames_tried")
    res["chosen_frame_meta"] = meta

    # ---- 7 步：压缩链（与照片同一函数）----
    res["stage"] = "compress"
    det = detection.get(res["chosen_frame"])
    gate = core.quality_gate(img, det, None)
    res["gate_ok"] = gate.ok
    res["gate_reason"] = gate.reason
    if not gate.ok:
        res["errors"].append(f"取景质量门未通过: {gate.reason}")
        return res
    res["metrics"] = metrics_of(det, img.width, img.height)

    from PIL import Image
    import io
    img_bytes = core.rules.save_jpeg(img, 95)
    out = core.process_face_image(img_bytes, branch="compare", mirror_input=False,
                                 apply_exif=False)
    res["upload_bytes"] = len(out.data)
    res["upload_steps"] = list(out.steps)
    with Image.open(io.BytesIO(out.data)) as o:
        res["upload_size"] = list(o.size)
    res["base64_len"] = len(out.base64)
    res["base64_no_wrap"] = "\n" not in out.base64
    if want_upload:
        res["_upload_bytes_data"] = out.data

    # 保存"选中帧+标注"，供项目直接复用
    res["_annotation"] = {
        "frames": {str(res["chosen_frame"]): {
            "box": [round(v, 2) for v in det.box],
            "points": [[round(x, 2), round(y, 2)] for x, y in det.points],
            "score": round(det.score, 4), "space": "image"}},
        "static": {
            "box": [round(v, 2) for v in det.box],
            "points": [[round(x, 2), round(y, 2)] for x, y in det.points],
            "score": round(det.score, 4), "space": "image"},
        "source_sha256": res["sha256"],
        "bind_apply": {"after_exif": True, "mirrored": mirror},
    }
    res["ok"] = True
    res["stage"] = "done"
    return res


def _detect_available() -> Tuple[bool, str]:
    """检测是否可用（复用 face_check 的判断口径）。"""
    from face_check import detect_available
    return detect_available()


def metrics_of(det, W: int, H: int) -> Dict[str, float]:
    """复算门限各中间量（与 check_framing 同公式）。"""
    import math
    x1, y1, x2, y2 = det.box
    (p0x, p0y), (p1x, p1y), (p2x, p2y), (p3x, p3y), (p4x, p4y) = det.points
    eye_mid_y = (p0y + p1y) / 2.0
    w = x2 - x1
    return {
        "box": [round(v, 1) for v in det.box],
        "face_px": [round(w, 1), round(y2 - y1, 1)],
        "score": round(det.score, 4),
        "width_ratio": round(w / float(W), 4),
        "top": round(y1 / H, 4), "bottom": round(y2 / H, 4),
        "left": round(x1 / W, 4), "right": round(x2 / W, 4),
        "yaw": round(abs(core.java_div(p2x - (p0x + p1x) / 2.0, p1x - p0x)), 4),
        "pitch": round(core.java_div(p2y - eye_mid_y,
                                     ((p3y + p4y) / 2.0) - eye_mid_y), 4),
        "roll_deg": round(abs(math.degrees(math.atan2(p1y - p0y, p1x - p0x))), 3),
    }


def auto_annotate_video(video: str, frame_step: int = 1, max_frames: int = 2000,
                        mirror: bool = False, scan_all: bool = True
                        ) -> Tuple[Dict[int, Any], List[Dict[str, Any]]]:
    """逐帧自动检测，产出 {帧号: FaceDetection} 与逐帧诊断表。

    与项目的"人工逐帧标注"等价替换：对每个抽样帧跑一次检测，检不到即该帧无检测
    （不放行），不做任何跨帧挪用。
    """
    from face_check import detect_frame_array, detect_available
    import core as _c

    can, why = detect_available()
    if not can:
        raise _c.FaceInputError(f"自动逐帧检测不可用：{why}")

    frames: Dict[int, Any] = {}
    per_frame: List[Dict[str, Any]] = []
    source = _c.rules.VideoFrameSource(video, frame_step=frame_step,
                                       max_frames=max_frames, mirror=mirror)
    for idx, pil_img in source.candidates():
        det = detect_frame_array(pil_img)
        row: Dict[str, Any] = {"frame": idx, "detected": det is not None}
        if det is not None:
            frames[idx] = _c.FaceDetection(box=tuple(det["box"]),
                                           points=[tuple(p) for p in det["points"]],
                                           score=det["score"], space="image")
            m = metrics_of(frames[idx], pil_img.width, pil_img.height)
            row.update({"score": det["score"], "width_ratio": m["width_ratio"],
                        "pitch": m["pitch"], "yaw": m["yaw"], "roll_deg": m["roll_deg"],
                        "top": m["top"], "bottom": m["bottom"]})
            # 该帧是否过门（用于诊断表）
            rect, pts = _c.detection_to_preview(frames[idx],
                                                (pil_img.width, pil_img.height),
                                                (pil_img.width, pil_img.height))
            g = _c.check_framing(rect, pts, pil_img.width, pil_img.height)
            row["gate_ok"] = bool(g.ok)
            row["gate_reason"] = g.reason
        per_frame.append(row)
        if not scan_all and det is not None and row.get("gate_ok"):
            break
    return frames, per_frame
