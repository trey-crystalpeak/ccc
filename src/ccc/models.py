"""Response models for the daemon-CLI protocol."""

from dataclasses import dataclass
from typing import Optional

from .state import Task


@dataclass
class WorkspaceSummary:
    id: str = ""
    name: str = ""
    status: str = ""
    project_path: str = ""
    total_cost_usd: float = 0.0
    updated_at: float = 0.0


@dataclass
class CleanupEntry:
    id: str = ""
    name: str = ""


@dataclass
class StatusDetail:
    id: str = ""
    name: str = ""
    status: str = ""
    total_cost_usd: float = 0.0
    error_message: Optional[str] = None
    task: Optional[Task] = None

    @classmethod
    def from_dict(cls, data: dict) -> "StatusDetail":
        task_data = data.get("task")
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            error_message=data.get("error_message"),
            task=Task(**task_data) if task_data else None,
        )
