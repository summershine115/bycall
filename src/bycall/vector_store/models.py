"""VectorStore 共享数据模型。

后端无关的 dataclass。设计要点：
- ``Chunk`` 携带 ACL 元数据（``acl``），权限预过滤（M2）直接把它翻译成 filter。
- ``SearchRequest`` 一次可同时给 vector + full_text + filter，对齐 zvec PoC step2
  验证的「向量 + FTS + 标量 filter 三合一」混合检索形态，也兼容 Qdrant/pgvector。
- filter 用结构化条件，不直接透传后端 DSL 字符串，避免换后端时业务层重写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ============ ACL / 权限 ============


@dataclass
class ACLMetadata:
    """每个 chunk 附的权限元数据（pre-filtering 用，见 HANDOVER §4.1）。

    召回阶段在向量检索 *之前* 把这些字段拼成 filter，reranker 只见授权候选。
    ACL 提取失败时不应构造此对象 → 调用方按"默认隔离"处理（不可搜索）。
    """

    tenant_id: str
    dept: str | None = None
    level: int | None = None  # 职级，越小越高或反之，由业务约定
    acl_users: list[str] = field(default_factory=list)  # 显式授权用户
    acl_groups: list[str] = field(default_factory=list)  # 显式授权用户组
    sensitivity_label: str | None = None  # public / internal / confidential ...


# ============ Collection 定义 ============


@dataclass
class FieldDef:
    """标量字段定义。``index``=True 时建相应索引（如 FTS 倒排 / 标量索引）。"""

    name: str
    type: Literal["string", "int64", "float", "bool"]
    nullable: bool = True
    # True → 该字符串字段建全文索引（FTS），用于混合检索的 FTS 路。
    fts: bool = False


@dataclass
class CollectionDef:
    """建 collection 的 schema 定义，后端无关。"""

    name: str
    fields: list[FieldDef]
    vector_field: str = "embedding"  # 向量字段名
    dimension: int = 1536


# ============ 写入 ============


@dataclass
class Chunk:
    """一个知识块 —— RAG 的最小检索单位（参考 WeKnora Chunk 模型）。

    ``id`` 全局唯一（建议 ``{doc_id}_{chunk_idx}``）。
    ``vector`` 由 embedding 模型产出，维度需与 ``CollectionDef.dimension`` 对齐。
    ``fields`` 存放除正文外的标量（source/year/部门等），用于 filter 与回显。
    ``acl`` 为 None 表示未授权 → 默认不可搜索（隔离）。

    父子块召回（M2）：
    - ``chunk_type="child"``（默认）：参与向量索引 + 检索。
    - ``chunk_type="parent"``：仅存 fields（不建向量索引），子块命中后带回提供上下文。
    - ``parent_id``：子块指向父块的 id。null → 无父块（普通 chunk 或父块本身）。
    - ``chunk_index``：在同一文档内的序号（用于排序 + 邻居上下文扩展）。
    """

    id: str
    text: str  # 正文（同时会作为 FTS 字段内容）
    vector: list[float]
    fields: dict[str, object] = field(default_factory=dict)
    acl: ACLMetadata | None = None
    parent_id: str | None = None  # 父块 id（父子块：child→parent）
    chunk_type: str = "child"  # "child"（默认，参与索引）| "parent"（仅上下文）
    chunk_index: int = 0  # 文档内序号


@dataclass
class InsertResult:
    """单条写入结果。后端各异，统一成 ok/id/message。"""

    id: str
    ok: bool
    message: str = ""


# ============ 检索 ============


@dataclass
class FilterCondition:
    """结构化过滤条件，翻译成各后端的 DSL（zvec 用单等号字符串，Qdrant 用 payload filter）。

    M0 只实现 ``match``（等值）与 ``in_``（集合包含）两种，覆盖 PoC step2 的
    ``source = 'wiki'`` 与未来 ACL 过滤的 ``dept IN (...)``。其余操作符按需扩展。
    """

    field: str
    op: Literal["match", "in"]
    value: str | int | float | bool | None = None  # op=="match" 时用
    values: list[str] | None = None  # op == "in" 时用

    def matches(self, candidate: object) -> bool:
        """Python 侧本地过滤（测试/降级用，生产走后端 filter）。"""
        actual = getattr(candidate, self.field, None)
        if self.op == "match":
            return actual == self.value
        if self.op == "in":
            return actual in (self.values or [])
        return False


@dataclass
class SearchRequest:
    """混合检索请求。任意组合 vector / full_text / filters；都给则三合一。"""

    # 向量路：与 collection 的向量字段做相似度。None 表示不走向量路。
    vector: list[float] | None = None
    # 全文路：关键词匹配（走 FTS 字段）。None 表示不走 FTS。
    full_text: str | None = None
    # 过滤：在召回前/中应用（pre-filtering）。多条件 AND。
    filters: list[FilterCondition] = field(default_factory=list)
    topk: int = 5
    # 要回显的标量字段名；None 表示回显全部。
    output_fields: list[str] | None = None


@dataclass
class SearchResult:
    """单条检索结果。``score`` 为融合后的综合得分。"""

    id: str
    score: float
    text: str = ""
    fields: dict[str, object] = field(default_factory=dict)
