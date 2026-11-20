"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    window_name: str
    biz_key: str
    event_ts: str
    amount: float = Field(gt=-1e12)


class WindowCreateIn(BaseModel):
    name: str
    open_at: str
    close_at: str


class HoldIn(BaseModel):
    window_name: str
    biz_key: str
    event_ts: str
    amount: float


class ReopenIn(BaseModel):
    mode: str = "作废重算"  # 本仓正确性口径已定甲路径（作废重算）；其余口径视为偏离窗重开册
    fail_simulate: bool = False  # 夹具用：强制重开事务中段失败，验证状态与世代回滚


class MaterializeIn(BaseModel):
    partition_key: str | None = None
    fail_simulate: bool = False
