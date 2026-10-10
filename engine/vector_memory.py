"""Vector-based long-term memory using FastEmbed + ChromaDB.

Optional feature: if fastembed/chromadb are not installed, the module
exports _VECTOR_AVAILABLE = False and VectorMemory becomes a no-op.
"""

import logging
import os
import re
from config import DATA_DIR

logger = logging.getLogger(__name__)

# Fix SSL certificate verification on Windows + use HuggingFace mirror
if not os.environ.get("SSL_CERT_FILE") or not os.environ.get("REQUESTS_CA_BUNDLE"):
    try:
        import certifi
        _ca = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", _ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    except ImportError:
        pass
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

try:
    from fastembed import TextEmbedding
    import chromadb
    _VECTOR_AVAILABLE = True
except ImportError:
    _VECTOR_AVAILABLE = False

_embed_model = None
_chroma_client = None

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[。！？…\n.!?])\s*')


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        _embed_model = TextEmbedding("BAAI/bge-small-zh-v1.5")
    return _embed_model


def _get_chroma():
    global _chroma_client
    if _chroma_client is None:
        db_path = DATA_DIR / "vector_db"
        db_path.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(db_path))
    return _chroma_client


def _split_sentences(text: str, min_len: int = 20, max_len: int = 300) -> list[str]:
    """Split text into sentence-level chunks for embedding."""
    raw = _SENTENCE_SPLIT_RE.split(text)
    chunks = []
    buf = ""
    for seg in raw:
        seg = seg.strip()
        if not seg:
            continue
        if buf and len(buf) + len(seg) > max_len:
            if len(buf) >= min_len:
                chunks.append(buf)
            buf = seg
        else:
            buf = (buf + seg) if buf else seg
    if buf and len(buf) >= min_len:
        chunks.append(buf)
    return chunks


