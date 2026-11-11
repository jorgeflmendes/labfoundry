"""Shared schema types."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$", min_length=1)]
