from pathlib import Path
import json
import tempfile
import configparser

import web_shared
import web_app


def test_map_config_defaults_and_fallback_to_ini():
    with tempfile.TemporaryDirectory() as td:
        ini_path = Path(td) / "config.ini"
        ini_path.write_text("[User]\nmap_key = MYSUPERMAPKEY123\n", encoding="utf-8")
        
        cfg = web_shared.get_map_config(td)
        assert cfg["provider"] == "amap"
        assert cfg["map_type"] == "normal"
        assert cfg["amap_key"] == "MYSUPERMAPKEY123"
        assert cfg.get("inherited_from_ini") is True
        assert cfg["osm_tile_url"].startswith("https://tile.openstreetmap.org")


def test_map_config_save_and_reload():
    with tempfile.TemporaryDirectory() as td:
        payload = {
            "provider": "osm",
            "map_type": "satellite",
            "amap_key": "KEY_AMAP",
            "baidu_ak": "AK_BAIDU",
            "tencent_key": "KEY_TENCENT",
            "osm_tile_url": "https://custom.tiles/{z}/{x}/{y}.png",
            "fallback_canvas": False,
        }
        saved = web_shared.save_map_config(payload, td)
        assert saved["provider"] == "osm"
        assert saved["map_type"] == "satellite"
        assert saved["baidu_ak"] == "AK_BAIDU"
        assert saved["tencent_key"] == "KEY_TENCENT"

        # 重新读取
        reloaded = web_shared.get_map_config(td)
        assert reloaded["provider"] == "osm"
        assert reloaded["map_type"] == "satellite"
        assert reloaded["osm_tile_url"] == "https://custom.tiles/{z}/{x}/{y}.png"
        assert reloaded["fallback_canvas"] is False


def test_map_config_ui_and_map_key_omitted_from_control_config_form():
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    app_js = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    # 1. 验证导航栏中存在“地图配置”Tab及面板
    assert 'id="tab-mapcfg"' in html
    assert '<span>地图配置</span>' in html
    assert 'id="pane-mapcfg"' in html
    assert 'id="p-card-amap"' in html
    assert 'id="p-card-baidu"' in html
    assert 'id="p-card-tencent"' in html
    assert 'id="p-card-osm"' in html
    assert 'id="mapTestHost"' in html

    # 2. 验证前端多地图提供商支持及切换函数
    assert "MAP_SYSTEM" in app_js
    assert "selectMapProvider" in app_js
    assert "loadBaiduMap" in app_js
    assert "loadTencentMap" in app_js
    assert "loadLeaflet" in app_js
    assert "gcj02ToBd09" in app_js

    # 3. 验证项目配置面板中显式跳过了 map_key 的渲染（移除学号 map_key 栏）
    assert "key.toLowerCase() === 'map_key'" in app_js


def test_save_control_config_preserves_existing_map_key():
    with tempfile.TemporaryDirectory() as td:
        ini_path = Path(td) / "config.ini"
        ini_path.write_text("[User]\naccount = 123\nmap_key = PRESERVED_MAP_KEY_999\n", encoding="utf-8")

        # 模拟前端保存配置：payload 中不含 map_key（因为 UI 已移除）
        payload = {"User": {"account": "456", "token": "NEW_TOKEN"}}
        web_shared.save_config(payload, str(ini_path))

        cp = configparser.ConfigParser()
        cp.read(ini_path, encoding="utf-8")
        assert cp.get("User", "account") == "456"
        assert cp.get("User", "token") == "NEW_TOKEN"
        # 验证 map_key 完好保留，未被删除或清空
        assert cp.get("User", "map_key") == "PRESERVED_MAP_KEY_999"


def test_map_layer_types_ui_and_logic():
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    app_js = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    # 1. 验证 HTML 中存在地图图层选择框及变更触发事件
    assert 'id="cfg_map_type"' in html
    assert 'onchange="onMapTypeChanged(this.value)"' in html
    assert 'id="mapTypeDesc"' in html

    # 2. 验证前端包含 MAP_LAYERS 字典定义，涵盖各提供商图层配置
    assert "const MAP_LAYERS = {" in app_js
    assert "amap:" in app_js
    assert "baidu:" in app_js
    assert "tencent:" in app_js
    assert "osm:" in app_js

    # 3. 验证卫星、混合、暗色等常见图层类型
    assert "satellite" in app_js
    assert "hybrid" in app_js
    assert "dark" in app_js
    assert "TopoMap" in app_js or "topo" in app_js

    # 4. 验证动态更新函数与事件处理
    assert "updateMapTypeOptions" in app_js
    assert "onMapTypeChanged" in app_js
    assert "updateMapTypeDesc" in app_js

    # 5. 验证在渲染多段线轨迹时将 map_type 应用至各提供商地图
    assert "const mapType = MAP_SYSTEM.config.map_type || 'normal';" in app_js
    assert "TileLayer.Satellite" in app_js
    assert "BMAP_SATELLITE_MAP" in app_js
    assert "mapTypeId = 'satellite'" in app_js
    assert "World_Imagery" in app_js


def test_web_app_map_config_api_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "HERE", str(tmp_path))
    client = web_app.app.test_client()
    res_get = client.get("/api/map-config").json
    assert res_get["ok"] is True
    assert "provider" in res_get["config"]
    assert "map_type" in res_get["config"]

    res_post = client.post("/api/map-config", json={"config": {"provider": "osm", "map_type": "topo"}}).json
    assert res_post["ok"] is True
    assert res_post["config"]["provider"] == "osm"
    assert res_post["config"]["map_type"] == "topo"
    assert client.get("/api/map-config").json["config"]["map_type"] == "topo"


