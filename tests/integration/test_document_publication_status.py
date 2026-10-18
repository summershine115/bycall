"""控制台检索状态必须与发布门一致，不能把入库状态直接当成可检索。"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.models import Document, KnowledgeBase, SystemSetting


def test_document_retrievability_matches_publication_gate(tmp_path, monkeypatch):
    dsn = f"sqlite:///{tmp_path / 'publication.db'}"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", dsn)
    get_settings.cache_clear()
    set_engine(create_engine(dsn, connect_args={"check_same_thread": False}))
    init_db()
    with session_scope() as session:
        kb = KnowledgeBase(name="发布状态", embedding_model_id=1, collection_name="status_test")
        session.add(kb)
        session.flush()
        kb_id = kb.id
        expected = {}
        for title, status, manifest_active, retrievable in [
            ("兼容旧索引", "completed", "legacy", True),
            ("新发布代", "completed", "generation_1", True),
            ("未发布", "completed", None, False),
            ("失败但保留旧代", "failed", "generation_old", False),
            ("处理中", "processing", "generation_old", False),
        ]:
            doc = Document(knowledge_base_id=kb_id, title=title, source="public", status=status)
            session.add(doc)
            session.flush()
            if manifest_active != "legacy":
                session.add(
                    SystemSetting(
                        key=f"_rag.index.{doc.id}",
                        value=json.dumps({"active": manifest_active, "generations": []}),
                    )
                )
            expected[title] = retrievable
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents")
        assert response.status_code == 200
        assert {row["title"]: row["is_retrievable"] for row in response.json()} == expected
