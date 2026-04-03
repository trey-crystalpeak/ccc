"""Abstraction over the Apple Container CLI."""

import json
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int


def ensure_system() -> None:
    """Start the container service if it is not already running."""
    result = subprocess.run(
        ["container", "system", "status"],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(["container", "system", "start"], check=True)


def build_image(tag: str, dockerfile_path: str, context_path: str) -> None:
    subprocess.run(
        ["container", "build", "--tag", tag, "--file", dockerfile_path, context_path],
        check=True,
    )


def image_exists(tag: str) -> bool:
    result = subprocess.run(
        ["container", "image", "inspect", tag],
        capture_output=True,
    )
    return result.returncode == 0


def force_remove(name: str) -> None:
    """Force-remove a container by name, ignoring errors if it doesn't exist."""
    subprocess.run(["container", "rm", "--force", name], capture_output=True)


def is_running(name: str) -> bool:
    result = subprocess.run(
        ["container", "inspect", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
        if isinstance(data, list):
            data = data[0]
        return data.get("state") == "running"
    except (json.JSONDecodeError, IndexError, KeyError):
        return False


class Container:
    """An Apple Container, driven via `container exec`."""

    def __init__(self, name: str) -> None:
        self.name = name

    def start(self, image: str, volumes: list[tuple[str, str]]) -> None:
        args = ["container", "run", "-d", "--name", self.name]
        for host_path, container_path in volumes:
            args.extend(["-v", f"{host_path}:{container_path}"])
        args.extend([image, "sleep", "infinity"])

        subprocess.run(args, check=True, capture_output=True)

    def exec(
        self,
        command: str,
        workdir: Optional[str] = None,
        interactive: bool = False,
    ) -> ExecResult:
        """Run a command inside the container.

        When interactive is True, stdin/stdout/stderr are attached directly
        to the terminal. The returned ExecResult will have empty stdout and
        stderr in that case.
        """
        args = ["container", "exec"]
        if interactive:
            args.append("-it")
        if workdir:
            args.extend(["--workdir", workdir])
        args.extend([self.name, "bash", "--login", "-c", command])

        if interactive:
            result = subprocess.run(args)
            return ExecResult(stdout="", stderr="", exit_code=result.returncode)

        result = subprocess.run(args, capture_output=True, text=True)
        return ExecResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
        )

    def stop(self) -> None:
        subprocess.run(["container", "stop", self.name], capture_output=True)

    def remove(self) -> None:
        subprocess.run(["container", "rm", self.name], capture_output=True)
