"""Internal memory router package.

Composes the ``/internal/memory`` endpoints (view/create/str_replace/
insert/delete/rename) implementing Anthropic's ``memory_20250818`` tool
contract as workspace-scoped storage.
"""

from api.routers.internal.memory.route import router

__all__ = ["router"]
