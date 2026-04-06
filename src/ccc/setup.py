"""Setup — build the base image, configure git, and login to Claude."""

import json
import shlex
from pathlib import Path

from .container import Container, build_image, ensure_system, force_remove
from .workspace import CLAUDE_CONFIG_MOUNT, CONFIG_CLAUDE, CONFIG_GIT, GIT_CONFIG_MOUNT, IMAGE_NAME

SETUP_CONTAINER = "ccc-setup"
DOCKERFILE = Path(__file__).resolve().parent.parent.parent / "etc" / "Dockerfile"
GIT_CONFIG_FILE = GIT_CONFIG_MOUNT + "/config"

CLAUDE_SETTINGS = {
    "skipDangerousModePermissionPrompt": True,
    "effortLevel": "high",
}


def _setup_git(container: Container) -> None:
    name = input("Git author name: ")
    email = input("Git author email: ")
    container.exec(f"git config --file {GIT_CONFIG_FILE} user.name {shlex.quote(name)}")
    container.exec(f"git config --file {GIT_CONFIG_FILE} user.email {shlex.quote(email)}")


def _setup_claude(container: Container) -> None:
    print("Log into Claude Code: type /login, authenticate, then exit.")
    container.exec("claude", interactive=True)


def run_setup(login_only: bool = False, git_only: bool = False) -> None:
    ensure_system()

    if not login_only and not git_only:
        print("Building base image...")
        build_image(IMAGE_NAME, str(DOCKERFILE), str(DOCKERFILE.parent))

    CONFIG_CLAUDE.mkdir(parents=True, exist_ok=True)
    CONFIG_GIT.mkdir(parents=True, exist_ok=True)

    # Pre-configure settings so --dangerously-skip-permissions works
    # without an interactive confirmation prompt.
    settings_file = CONFIG_CLAUDE / "settings.json"
    if settings_file.exists():
        existing = json.loads(settings_file.read_text())
        merged = {**existing, **CLAUDE_SETTINGS}
    else:
        merged = CLAUDE_SETTINGS
    settings_file.write_text(json.dumps(merged, indent=2) + "\n")

    volumes = [
        (str(CONFIG_CLAUDE), CLAUDE_CONFIG_MOUNT),
        (str(CONFIG_GIT), GIT_CONFIG_MOUNT),
    ]

    force_remove(SETUP_CONTAINER)
    print("Starting setup container...")
    container = Container(SETUP_CONTAINER)
    container.start(IMAGE_NAME, volumes)

    steps = []
    if not login_only:
        steps.append(_setup_git)
    if not git_only:
        steps.append(_setup_claude)

    try:
        for step in steps:
            step(container)
    finally:
        print("Cleaning up setup container...")
        container.stop()
        container.remove()
    print("Setup complete.")
