"""Abstraction over the Claude Code CLI."""

import json
import shlex
from dataclasses import dataclass, field


REVIEWER_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["work_done", "answer", "needs_input", "not_complete"],
        },
        "comment": {"type": "string"},
    },
    "required": ["status", "comment"],
})

CONDITION_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "result": {"type": "boolean"},
    },
    "required": ["result"],
})


@dataclass
class ClaudeResult:
    type: str = ""
    subtype: str = ""
    result: str = ""
    session_id: str = ""
    total_cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = False
    structured_output: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, stdout: str) -> "ClaudeResult":
        data = json.loads(stdout)
        data["model_usage"] = data.pop("modelUsage", {})
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def _command(prompt: str, *args: str) -> str:
    return " ".join([
        "claude",
        "-p", shlex.quote(prompt),
        *args,
        "--dangerously-skip-permissions",
        "--output-format", "json",
    ])


class Agent:

    def __init__(self, name: str, *, model: str, schema: str = "",
                 max_turns: int = 0, persist_session: bool = False,
                 state=None) -> None:
        self.name = name
        self.model = model
        self.schema = schema
        self.max_turns = max_turns
        self.persist_session = persist_session
        self.state = state

    def command(self, prompt: str, *, fork: bool = False,
                system_prompt: str = "") -> str:
        """Build a shell command string. Session ID is pulled from state."""
        args = []

        if fork or self.persist_session:
            session_id = self.state.session_id if self.state else ""
            if fork and not session_id:
                raise ValueError(f"{self.name}: cannot fork without a session_id")
            if session_id:
                args.extend(["-r", shlex.quote(session_id)])

        if fork:
            args.append("--fork-session")

        args.extend(["--model", self.model])

        if system_prompt:
            args.extend(["--append-system-prompt", shlex.quote(system_prompt)])
        if self.schema:
            args.extend(["--json-schema", shlex.quote(self.schema)])
        if self.max_turns:
            args.extend(["--max-turns", str(self.max_turns)])
        if not self.persist_session:
            args.append("--no-session-persistence")

        return _command(prompt, *args)
