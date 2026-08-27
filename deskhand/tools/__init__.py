"""The tool registry.

Importing this package registers every tool. The split by module is the risk
split, and it is not cosmetic: which class a tool belongs to decides whether it
can run without a human, and that decision is made here, once, at import time.
"""

from deskhand.tools import irreversible, read, reversible  # noqa: F401  (import registers)
from deskhand.tools.base import (
    RiskClass,
    ToolContext,
    ToolDef,
    ToolError,
    ToolOutcome,
    all_tools,
    api_schemas,
    args_hash,
    get,
    is_registered,
    requires_approval,
)

__all__ = [
    "RiskClass",
    "ToolContext",
    "ToolDef",
    "ToolError",
    "ToolOutcome",
    "all_tools",
    "api_schemas",
    "args_hash",
    "get",
    "is_registered",
    "requires_approval",
]
