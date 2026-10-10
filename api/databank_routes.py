"""Data Bank API routes: file upload, list, delete for per-game document RAG."""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from api.game_routes import _sessions
from engine.data_bank import DataBank
from engine.vector_memory import _VECTOR_AVAILABLE
from search.web_scraper import WebScraper

logger = logging.getLogger(__name__)
router = APIRouter(tags=["databank"])

# Per-session data banks (keyed by save_id)
_data_banks: dict[str, DataBank] = {}


def _get_data_bank(save_id: str) -> DataBank:
    session = _sessions.get(save_id)
    if session is not None and session.data_bank is not None:
        return session.data_bank
    if save_id not in _data_banks:
        _data_banks[save_id] = DataBank(save_id)
    return _data_banks[save_id]


@router.post("/{save_id}/databank/upload")
async def upload_databank_file(save_id: str, file: UploadFile = File(...)):
    """Upload a document to the game's Data Bank for RAG retrieval."""
    if not _VECTOR_AVAILABLE:
        raise HTTPException(status_code=503, detail="Vector features unavailable (install fastembed + chromadb)")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk")
        except Exception:
            raise HTTPException(status_code=400, detail="Unable to decode file (try UTF-8 or GBK)")

    if not text.strip():
        raise HTTPException(status_code=400, detail="File is empty")
    if len(text) > 500_000:
        raise HTTPException(status_code=400, detail="File too large (max 500KB text)")

    bank = _get_data_bank(save_id)
    meta = await asyncio.to_thread(bank.ingest, file.filename or "unknown.txt", text)
    logger.info("Data Bank 上传: save=%s, file=%s, chunks=%d", save_id, file.filename, meta["chunks"])
    return {"status": "ok", "file": meta}


@router.get("/{save_id}/databank")
async def list_databank_files(save_id: str):
    """List uploaded files in the Data Bank."""
    bank = _get_data_bank(save_id)
    return {"files": await asyncio.to_thread(bank.list_files)}


@router.delete("/{save_id}/databank/{file_id}")
async def delete_databank_file(save_id: str, file_id: str):
    """Delete a file from the Data Bank."""
    bank = _get_data_bank(save_id)
    await asyncio.to_thread(bank.delete_file, file_id)
    return {"status": "ok"}


class ScrapeRequest(BaseModel):
    url: str


@router.post("/{save_id}/databank/scrape")
async def scrape_to_databank(save_id: str, req: ScrapeRequest):
    """Scrape a URL and ingest its content into the Data Bank."""
    if not _VECTOR_AVAILABLE:
        raise HTTPException(status_code=503, detail="Vector features unavailable (install fastembed + chromadb)")

    result = await WebScraper.scrape(req.url)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "抓取失败"))

    text = result["content"]
    if not text.strip():
        raise HTTPException(status_code=400, detail="网页内容为空")
    if len(text) > 500_000:
        text = text[:500_000]

    title = result.get("title") or req.url
    bank = _get_data_bank(save_id)
    meta = await asyncio.to_thread(bank.ingest, title, text)
    logger.info("Data Bank URL抓取: save=%s, url=%s, chunks=%d", save_id, req.url, meta["chunks"])
    return {"status": "ok", "file": meta}
