"""Pydantic output contracts referenced by checked-in AI tasks."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentClassificationResult(BaseModel):
    """Small non-product fixture result for ``document.classify``."""

    model_config = ConfigDict(extra="forbid")

    category: Literal["lease", "invoice", "correspondence", "other"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=500)
