"""Data Bank: document upload, chunking, and vector retrieval for game context enrichment."""

import logging
import uuid
from pathlib import Path

from engine.vector_memory import VectorMemory, _VECTOR_AVAILABLE, _split_sentences, _get_embed_model, _get_chroma

logger = logging.getLogger(__name__)


class DataBank:
    """Per-game document store with chunked vector retrieval."""

    def __init__(self, save_id: str, *, known_empty: bool = False):
        self.save_id = save_id
        self.collection_name = f"databank_{save_id[:32]}"
        self._collection = None
        self._known_empty = known_empty

    def _get_collection(self):
        if not _VECTOR_AVAILABLE:
            return None
        if self._collection is None:
            client = _get_chroma()
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def ingest(self, filename: str, content: str, chunk_size: int = 800) -> dict:
        """Split content into chunks, embed, and store. Returns file metadata."""
        file_id = str(uuid.uuid4())[:8]
        chunks = self._split_chunks(content, chunk_size)
        if not chunks:
            chunks = [content[:1000]]

        col = self._get_collection()
        if not col:
            raise RuntimeError("Data Bank 向量存储不可用")
        try:
            model = _get_embed_model()
            embeddings = list(model.embed(chunks))
            ids = [f"{file_id}_c{i}" for i in range(len(chunks))]
            metadatas = [{"file_id": file_id, "filename": filename, "chunk_index": i, "doc_type": "databank", "chars": len(content)} for i in range(len(chunks))]
            col.upsert(
                ids=ids,
                embeddings=[e.tolist() for e in embeddings],
                documents=chunks,
                metadatas=metadatas,
            )
            self._known_empty = False
        except Exception as e:
            logger.warning("Data Bank 向量写入失败: %s", e)
            raise

        file_meta = {
            "id": file_id,
            "filename": filename,
            "chunks": len(chunks),
            "chars": len(content),
        }
        return file_meta

    def delete_file(self, file_id: str) -> bool:
        """Remove all chunks for a file from the vector store."""
        col = self._get_collection()
        if col:
            try:
                results = col.get(where={"file_id": file_id})
                if results and results["ids"]:
                    col.delete(ids=results["ids"])
            except Exception as e:
                logger.warning("Data Bank 删除失败: %s", e)
                raise
        return True

    def query(self, query_text: str, top_k: int = 3) -> list[dict]:
        """Retrieve relevant chunks from uploaded documents."""
        if self._known_empty:
            return []
        col = self._get_collection()
        if not col:
            return []
        try:
            if col.count() == 0:
                return []
            model = _get_embed_model()
            embedding = list(model.embed([query_text]))[0]
            results = col.query(
                query_embeddings=[embedding.tolist()],
                n_results=top_k,
            )
            if not results or not results["documents"]:
                return []
            docs = results["documents"][0]
            metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(docs)
            dists = results["distances"][0] if results.get("distances") else [0.0] * len(docs)
            return [
                {"text": doc, "filename": meta.get("filename", ""), "distance": dist}
                for doc, meta, dist in zip(docs, metas, dists)
                if dist < 0.8
            ]
        except Exception as e:
            logger.warning("Data Bank 查询失败: %s", e)
            return []

    def list_files(self) -> list[dict]:
        col = self._get_collection()
        if not col:
            return []
        files = {}
        for meta in col.get(include=["metadatas"])["metadatas"] or []:
            if not meta or not meta.get("file_id"):
                continue
            file_id = meta["file_id"]
            if file_id not in files:
                files[file_id] = {
                    "id": file_id, "filename": meta.get("filename", ""),
                    "chunks": 0, "chars": meta.get("chars"),
                }
            files[file_id]["chunks"] += 1
        return list(files.values())

    @staticmethod
    def _split_chunks(text: str, chunk_size: int = 800) -> list[str]:
        """Split text into overlapping chunks."""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []
        chunks = []
        overlap = min(100, chunk_size // 4)
        i = 0
        while i < len(text):
            end = i + chunk_size
            chunk = text[i:end].strip()
            if chunk:
                chunks.append(chunk)
            i = end - overlap
        return chunks
