"""SQLModel/SQLAlchemy 的 pydantic 模型 JSON 列类型。

问题：SQLAlchemy 原生 ``JSON`` 列用 ``json.dumps``，无法序列化 pydantic BaseModel。
解决：``PydanticJSONType`` 把 pydantic 对象 ↔ JSON 字符串互转，存进 DB 的是字符串。

用法：
    config: RoleConfig = Field(
        default_factory=RoleConfig,
        sa_column=Column("config", PydanticJSONType(RoleConfig)),
    )
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import String, TypeDecorator


class PydanticJSONType(TypeDecorator):
    """把指定 pydantic 模型存为 JSON 字符串列。

    - 写入：``model.model_dump_json()`` → 字符串
    - 读取：字符串 → ``ModelClass.model_validate_json()``
    """

    impl = String
    cache_ok = True

    def __init__(self, model_class: type[BaseModel], *args, **kwargs):
        self.model_class = model_class
        super().__init__(*args, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            return value  # 已经是 JSON 字符串
        return value.model_dump_json()

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return None
        if isinstance(value, self.model_class):
            return value
        return self.model_class.model_validate_json(value)


class DictJSONType(TypeDecorator):
    """把 dict 存为 JSON 字符串列（用于 im_bindings 这类自由 dict）。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        import json

        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return {}
        import json

        return json.loads(value)
