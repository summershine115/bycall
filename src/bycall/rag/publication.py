"""索引发布登记：SQL 决定哪一代向量可以在召回前进入候选集。"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import String, cast
from sqlmodel import Session, select

from bycall.governance.models import Document, SystemSetting

INDEX_PREFIX = "_rag.index."


def index_record(session: Session, doc_id: int) -> SystemSetting | None:
    return session.exec(SystemSetting.select_by_key(f"{INDEX_PREFIX}{doc_id}")).first()


def read_manifest(session: Session, doc_id: int) -> dict:
    row = index_record(session, doc_id)
    if row is None:
        return {"active": f"doc_{doc_id}", "generations": [f"doc_{doc_id}"]}
    return json.loads(row.value)


def stage_generation(session: Session, doc: Document) -> str:
    """先持久化待写代；中断留下的向量也不会被误认为旧索引。"""
    manifest = read_manifest(session, doc.id)
    generation = f"doc_{doc.id}_g_{uuid.uuid4().hex}"
    manifest["generations"].append(generation)
    row = index_record(session, doc.id) or SystemSetting(key=f"{INDEX_PREFIX}{doc.id}")
    row.value = json.dumps(manifest)
    session.add(row)
    session.commit()
    return generation


def activate_generation(session: Session, doc_id: int, generation: str) -> None:
    """只加入当前事务，调用方与文档状态、统计一起提交。"""
    row = index_record(session, doc_id)
    manifest = json.loads(row.value)
    manifest["active"] = generation
    row.value = json.dumps(manifest)
    session.add(row)


def visible_generations(
    session: Session, kb_id: int, tags: list[str], level: int, is_admin: bool
) -> list[str]:
    """当前 SQL ACL 先筛文档，再以 generation 白名单预过滤向量查询。"""
    # 仅取 ID 与发布记录；关联键已有唯一索引，避免逐文档查询和读取正文。
    stmt = (
        select(Document.id, SystemSetting.value)
        .outerjoin(SystemSetting, SystemSetting.key == INDEX_PREFIX + cast(Document.id, String))
        .where(Document.knowledge_base_id == kb_id, Document.status == "completed")
        .order_by(Document.id.desc())
    )
    if not is_admin:
        stmt = stmt.where(Document.level <= level)
        if "*" not in tags:
            stmt = stmt.where(Document.source.in_(tags))
    allowed = []
    for doc_id, value in session.exec(stmt):
        active = json.loads(value).get("active") if value is not None else f"doc_{doc_id}"
        if active:
            allowed.append(active)
    return allowed
