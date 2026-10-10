"""FastAPI application entry point."""

import hashlib
import logging
import re
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse

from config import HOST, PORT, STATIC_DIR, API_TOKEN
from db.database import init_db, close_pool

# --------------- Logging setup ---------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy third-party loggers
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

logger = logging.getLogger("tavern")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("数据库初始化完成")
    logger.info("酒馆服务启动 — http://%s:%s", HOST, PORT)
    try:
        yield
    finally:
        try:
            from api.game_routes import _persist_sessions_on_shutdown
            await _persist_sessions_on_shutdown()
        finally:
            await close_pool()
            logger.info("酒馆服务已关闭")


app = FastAPI(title="酒馆 - AI互动文字游戏", version="1.0.0", lifespan=lifespan)

# Import and register routers
from api.game_routes import router as game_router
from api.script_routes import router as script_router
from api.save_routes import router as save_router
from api.config_routes import router as config_router
from api.search_routes import router as search_router
from api.databank_routes import router as databank_router

app.include_router(game_router, prefix="/api/game", tags=["game"])
app.include_router(script_router, prefix="/api/scripts", tags=["scripts"])
app.include_router(save_router, prefix="/api/saves", tags=["saves"])
app.include_router(config_router, prefix="/api/config", tags=["config"])
app.include_router(search_router, prefix="/api/search", tags=["search"])
app.include_router(databank_router, prefix="/api/game", tags=["databank"])

# Token authentication middleware (only active when TAVERN_API_TOKEN is set)
if API_TOKEN:
    import hmac

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path.startswith(("/api/", "/rpg/api/")):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            # S6: 使用恒定时间比较，防止时序侧信道泄露 token
            if not hmac.compare_digest(token, API_TOKEN):
                return JSONResponse(status_code=401, content={"detail": "无效的 API Token"})
        return await call_next(request)

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount RPG engine as sub-application at /rpg
try:
    from rpg.routes import app as rpg_app
    app.mount("/rpg", rpg_app)
    logger.info("RPG 推演引擎已挂载到 /rpg")
except Exception as e:
    logger.warning("RPG 引擎加载失败（可忽略）: %s", e)


@app.get("/shell")
async def shell_page():
    shell_path = STATIC_DIR / "shell.html"
    if shell_path.exists():
        return FileResponse(str(shell_path), media_type="text/html")
    return HTMLResponse("<h1>Shell not found</h1>", status_code=404)


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def version_asset(match):
        asset_url = match.group("path")
        asset_path = STATIC_DIR / asset_url.removeprefix("/static/")
        version = hashlib.sha256(asset_path.read_bytes()).hexdigest()[:12]
        return f'{match.group("prefix")}{asset_url}?v={version}{match.group("quote")}'

    html = re.sub(
        r'''(?P<prefix>\b(?:src|href)=(?P<quote>["']))(?P<path>/static/(?:css|js)/[^"'?]+)(?:\?[^"']*)?(?P=quote)''',
        version_asset,
        html,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
