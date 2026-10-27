# -*- coding: utf-8 -*-
"""F:\\Yun2Face 内核：离线人脸预检（取景质量门 + APK 压缩链）。

设计要点
--------
1. **规则代码不手改**。取景门、质量门、压缩链、坐标映射全部来自
   `core/rules.py`——它是 `yunForNewVersion-develop/yun_face.py` 的**原字节副本**
   （SHA256 记录见 README）。任何阈值都不在本文件里重写，避免与项目分叉。
2. **零第三方依赖即可跑基础检查**：只用 Pillow（PIL）。检测模型（YuNet）是可选增强，
   缺 opencv/numpy 时基础检查照常工作，只是不能自动产标注。
3. 为什么需要 `yun_http_stub.py`：原文件顶部 `from yun_http import (...)` 只为
   **上传/比对**路径服务（YunClient 等）。本工具做的是"上传之前的预检"，
   不发任何网络请求，因此用最小替身满足导入，不需要 gmssl/pycryptodome/cryptography。
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_rules():
    """按文件路径加载规则代码，规避模块名冲突。

    `rules.py` 顶部 `from yun_http import (...)` 需要解析到本目录的最小替身；
    这里只在**加载规则代码期间**临时把 `core/` 放到 sys.path 最前，加载完立即
    恢复 sys.path 与 `sys.modules['yun_http']`，避免把全局的 `yun_http`
    也替换成替身（否则同时跑全部测试时，别的模块会 import 到错误的 yun_http）。
    """
    if "yun2face_rules" in sys.modules:
        return sys.modules["yun2face_rules"]
    path = os.path.join(HERE, "rules.py")
    spec = importlib.util.spec_from_file_location("yun2face_rules", path)
    module = importlib.util.module_from_spec(spec)
    saved_yun_http = sys.modules.pop("yun_http", None)
    sys.modules["yun2face_rules"] = module   # dataclass 需要 cls.__module__ 可见
    sys.path.insert(0, HERE)
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(HERE)
        except ValueError:
            pass
        if saved_yun_http is not None:
            sys.modules["yun_http"] = saved_yun_http
        else:
            sys.modules.pop("yun_http", None)
    return module


rules = _load_rules()

# ---- 对外重导出（调用方只依赖 core 的这几个名字）----
FaceInputError = rules.FaceInputError
FaceCompressError = rules.FaceCompressError
FaceDetection = rules.FaceDetection
DetectionBundle = rules.DetectionBundle
GateResult = rules.GateResult
FaceImageOutput = rules.FaceImageOutput
PhotoSource = rules.PhotoSource
check_framing = rules.check_framing
quality_gate = rules.quality_gate
process_face_image = rules.process_face_image
detection_to_preview = rules.detection_to_preview
exif_rotation = rules.exif_rotation
load_detection_bundle = rules.load_detection_bundle
load_detection_json = rules.load_detection_json
sha256_file = rules.sha256_file
verify_photo_binding = rules.verify_photo_binding
java_div = rules.java_div

MAX_WIDTH = rules.MAX_WIDTH
MAX_BYTES = rules.MAX_BYTES
LUBAN_100 = rules.LUBAN_100
QUALITY_LADDER = rules.QUALITY_LADDER
MANUAL_SHOT_MIN_SCORE = rules.MANUAL_SHOT_MIN_SCORE


# ---- 阈值表（供报告/文档引用，值本身仍取自规则代码）----
THRESHOLDS = {
    "框宽占图宽": ("0.40 ~ 0.65", "区间外报“距离摄像头过远/过近”"),
    "框 top": ("≥ 0.20 × 图高", "越界报“请将正脸调整到识别框内”"),
    "框 bottom": ("≤ 0.80 × 图高", "同上"),
    "框 left": ("≥ 0.10 × 图宽", "同上"),
    "框 right": ("≤ 0.90 × 图宽", "同上"),
    "框在图内": ("x1≥0 且 x2≤W 且 y1≥0 且 y2≤H", "同上"),
    "偏航 yaw": ("≤ 0.15", "|鼻尖x − 双眼中点x| ÷ 瞳距"),
    "俯仰 pitch": ("0.30 ~ 0.80", "(鼻尖y − 眼线y) ÷ (嘴角中点y − 眼线y)"),
    "翻滚 roll": ("≤ 15°", "双眼连线倾角"),
    "检测分数": (f"≥ {rules.MANUAL_SHOT_MIN_SCORE}", "JTFaceCompareActivity:194 手动拍摄门"),
    "上传宽度": (f"≤ {rules.MAX_WIDTH} px", "FaceImageCompressor：仅宽 >720 才等比缩放"),
    "上传体积": (f"≤ {rules.MAX_BYTES} B (150KB)", "超限走质量阶梯 80,75,…,20"),
    "质量阶梯": (f"{rules.QUALITY_LADDER[0]}..{rules.QUALITY_LADDER[-1]} 步长 5", "全部超限 → FaceCompressError"),
}


def thresholds_table() -> str:
    """渲染阈值表（纯文本，供 CLI/报告复用）。"""
    lines = [f"{'检查项':<12}{'要求':<34}说明", "-" * 92]
    for k, (req, note) in THRESHOLDS.items():
        lines.append(f"{k:<12}{req:<34}{note}")
    return "\n".join(lines)
