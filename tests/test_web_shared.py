# -*- coding: utf-8 -*-
"""测试 web_shared 模块与 web_app.py 的路线与配置功能。"""
import os
import pytest
from pathlib import Path

import web_shared
import web_app

ROOT = Path(__file__).parents[1]


def test_web_shared_exports():
    """验证 web_shared 导出完整性。"""
    assert hasattr(web_shared, "list_campuses")
    assert hasattr(web_shared, "list_sources")
    assert hasattr(web_shared, "list_geojsons")
    assert hasattr(web_shared, "build_preview")
    assert hasattr(web_shared, "read_config_masked")
    assert hasattr(web_shared, "save_config")
    assert hasattr(web_shared, "MASK")
    assert hasattr(web_shared, "KEEP")
    assert hasattr(web_shared, "SENSITIVE_KEYS")
    assert hasattr(web_shared, "PLAN")


def test_web_app_uses_web_shared():
    """验证 web_app.py 路线元数据与预览直接工作于 web_shared。"""
    meta = web_app.route_meta()
    assert "campuses" in meta
    assert "sources" in meta
    assert isinstance(meta["campuses"], dict)
    assert isinstance(meta["sources"], list)


def test_build_preview_base_scheme():
    """验证基本打表回放预览生成。"""
    campuses = web_shared.list_campuses()
    if not campuses:
        pytest.skip("没有找到校区任务表目录")
    first_campus = next(iter(campuses))
    first_task = campuses[first_campus][0]

    out = web_shared.build_preview({"scheme": ["base"], "campus": [first_campus], "task": [first_task]})
    assert "error" not in out
    assert "ref" in out
    assert "send" in out
    assert "command" in out
    assert out["command"]["argv"][0] == "-a"