class VectorMemory:
    """Per-game vector store for semantic retrieval of past turns."""

    def __init__(self, save_id: str):
        self.collection_name = f"game_{save_id[:32]}"
        self._collection = None
        self._legacy_checked_nodes = set()

    def _get_collection(self):
        if self._collection is None:
            client = _get_chroma()
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def add(self, node_id: str, text: str, metadata: dict, embed_text: str = ""):
        """Embed and store a turn's text using sentence-level chunking.

        Each sentence chunk gets its own embedding for precise retrieval,
        but the full text is stored in documents for complete context on recall.
        If embed_text is provided, it is used for embedding instead of the raw text
        (summarize-before-embed pattern).
        """
        if not text or len(text) < 20:
            return
        try:
            model = _get_embed_model()
            source_for_embed = embed_text if embed_text else text
            chunks = _split_sentences(source_for_embed)
            if not chunks:
                chunks = [source_for_embed[:300]]
            embeddings = list(model.embed(chunks))
            col = self._get_collection()
            ids = [f"{node_id}_c{i}" for i in range(len(chunks))]
            parent_node = metadata.get("parent_node", node_id)
            # Remove stale chunks from previous writes (e.g. after regenerate)
            try:
                old_ids = col.get(where={"parent_node": parent_node})["ids"]
                if parent_node != node_id:
                    old_ids += col.get(where={"parent_node": node_id})["ids"]
                stale = [old_id for old_id in old_ids
                         if old_id.startswith(f"{node_id}_c") and old_id not in ids]
                if stale:
                    col.delete(ids=stale)
            except Exception:
                pass
            base_meta = {**metadata, "parent_node": parent_node}
            base_meta.setdefault("doc_type", "turn")
            metadatas = [{**base_meta, "chunk_index": i} for i in range(len(chunks))]
            documents = [text] * len(chunks)
            col.upsert(
                ids=ids,
                embeddings=[e.tolist() for e in embeddings],
                documents=documents,
                metadatas=metadatas,
            )
        except Exception as e:
            logger.warning("向量写入失败: %s", e)

    def add_lorebook(self, entry_id: str, text: str, metadata: dict):
        """Index a single lorebook entry into the vector store (no chunking)."""
        if not text or len(text) < 20:
            return
        try:
            model = _get_embed_model()
            embeddings = list(model.embed([text[:500]]))
            self._get_collection().upsert(
                ids=[f"lore_{entry_id}"],
                embeddings=[embeddings[0].tolist()],
                documents=[text],
                metadatas=[{**metadata, "doc_type": "lorebook", "entry_id": entry_id}],
            )
        except Exception as e:
            logger.warning("lorebook向量写入失败: %s", e)

    def add_lorebook_batch(self, entries: list[tuple[str, str, dict]]):
        """Batch index multiple lorebook entries. Each tuple: (entry_id, text, metadata)."""
        if not entries:
            return
        valid = [(eid, t, m) for eid, t, m in entries if t and len(t) >= 20]
        if not valid:
            return
        try:
            model = _get_embed_model()
            texts = [t for _, t, _ in valid]
            all_embeddings = list(model.embed(texts))
            ids = [f"lore_{eid}" for eid, _, _ in valid]
            metadatas = [{**m, "doc_type": "lorebook", "entry_id": eid} for eid, _, m in valid]
            self._get_collection().upsert(
                ids=ids,
                embeddings=[e.tolist() for e in all_embeddings],
                documents=texts,
                metadatas=metadatas,
            )
        except Exception as e:
            logger.warning("lorebook批量向量写入失败: %s", e)

    def remove_lorebook(self, entry_id: str):
        """Remove a lorebook entry from the vector store."""
        try:
            self._get_collection().delete(ids=[f"lore_{entry_id}"])
        except Exception:
            pass

    def _migrate_legacy_branch_metadata(self, col, node_ids: set[str]):
        pending = node_ids - self._legacy_checked_nodes
        if not pending or not hasattr(col, "update"):
            return
        try:
            records = col.get(
                where={"doc_type": {"$in": ["event", "milestone"]}},
                include=["metadatas"],
            )
            update_ids = []
            update_metadatas = []
            milestone_nodes = sorted(pending, key=len, reverse=True)
            for entry_id, metadata in zip(records["ids"], records["metadatas"]):
                parent = metadata.get("parent_node", "")
                if not entry_id.startswith(f"{parent}_c"):
                    continue
                if metadata.get("doc_type") == "event":
                    node_id = parent.removeprefix("event_") if parent.startswith("event_") else None
                else:
                    node_id = next((node for node in milestone_nodes
                                    if parent.startswith(f"ms_{node}_")), None)
                if node_id in pending:
                    update_ids.append(entry_id)
                    update_metadatas.append({**metadata, "parent_node": node_id})
            if update_ids:
                col.update(ids=update_ids, metadatas=update_metadatas)
            self._legacy_checked_nodes.update(pending)
        except Exception as exc:
            logger.warning("旧分支向量元数据迁移失败: %s", exc)

    def query(self, query_text: str, top_k: int = 3, exclude_turns: list | None = None,
              doc_type: str | None = None, allowed_node_ids: set[str] | None = None) -> list[dict]:
        """Retrieve most relevant documents by semantic similarity.

        Splits query_text into sentences and queries each separately for better
        sentence-to-sentence matching, then merges results by distance.
        Returns dicts with keys: turn, text, game_time, doc_type, entry_id, comment.
        Deduplicates turn chunks by parent_node — returns full turn text on first hit.
        """
        if not query_text:
            return []
        try:
            col = self._get_collection()
            if col.count() == 0:
                return []
            if allowed_node_ids and doc_type in (None, "event", "milestone"):
                self._migrate_legacy_branch_metadata(col, allowed_node_ids)
            model = _get_embed_model()
            sentences = _split_sentences(query_text, min_len=10)
            if not sentences:
                sentences = [query_text]
            q_embs = list(model.embed(sentences))
            n_results = min(top_k * 3 + len(exclude_turns or []) + 5, col.count())
            where_filter = {"doc_type": doc_type} if doc_type else None
            if allowed_node_ids is not None and doc_type != "lorebook":
                node_ids = sorted(allowed_node_ids)
                if doc_type:
                    if not node_ids:
                        return []
                    where_filter = {"$and": [
                        {"doc_type": doc_type}, {"parent_node": {"$in": node_ids}},
                    ]}
                elif node_ids:
                    where_filter = {"$or": [
                        {"doc_type": "lorebook"},
                        {"$and": [{"doc_type": "turn"}, {"parent_node": {"$in": node_ids}}]},
                        {"$and": [{"doc_type": "event"}, {"parent_node": {"$in": node_ids}}]},
                        {"$and": [{"doc_type": "milestone"}, {"parent_node": {"$in": node_ids}}]},
                    ]}
                else:
                    where_filter = {"doc_type": "lorebook"}
            results = col.query(
                query_embeddings=[e.tolist() for e in q_embs],
                n_results=n_results,
                where=where_filter,
            )
            # Merge results from all sentence queries by best distance
            hit_map: dict[str, tuple[float, str, dict]] = {}
            for qi in range(len(sentences)):
                docs = results["documents"][qi]
                metas = results["metadatas"][qi]
                dists = results["distances"][qi] if results.get("distances") else [0.0] * len(docs)
                for i, doc in enumerate(docs):
                    meta = metas[i]
                    chunk_id = (meta.get("doc_type", "turn"),
                                meta.get("parent_node", "") or meta.get("entry_id", i),
                                meta.get("node_id", ""))
                    dist = dists[i]
                    if chunk_id not in hit_map or dist < hit_map[chunk_id][0]:
                        hit_map[chunk_id] = (dist, doc, metas[i])
            sorted_hits = sorted(hit_map.values(), key=lambda x: x[0])
            entries = []
            seen_nodes = set()
            for dist, doc, meta in sorted_hits:
                doc_type = meta.get("doc_type", "turn")
                turn = meta.get("turn_number", 0)
                if doc_type == "turn" and exclude_turns and turn in exclude_turns:
                    continue
                parent_node = meta.get("parent_node", "")
                if doc_type == "turn" and parent_node:
                    if parent_node in seen_nodes:
                        continue
                    seen_nodes.add(parent_node)
                entries.append({
                    "turn": turn,
                    "text": doc,
                    "game_time": meta.get("game_time", ""),
                    "doc_type": doc_type,
                    "entry_id": meta.get("entry_id", ""),
                    "comment": meta.get("comment", ""),
                    "active_lore_ids": meta.get("active_lore_ids", ""),
                    "score": max(0.0, 1.0 - dist),
                })
                if len(entries) >= top_k:
                    break
            return entries
        except Exception as e:
            logger.warning("向量检索失败: %s", e)
            return []

    def delete_collection(self):
        """Clean up when a game save is deleted."""
        try:
            _get_chroma().delete_collection(self.collection_name)
        except Exception:
            pass
