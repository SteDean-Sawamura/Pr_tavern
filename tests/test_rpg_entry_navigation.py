import httpx
import pytest

from app import app


@pytest.mark.asyncio
async def test_main_and_legacy_rpg_entries_link_and_explain_save_boundary():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        main = await client.get("/")
        legacy = await client.get("/rpg/")

    assert main.status_code == 200
    assert 'href="/rpg/"' in main.text
    assert "旧版 RPG 存档与主游戏不互通" in main.text
    assert legacy.status_code == 200
    assert 'href="/"' in legacy.text
    assert "旧存档不会自动迁移" in legacy.text
