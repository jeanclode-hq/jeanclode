"""Base schemas for common API patterns."""

from time import time
from typing import Annotated

from pydantic import BaseModel, Field

PAGINATION_LIMIT = Field(
    ge=5,
    le=200,
    title="Limit",
    description="Maximum number of items per page",
    examples=[20, 50, 100],
)

PAGINATION_PAGE = Field(
    ge=1,
    title="Page number",
    description="Page number (1-indexed)",
    examples=[1, 2, 3],
)

SEARCH = Field(
    min_length=1,
    max_length=200,
    title="Search term",
    description="Search term to filter results across multiple fields",
)

PAGINATION_ORDER_BY = Field(
    title="Order by",
    description="Fields to sort by. Prefix with '-' for descending order.",
    examples=[["-created_at"], ["repository", "-pr_number"]],
)


class ListGenericQuery(BaseModel):
    """Common query params a listing endpoint may receive."""

    limit: Annotated[int, PAGINATION_LIMIT] = 20
    page: Annotated[int, PAGINATION_PAGE] = 1
    search: Annotated[str | None, SEARCH] = None
    order_by: Annotated[list[str] | None, PAGINATION_ORDER_BY] = ["-created_at"]


class PaginationMeta(BaseModel):
    """Pagination metadata for list responses."""

    total: int = Field(description="Total number of items")
    limit: int = Field(description="Items per page")
    page: int = Field(description="Current page number")


class ListMetaResponse(BaseModel):
    """Meta information for listing responses."""

    timestamp: Annotated[
        float,
        Field(
            title="Timestamp of the request",
            description="The timestamp when the request was made (Unix Time).",
        ),
    ] = Field(default_factory=time)


class ListGenericResponse[T](BaseModel):
    """Generic listing response."""

    objects: list[T]
    meta: ListMetaResponse = Field(default_factory=ListMetaResponse)


class ListPaginatedResponse[T](ListGenericResponse[T]):
    """Extend listing response with the pagination object."""

    pagination: PaginationMeta
