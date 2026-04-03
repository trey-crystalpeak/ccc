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
    total_cost_usd: float = 0.0
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def current_task(self) -> Task:
        return self.tasks[-1]

    def handle_event(self, event: Event) -> None:
        """Advance the state machine based on an event."""
        new_status = None

        if self.status == Status.NAMING:
            if event == Event.NAME_GENERATED:
                new_status = Status.RUNNING
            elif event == Event.FAILURE:
                new_status = Status.ERROR

        elif self.status == Status.RUNNING:
            if event == Event.WORKER_EXITED:
                new_status = Status.REVIEWING
            elif event == Event.FAILURE:
                new_status = Status.ERROR

        elif self.status == Status.REVIEWING:
            if event == Event.WORK_DONE:
                new_status = Status.PENDING_HAS_CHANGES
            elif event == Event.ANSWER:
                new_status = Status.IDLE_ANSWERED
            elif event == Event.NEEDS_INPUT:
                new_status = Status.PENDING_NEEDS_INPUT
            elif event == Event.NOT_COMPLETE:
                new_status = Status.RUNNING
            elif event == Event.FAILURE:
                new_status = Status.ERROR

        elif self.status == Status.PENDING_HAS_CHANGES:
            if event == Event.USER_MESSAGE:
                new_status = Status.RUNNING
            elif event == Event.USER_MERGES:
                new_status = Status.MERGING

        elif self.status == Status.PENDING_NEEDS_INPUT:
            if event == Event.USER_MESSAGE:
                new_status = Status.RUNNING
            elif event == Event.USER_MERGES:
                new_status = Status.MERGING

        elif self.status == Status.MERGING:
            if event == Event.MERGE_SUCCEEDED:
                new_status = Status.IDLE
            elif event == Event.MERGE_CONFLICTED:
                new_status = Status.RUNNING
            elif event == Event.FAILURE:
                new_status = Status.ERROR

        elif self.status == Status.IDLE:
            if event == Event.USER_MESSAGE:
                new_status = Status.RUNNING

        elif self.status == Status.IDLE_ANSWERED:
            if event == Event.USER_MESSAGE:
                new_status = Status.RUNNING
            elif event == Event.USER_MERGES:
                new_status = Status.MERGING

        if new_status:
            self.status = new_status
        else:
            print(f"Unexpected event {event} in status {self.status}, moving to ERROR")
            self.status = Status.ERROR
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
