"""Consistent API response envelope.

Every endpoint returns either::

    {"success": true,  "data": {...}, "error": null, "meta": {...}}

or::

    {"success": false, "data": null, "error": {"code": "...", "message": "..."},
     "meta": {...}}

Clients can therefore branch on one field regardless of endpoint.
"""

from __future__ import annotations

from typing import Any, Dict, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str = Field(..., description="Stable machine-readable error code")
    message: str = Field(..., description="Human-readable message")
    details: Optional[Any] = Field(default=None, description="Optional structured detail")
    field: Optional[str] = Field(default=None, description="Offending field, when applicable")


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None
    field: Optional[str] = None


class APIResponse(BaseModel, Generic[T]):
    success: bool = True
    data: Optional[T] = None
    error: Optional[ErrorResponse] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


def success(data: Any = None, **meta: Any) -> Dict[str, Any]:
    """Build a success envelope."""
    return {"success": True, "data": data, "error": None, "meta": meta}


def failure(code: str, message: str, details: Any = None, field: Optional[str] = None, **meta: Any) -> Dict[str, Any]:
    """Build a failure envelope."""
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message, "details": details, "field": field},
        "meta": meta,
    }