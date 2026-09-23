"""数字员工头像：本地图片上传（2026-09-23 起不再手贴外链 URL）。

覆盖：上传落盘 + 站内路径回吐 + 端点可读取、类型/大小**按文件内容**校验、
重复上传覆盖旧图、移除清干净、控制面鉴权、存量外链兼容。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine

# 只测文件头（后端按魔数识别），不要求图片可真正解码。
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# 伪装成图片的非图片（声明 image/png 也没用，内容说了算）
FAKE = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """完整 app（含会话守卫）+ 临时 SQLite + 临时头像目录。"""
    db_path = tmp_path / "avatar.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_AVATAR_DIR", str(tmp_path / "avatars"))
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    from bycall.api.app import create_app

    return TestClient(create_app())


def _login(c: TestClient) -> None:
    assert c.post("/api/login", data={"name": "admin", "password": "admin123"}).status_code == 200


def _create_agent(c: TestClient) -> int:
    resp = c.post("/api/admin/agents", json={"display_name": "销售助手"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload(c: TestClient, agent_id: int, raw: bytes, ctype: str = "image/png"):
    return c.post(
        f"/api/admin/agents/{agent_id}/avatar",
        files={"file": ("avatar.png", raw, ctype)},
    )


def _stored(agent_id: int) -> list[Path]:
    """该实例当前落盘的头像文件。"""
    return sorted(Path(get_settings().avatar_dir).glob(f"agent_{agent_id}.*"))


def test_upload_saves_local_file_and_serves_it(client: TestClient):
    """上传 → 写本地文件 + avatar_url 变站内路径，端点原样吐回图片字节。"""
    _login(client)
    agent_id = _create_agent(client)

    resp = _upload(client, agent_id, PNG)
    assert resp.status_code == 201, resp.text
    url = resp.json()["avatar_url"]
    assert url.startswith(f"/api/admin/agents/{agent_id}/avatar?v=")  # 站内路径，不是外链

    files = _stored(agent_id)
    assert len(files) == 1 and files[0].read_bytes() == PNG  # 落在配置的本地目录

    got = client.get(url.split("?")[0])
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/png")
    assert got.content == PNG


def test_upload_rejects_non_image_regardless_of_declared_type(client: TestClient):
    """类型按文件头判定：声明 image/png 的 SVG 也被拒，avatar_url 不被污染。"""
    _login(client)
    agent_id = _create_agent(client)

    resp = _upload(client, agent_id, FAKE, ctype="image/png")
    assert resp.status_code == 400
    assert client.get(f"/api/admin/agents/{agent_id}").json()["avatar_url"] == ""
    assert _stored(agent_id) == []


def test_upload_rejects_oversize(client: TestClient):
    """超过 2MB → 413，不落盘。"""
    _login(client)
    agent_id = _create_agent(client)

    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024)
    resp = _upload(client, agent_id, big)
    assert resp.status_code == 413
    assert _stored(agent_id) == []


def test_reupload_replaces_then_delete_clears(client: TestClient):
    """重复上传覆盖（不留两张图）；移除头像 → 路径清空 + 文件删除 + 端点 404。"""
    _login(client)
    agent_id = _create_agent(client)

    first = _upload(client, agent_id, PNG).json()["avatar_url"]
    second = _upload(client, agent_id, JPG, ctype="image/jpeg").json()["avatar_url"]
    assert first != second  # 内容哈希不同 → URL 不同，浏览器缓存不会串

    files = _stored(agent_id)
    assert len(files) == 1 and files[0].suffix == ".jpg"
    assert client.get(second.split("?")[0]).content == JPG

    assert client.delete(f"/api/admin/agents/{agent_id}/avatar").status_code == 204
    assert client.get(f"/api/admin/agents/{agent_id}").json()["avatar_url"] == ""
    assert client.get(f"/api/admin/agents/{agent_id}/avatar").status_code == 404
    assert _stored(agent_id) == []


def test_avatar_endpoints_require_admin_session(client: TestClient):
    """未登录的上传/读取/移除一律 401——头像不绕过控制面守卫。"""
    _login(client)
    agent_id = _create_agent(client)
    client.post("/api/logout")

    assert _upload(client, agent_id, PNG).status_code == 401
    assert client.get(f"/api/admin/agents/{agent_id}/avatar").status_code == 401
    assert client.delete(f"/api/admin/agents/{agent_id}/avatar").status_code == 401


def test_legacy_external_url_still_renderable_and_clearable(client: TestClient):
    """存量外链不迁移、不破坏：原值照常回显；站内端点对它 404；移除一并清掉。"""
    _login(client)
    agent_id = _create_agent(client)
    legacy = "https://cdn.example.com/a.png"

    assert (
        client.put(f"/api/admin/agents/{agent_id}", json={"avatar_url": legacy}).status_code == 200
    )
    assert client.get(f"/api/admin/agents/{agent_id}").json()["avatar_url"] == legacy
    assert client.get(f"/api/admin/agents/{agent_id}/avatar").status_code == 404  # 本地无文件

    assert client.delete(f"/api/admin/agents/{agent_id}/avatar").status_code == 204
    assert client.get(f"/api/admin/agents/{agent_id}").json()["avatar_url"] == ""
