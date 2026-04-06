"""Workspace management — project copying, git branching, merging, cleanup."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .claude import CONDITION_SCHEMA, REVIEWER_SCHEMA, Agent
from .config import load_project
from .container import Container, Volume, build_image, force_remove, image_exists, is_running
from .state import RECOVERABLE_STATES, Event, Status, Task, WorkspaceState

BASE_DIR = Path.home() / ".ccc"
WORKSPACES_DIR = BASE_DIR / "workspaces"
CONFIG_CLAUDE = BASE_DIR / "config" / "claude"
CONFIG_GIT = BASE_DIR / "config" / "git"
SOCKET_PATH = str(BASE_DIR / "daemon.sock")
IMAGE_NAME = "ccc-base"
WORKDIR = "/home/user/project"
CLAUDE_CONFIG_MOUNT = "/home/user/.claude"
GIT_CONFIG_MOUNT = "/home/user/.config/git"


def _encode_path(path: str) -> str:
    """Encode a project path as a flat directory name.

    Matches Claude Code's convention: replace / with -.
    This is lossy (/foo/bar and /foo-bar collide) but collisions
    are unlikely in practice, and consistency with Claude Code is
    more valuable than theoretical uniqueness.
    """
    return path.replace("/", "-").lstrip("-")


def _git(*args, repo=None):
    """Run a git command. Returns completed process."""
    cmd = ["git"]
    if repo:
        cmd.extend(["-C", str(repo)])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _resolve_image(image_config: dict) -> str:
    """Build a custom image from config if needed, return its tag."""
    apt = image_config.get("apt", [])
    run = image_config.get("run", [])
    content = json.dumps({"apt": apt, "run": run}, sort_keys=True)
    tag = "ccc-" + hashlib.sha256(content.encode()).hexdigest()[:12]

    if image_exists(tag):
        return tag

    lines = [f"FROM {IMAGE_NAME}", "USER root"]
    if apt:
        lines.append(f"RUN apt-get update && apt-get install -y {' '.join(apt)}")
    for cmd in run:
        lines.append(f"RUN {cmd}")
    lines.append("USER user")

    with tempfile.TemporaryDirectory() as tmpdir:
        dockerfile = Path(tmpdir) / "Dockerfile"
        dockerfile.write_text("\n".join(lines) + "\n")
        print(f"Building custom image {tag}...")
        build_image(tag, str(dockerfile), tmpdir)

    return tag


class Workspace:
    """A workspace: its own copy of a project, container, and Claude session.

    The mnt_* paths are host-side mount sources for container volumes.
    """

    def __init__(self, host_project: str, id: Optional[str] = None) -> None:
        self.id = id or str(uuid.uuid4())
        self.host_project = Path(host_project).resolve()
        self.path = WORKSPACES_DIR / _encode_path(str(self.host_project)) / self.id
        self.mnt_project = self.path / "mnt" / "project"
        self.mnt_git_config = self.path / "mnt" / "git-config"
        self.state_file = self.path / "state.json"
        self._lock = threading.Lock()
        self._mounts: list[Volume] = []

    def _init_agents(self) -> None:
        self.agent_namer = Agent("namer", model="haiku", max_turns=1, tools="", state=self.state)
        self.agent_worker = Agent("worker", model="opus", persist_session=True, state=self.state)
        self.agent_reviewer = Agent(
            "reviewer", model="sonnet", schema=REVIEWER_SCHEMA, state=self.state
        )
        self.agent_summarizer = Agent("summarizer", model="sonnet", state=self.state)
        self.agent_evaluator = Agent(
            "evaluator", model="sonnet", schema=CONDITION_SCHEMA, state=self.state
        )

    @property
    def volumes(self) -> list[Volume]:
        return [
            Volume(str(self.mnt_project), WORKDIR),
            Volume(str(CONFIG_CLAUDE), CLAUDE_CONFIG_MOUNT),
            Volume(str(self.mnt_git_config), GIT_CONFIG_MOUNT),
            *self._mounts,
        ]

    @property
    def context_paths(self) -> list[str]:
        return [v.container_path for v in self._mounts]

    def _prepare_mounts(self, paths: list[str]) -> list[Volume]:
        """Resolve CLI mount paths into readonly volumes.

        Directories are mounted directly at /context/<name>. Files are
        copied into a shared staging directory mounted at /context/ since
        Apple Containers only support directory mounts.
        """
        staging = self.path / "mnt" / "context"
        mounts = []
        for raw in paths:
            src = Path(raw).expanduser().resolve()
            if not src.exists():
                raise ValueError(f"Mount source does not exist: {src}")
            if src.is_file():
                staging.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, staging / src.name)
            else:
                mounts.append(Volume(str(src), f"/context/{src.name}", readonly=True))
        if staging.exists():
            mounts.insert(0, Volume(str(staging), "/context", readonly=True))
        return mounts

    def create(self, prompt: str, mounts: Optional[list[str]] = None) -> None:
        branch_result = _git("rev-parse", "--abbrev-ref", "HEAD", repo=self.host_project)
        self.state = WorkspaceState(
            workspace_id=self.id,
            project_path=str(self.host_project),
            host_branch=branch_result.stdout.strip(),
        )
        self.container = Container(self.id)
        self._init_agents()

        print(f"[{self.id}] Copying project...")
        self.path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.host_project, self.mnt_project)
        shutil.copytree(CONFIG_GIT, self.mnt_git_config)
        _git("checkout", "-b", self.id, repo=self.mnt_project)

        self._mounts = self._prepare_mounts(mounts or [])
        self.state.mounts = [asdict(v) for v in self._mounts]
        self.state.tasks.append(Task(prompt=prompt))
        self.state.save(self.state_file)

        project_config = load_project(str(self.host_project))
        image_config = project_config.get("image", {})
        image = _resolve_image(image_config) if image_config else IMAGE_NAME

        print(f"[{self.id}] Starting container...")
        self.container.start(image, self.volumes)

    @classmethod
    def restore(cls, state_file: Path) -> "Workspace":
        """Restore from disk. Non-recoverable states become ERROR."""
        state = WorkspaceState.load(state_file)
        if state.status not in RECOVERABLE_STATES:
            state.status = Status.ERROR
            state.updated_at = time.time()
            state.save(state_file)

        ws = cls(state.project_path, id=state.workspace_id)
        ws.state = state
        ws.container = Container(ws.id)
        ws._init_agents()
        ws._mounts = [Volume(**m) for m in state.mounts]

        label = state.name or state.workspace_id
        if state.status != Status.ERROR and not is_running(ws.id):
            project_config = load_project(state.project_path)
            image_config = project_config.get("image", {})
            image = _resolve_image(image_config) if image_config else IMAGE_NAME
            print(f"[{label}] Restarting container...")
            force_remove(ws.id)
            ws.container.start(image, ws.volumes)
        else:
            print(f"[{label}] Restored ({state.status.value})")

        return ws

    def handle_event(self, event: Event) -> None:
        """Fire a state machine event and persist."""
        with self._lock:
            self.state.handle_event(event)
            self.state.save(self.state_file)

    def head_commit(self) -> str:
        result = self.container.exec("git rev-parse HEAD", workdir=WORKDIR)
        return result.stdout.strip()

    def logs(self) -> str:
        # Workspace-specific logs are always correct
        candidates = list(self.mnt_project.glob(".claude/**/*.jsonl"))
        if not candidates:
            # Shared config dir has logs from ALL workspaces —
            # filter by session_id to avoid showing the wrong one.
            shared = list(CONFIG_CLAUDE.glob("**/*.jsonl"))
            if self.state.session_id:
                candidates = [p for p in shared if self.state.session_id in str(p)]
            if not candidates:
                candidates = shared
        if not candidates:
            return ""
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        return latest.read_text()

    def sync_from_host(self) -> None:
        """Fetch host branch into the workspace as refs/heads/host-main."""
        _git(
            "fetch",
            str(self.host_project),
            f"{self.state.host_branch}:refs/heads/host-main",
            repo=self.mnt_project,
        )

    def merge_from_container(self) -> bool:
        """Merge workspace branch into host project. Returns False on conflict."""
        checkout = _git("checkout", self.state.host_branch, repo=self.host_project)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"Cannot checkout {self.state.host_branch}: {checkout.stderr.strip()}"
            )
        result = _git(
            "pull",
            "--no-rebase",
            str(self.mnt_project),
            self.id,
            repo=self.host_project,
        )
        if result.returncode != 0:
            _git("merge", "--abort", repo=self.host_project)
            return False
        return True

    def merge_from_host(self) -> bool:
        """Merge host into workspace for conflict resolution."""
        _git(
            "fetch",
            str(self.host_project),
            f"{self.state.host_branch}:refs/heads/host-main",
            repo=self.mnt_project,
        )
        result = _git("merge", "host-main", repo=self.mnt_project)
        return result.returncode == 0

    def diff(self) -> str:
        """Three-dot diff of workspace changes against the host branch."""
        ref = f"refs/ccc/{self.id}"
        _git("fetch", str(self.mnt_project), f"{self.id}:{ref}", repo=self.host_project)
        try:
            result = _git(
                "diff",
                "--color=always",
                f"{self.state.host_branch}...{ref}",
                repo=self.host_project,
            )
            return result.stdout
        finally:
            _git("update-ref", "-d", ref, repo=self.host_project)

    def delete(self) -> None:
        self.container.stop()
        self.container.remove()
        if self.path.exists():
            shutil.rmtree(self.path)
