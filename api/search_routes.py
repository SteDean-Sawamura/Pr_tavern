"""Search and material management API routes."""

import logging
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional

from search.search_engine import SearchEngine
from search.material_manager import MaterialManager

router = APIRouter()
logger = logging.getLogger("tavern.search")


async def _get_ai_provider():
    """Get the configured AI provider for search features."""
    from api.config_routes import get_ai_provider_instance
    return await get_ai_provider_instance()


class SearchRequest(BaseModel):
    query: str
    sources: Optional[list[str]] = None
    scrape_url: Optional[str] = None
    scrape_urls: Optional[list[str]] = None  # multi-URL scraping
    max_results: Optional[int] = 5  # web search result count


class SaveMaterialRequest(BaseModel):
    title: str
    content: str
    url: Optional[str] = None
    source_type: str = "manual"
    search_query: Optional[str] = None


class AddTagsRequest(BaseModel):
    tags: list[str]


class InjectRequest(BaseModel):
    material_id: int
    script_id: str
    target: str  # 'background', 'location', 'npc', 'event', 'lorebook'


class TransformRequest(BaseModel):
    material_id: int
    target: str  # 'background', 'location', 'npc', 'event', 'lorebook'
    script: Optional[dict] = None  # 完整剧本对象，同 ai-generate/tab


class MaterialSearchRequest(BaseModel):
    query: Optional[str] = None
    tags: Optional[list[str]] = None


@router.post("/web")
async def web_search(req: SearchRequest):
    """Perform multi-source search."""
    ai_provider = await _get_ai_provider()
    engine = SearchEngine(ai_provider)

    # Merge single scrape_url into scrape_urls list
    urls = list(req.scrape_urls or [])
    if req.scrape_url and req.scrape_url not in urls:
        urls.append(req.scrape_url)

    logger.info("网页搜索 — query=%s, sources=%s, urls=%d",
                req.query[:60], req.sources, len(urls))
    results = await engine.search(
        query=req.query,
        sources=req.sources,
        scrape_urls=urls or None,
        max_results=req.max_results or 5,
    )
    logger.info("搜索完成 — 结果数=%d", len(results))
    return {"results": results, "count": len(results)}


@router.post("/materials")
async def save_material(req: SaveMaterialRequest):
    """Save a search result as a material."""
    ai_provider = await _get_ai_provider()
    manager = MaterialManager(ai_provider)
    material_id = await manager.save_material(req.model_dump())
    material = await manager.get_material(material_id)
    logger.info("保存素材 — id=%d, title=%s, source=%s", material_id, req.title[:40], req.source_type)
    return {"id": material_id, "material": material}


@router.get("/materials")
async def list_materials(query: Optional[str] = None, tags: Optional[str] = None):
    """List materials, optionally filtered by query or tags."""
    tag_list = tags.split(",") if tags else None
    manager = MaterialManager()
    materials = await manager.list_materials(query=query, tags=tag_list)
    return {"materials": materials, "count": len(materials)}


@router.get("/materials/tags")
async def list_tags():
    """Get all unique tags."""
    manager = MaterialManager()
    tags = await manager.get_all_tags()
    return {"tags": tags}


@router.get("/materials/{material_id}")
async def get_material(material_id: int):
    manager = MaterialManager()
    material = await manager.get_material(material_id)
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    return material


@router.delete("/materials/{material_id}")
async def delete_material(material_id: int):
    manager = MaterialManager()
    await manager.delete_material(material_id)
    return {"status": "ok"}


@router.post("/materials/{material_id}/tags")
async def add_tags(material_id: int, req: AddTagsRequest):
    manager = MaterialManager()
    await manager.add_tags(material_id, req.tags)
    return {"status": "ok"}


@router.delete("/materials/{material_id}/tags/{tag}")
async def remove_tag(material_id: int, tag: str):
    manager = MaterialManager()
    await manager.remove_tag(material_id, tag)
    return {"status": "ok"}


@router.post("/materials/search")
async def search_materials(req: MaterialSearchRequest):
    """Search within saved materials."""
    manager = MaterialManager()
    materials = await manager.list_materials(query=req.query, tags=req.tags)
    return {"materials": materials, "count": len(materials)}


@router.post("/inject")
async def inject_material(req: InjectRequest):
    """Inject a material into a script."""
    logger.info("注入素材 — material_id=%d, script_id=%s, target=%s",
                req.material_id, req.script_id, req.target)
    ai_provider = await _get_ai_provider()
    manager = MaterialManager(ai_provider)
    try:
        updated_script = await manager.inject_to_script(
            req.material_id, req.script_id, req.target
        )
        return {"status": "ok", "script": updated_script}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/transform")
async def transform_material(req: TransformRequest):
    """Transform material content into a script element (without saving to DB)."""
    logger.info("转化素材 — material_id=%d, target=%s", req.material_id, req.target)
    ai_provider = await _get_ai_provider()
    if not ai_provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")
    manager = MaterialManager(ai_provider)
    material = await manager.get_material(req.material_id)
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    from search.ai_knowledge import AIKnowledge
    ai_knowledge = AIKnowledge(ai_provider)

    script_summary = ""
    if req.script:
        from api.script_routes import _build_full_script_summary
        _target_tab_map = {
            "npc": "characters", "location": "world", "event": "events",
            "lorebook": "lorebook", "organization": "organizations",
            "background": "world", "faction": "organizations",
            "random_item": "dice",
        }
        script_summary = _build_full_script_summary(
            req.script, focus_tab=_target_tab_map.get(req.target)
        )

    transformed = await ai_knowledge.transform_to_script_element(
        material["content"], req.target, script_summary=script_summary
    )
    return {"transformed": transformed, "target": req.target}


@router.post("/materials/upload")
async def upload_material(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
):
    """Import material from uploaded file (txt, md, doc/docx)."""
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in ("txt", "md", "doc", "docx"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: .{ext}，仅支持 txt、md、doc/docx",
        )

    raw = await file.read()

    if ext in ("doc", "docx"):
        try:
            import io
            from docx import Document

            doc = Document(io.BytesIO(raw))
            content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"解析 doc 文件失败: {e}")
    else:
        for encoding in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
            try:
                content = raw.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            raise HTTPException(status_code=400, detail="无法识别文件编码")

    if not content.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")

    material_title = title or filename.rsplit(".", 1)[0] or "导入素材"

    ai_provider = await _get_ai_provider()
    manager = MaterialManager(ai_provider)
    material_id = await manager.save_material({
        "title": material_title,
        "content": content.strip(),
        "source_type": f"file:{ext}",
    })
    material = await manager.get_material(material_id)
    logger.info("文件导入素材 — id=%d, file=%s, size=%d", material_id, filename, len(content))
    return {"id": material_id, "material": material}
