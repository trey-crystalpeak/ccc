"""Workspace state management."""

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from pathlib import Path


class Status(str, Enum):
    NAMING = "naming"
    RUNNING = "running"
    REVIEWING = "reviewing"
    PENDING_HAS_CHANGES = "pending_has_changes"
    PENDING_NEEDS_INPUT = "pending_needs_input"
    MERGING = "merging"
    IDLE = "idle"
    IDLE_ANSWERED = "idle_answered"
    ERROR = "error"


class Event(Enum):
    NAME_GENERATED = auto()
    WORKER_EXITED = auto()
    WORK_DONE = auto()
    ANSWER = auto()
    NEEDS_INPUT = auto()
    NOT_COMPLETE = auto()
    USER_MESSAGE = auto()
    USER_MERGES = auto()
    MERGE_SUCCEEDED = auto()
    MERGE_CONFLICTED = auto()
    FAILURE = auto()


# States that survive a daemon restart. Everything else becomes ERROR.
RECOVERABLE_STATES = {
    Status.PENDING_HAS_CHANGES,
    Status.PENDING_NEEDS_INPUT,
    Status.IDLE,
    Status.IDLE_ANSWERED,
}

# (current_status, event) -> next_status
TRANSITIONS: dict[tuple[Status, Event], Status] = {
    (Status.NAMING, Event.NAME_GENERATED): Status.RUNNING,
    (Status.NAMING, Event.FAILURE): Status.ERROR,
    (Status.RUNNING, Event.WORKER_EXITED): Status.REVIEWING,
    (Status.RUNNING, Event.FAILURE): Status.ERROR,
    (Status.REVIEWING, Event.WORK_DONE): Status.PENDING_HAS_CHANGES,
    (Status.REVIEWING, Event.ANSWER): Status.IDLE_ANSWERED,
    (Status.REVIEWING, Event.NEEDS_INPUT): Status.PENDING_NEEDS_INPUT,
    (Status.REVIEWING, Event.NOT_COMPLETE): Status.RUNNING,
    (Status.REVIEWING, Event.FAILURE): Status.ERROR,
    (Status.PENDING_HAS_CHANGES, Event.USER_MESSAGE): Status.RUNNING,
    (Status.PENDING_HAS_CHANGES, Event.USER_MERGES): Status.MERGING,
    (Status.PENDING_NEEDS_INPUT, Event.USER_MESSAGE): Status.RUNNING,
    (Status.PENDING_NEEDS_INPUT, Event.USER_MERGES): Status.MERGING,
    (Status.MERGING, Event.MERGE_SUCCEEDED): Status.IDLE,
    (Status.MERGING, Event.MERGE_CONFLICTED): Status.RUNNING,
    (Status.MERGING, Event.FAILURE): Status.ERROR,
    (Status.IDLE, Event.USER_MESSAGE): Status.RUNNING,
    (Status.IDLE_ANSWERED, Event.USER_MESSAGE): Status.RUNNING,
    (Status.IDLE_ANSWERED, Event.USER_MERGES): Status.MERGING,
}


@dataclass
class Task:
    prompt: str
    completed: bool = False
    summary: str = ""
    reviewer_comment: str = ""
    messages: list = field(default_factory=list)


@dataclass
class WorkspaceState:
    workspace_id: str
    project_path: str
    status: Status = Status.NAMING
    name: str = ""
    session_id: str = ""
    host_branch: str = ""
    tasks: list = field(default_factory=list)
    mounts: list = field(default_factory=list)
    total_cost_usd: float = 0.0
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def current_task(self) -> Task:
        return self.tasks[-1]

    def handle_event(self, event: Event) -> None:
        """Advance the state machine based on an event."""
        new_status = TRANSITIONS.get((self.status, event))
        if new_status is None:
            print(f"Unexpected event {event} in status {self.status}, moving to ERROR")
            new_status = Status.ERROR
        self.status = new_status
        self.updated_at = time.time()

    def save(self, path: Path) -> None:
        data = asdict(self)
        data["status"] = self.status.value
        path.write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> "WorkspaceState":
        data = json.loads(path.read_text())
        data["status"] = Status(data["status"])
        data["tasks"] = [Task(**t) for t in data["tasks"]]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})
