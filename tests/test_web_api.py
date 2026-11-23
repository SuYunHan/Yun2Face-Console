from base64 import b64decode
from io import BytesIO

import pytest
from PIL import Image

import web_app


@pytest.mark.parametrize("path", ["/", "/index.html", "/app.js", "/static/app.js", "/api/missing"])
def test_api_does_not_serve_frontend(path):
    response = web_app.app.test_client().get(path)

    assert response.status_code == 404
    assert response.json["ok"] is False


def test_api_only_accepts_json_objects_for_writes():
    client = web_app.app.test_client()

    assert client.post("/api/students", data="{}").status_code == 415
    for response in (
        client.post("/api/students", json=[]),
        client.post("/api/students", data="{", content_type="application/json"),
    ):
        assert response.status_code == 400
        assert response.json["ok"] is False


def test_wrong_method_returns_json_and_the_allowed_methods():
    response = web_app.app.test_client().post("/api/config", json={})

    assert response.status_code == 405
    assert response.json["ok"] is False
    assert "GET" in response.allow


def test_only_configured_frontend_origins_receive_cors_headers(monkeypatch):
    origin = "http://localhost:9000"
    monkeypatch.setitem(web_app.app.config, "FRONTEND_ORIGINS", {origin})
    client = web_app.app.test_client()
    response = client.options("/api/students", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })

    assert response.status_code == 204
    assert response.access_control_allow_origin == origin
    assert "POST" in response.access_control_allow_methods
    assert "content-type" in response.access_control_allow_headers
    assert "Origin" in response.vary
    rejected = client.get("/api/students", headers={"Origin": "http://example.invalid"})
    assert rejected.access_control_allow_origin is None


def test_photo_api_returns_json_and_keeps_original_and_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setitem(web_app.CFG, "photos", str(tmp_path))
    monkeypatch.setattr(web_app, "CACHE_DIR", str(tmp_path / "cache"))
    student = tmp_path / "测试"
    student.mkdir()
    photo = student / "照片 1.png"
    Image.new("RGB", (500, 300)).save(photo)
    client = web_app.app.test_client()

    original = client.get("/api/media/file/测试/照片%201.png")
    assert original.status_code == 200
    assert original.json["mime_type"] == "image/png"
    assert b64decode(original.json["data"]) == photo.read_bytes()
    assert original.cache_control.no_store

    thumb = client.get("/api/media/thumb/测试/照片%201.png")
    assert thumb.status_code == 200
    assert thumb.json["mime_type"] == "image/jpeg"
    with Image.open(BytesIO(b64decode(thumb.json["data"]))) as image:
        assert image.size == (420, 252)

    assert client.get("/api/media/file/测试/missing.jpg").status_code == 404
    assert client.get("/api/media/file/../照片%201.png").status_code == 400
