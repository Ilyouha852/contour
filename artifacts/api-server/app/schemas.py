from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RecommendRequest(BaseModel):
    lot_id: str = Field(min_length=1, max_length=64)
    top_k: int = Field(default=20, ge=1, le=100)
    loss_weight: float | None = Field(default=None, ge=0, le=1)

    @field_validator("lot_id")
    @classmethod
    def trim_lot_id(cls, value: str) -> str:
        return value.strip()


class BatchRunRequest(BaseModel):
    lot_ids: list[str] | None = None
    top_k: int = Field(default=20, ge=1, le=100)
    loss_weight: float | None = Field(default=None, ge=0, le=1)

    @field_validator("lot_ids")
    @classmethod
    def trim_lot_ids(cls, value: list[str] | None) -> list[str] | None:
        return [item.strip() for item in value if item.strip()] if value is not None else None