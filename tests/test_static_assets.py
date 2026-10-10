import hashlib
import os
import re

import httpx
import pytest

import app as application


@pytest.fixture
def static_page(tmp_path, monkeypatch):
    for directory in ("css", "js"):
        (tmp_path / directory).mkdir()
    (tmp_path / "css/style.css").write_text("body { color: white; }", encoding="utf-8")
    (tmp_path / "js/map.js").write_text("function centerMap() {}", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="/static/css/style.css?v=old">'
        "<script src='/static/js/map.js'></script>"
        '<script src="https://example.com/external.js"></script>',
        encoding="utf-8",
    )
    monkeypatch.setattr(application, "STATIC_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_index_versions_local_assets_and_revalidates_html(static_page):
    transport = httpx.ASGITransport(app=application.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    for asset in ("css/style.css", "js/map.js"):
        version = hashlib.sha256((static_page / asset).read_bytes()).hexdigest()[:12]
        assert f"/static/{asset}?v={version}" in response.text
    assert "?v=old" not in response.text
    assert 'src="https://example.com/external.js"' in response.text


@pytest.mark.asyncio
async def test_index_only_changes_the_version_of_modified_assets(static_page):
    transport = httpx.ASGITransport(app=application.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/")
        unchanged = await client.get("/")
        assert unchanged.text == first.text

        script = static_page / "js/map.js"
        stat = script.stat()
        script.write_text("function zoomToMap() {}", encoding="utf-8")
        os.utime(script, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        changed = await client.get("/")

    assert changed.text != first.text
    assert re.search(r"/static/css/[^\"]+", changed.text).group() == re.search(
        r"/static/css/[^\"]+", first.text
    ).group()
    version = hashlib.sha256(script.read_bytes()).hexdigest()[:12]
    assert f"/static/js/map.js?v={version}" in changed.text


@pytest.mark.asyncio
async def test_shipped_index_versions_every_local_stylesheet_and_script():
    transport = httpx.ASGITransport(app=application.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assets = re.findall(r'(?:src|href)="(/static/(?:css|js)/[^\"]+)"', response.text)
    assert any("main_map_view.js" in asset for asset in assets)
    for asset in assets:
        path, separator, query = asset.partition("?")
        assert separator
        version = hashlib.sha256((application.STATIC_DIR / path.removeprefix("/static/")).read_bytes()).hexdigest()[:12]
        assert query == f"v={version}"
