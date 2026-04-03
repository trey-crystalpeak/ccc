# ccc

A system for running Claude Code autonomously in isolated Apple Containers. You send a task, Claude works on it independently in a containerized copy of your project, a reviewer checks completion, and you review and merge the results.

## Prerequisites

- macOS 26 (Tahoe) with Apple silicon
- Apple Container CLI installed (`/usr/local/bin/container`)
- Python 3.9+
- A Claude Code account (Max plan recommended for Opus access)

## Setup

```
python3 -m ccc setup
```

This builds the base container image, prompts for your git identity, and launches Claude Code for you to log in. To update just credentials or git config later:

```
python3 -m ccc setup --login-only
python3 -m ccc setup --git-only
```

## Usage

Start the daemon (runs in foreground):

```
python3 -m ccc daemon start
```

Create a workspace and send a task:

```
python3 -m ccc new /path/to/project "Add a dark mode toggle"
```

Check on your workspaces:

```
python3 -m ccc list
python3 -m ccc list --cwd
python3 -m ccc status <id>
python3 -m ccc diff <id>
python3 -m ccc logs <id>
```

When work is done, merge or provide feedback:

```
python3 -m ccc merge <id>
python3 -m ccc send <id> "Use Tailwind instead of custom CSS"
```

Respond to questions, send new tasks to idle workspaces, or provide feedback — all through `send`:

```
python3 -m ccc send <id> "Use PostgreSQL"
python3 -m ccc send <id> "Now add user authentication"
```

Keep a workspace up to date with the host project:

```
python3 -m ccc sync <id>
python3 -m ccc send <id> "Rebase on host-main"
```

Clean up:

```
python3 -m ccc delete <id>
python3 -m ccc cleanup
python3 -m ccc daemon stop
```

## Architecture

```
cli.py          Thin client. Parses args, sends JSON over Unix socket.
daemon.py       Background server. Manages workspaces, dispatches commands, spawns threads.
orchestrator.py Claude workflow: naming → task → fork → review → classify.
autoprompt.py   Automated follow-up tasks triggered by conditions.
workspace.py    Project copying, git branching, merging, directory layout.
state.py        Status enum, event-driven state machine, JSON persistence.
container.py    Abstraction over Apple Container CLI.
claude.py       Agent abstraction over Claude Code CLI.
config.py       Global and per-project configuration loading.
setup.py        Image building, git config, Claude login.
```

## Workspace Lifecycle

Each workspace gets its own container, its own copy of the project on a dedicated git branch, and its own Claude session. The daemon drives the workflow through a state machine:

```
naming → running → reviewing → pending_has_changes → merging → idle
                            → pending_needs_input
                            → idle_answered
```

The review loop (fork → summarize → review → classify) runs after every Claude exit. If the reviewer says "not complete," Claude keeps working. If "work done," the workspace waits for you to merge or provide feedback. If "needs input," it waits for your answer.

Merge conflicts are resolved by Claude: the daemon merges your project's changes into the workspace copy, and Claude resolves the conflict markers.

## Directory Layout

```
~/.ccc/
  daemon.sock                          Unix socket
  config/
    claude/                            Claude credentials (shared mount into all containers)
    git/                               Master git config
    ccc.json                           Global config (auto-prompts)
  workspaces/
    <encoded-project-path>/
      <workspace-uuid>/
        state.json                     Workspace state
        mnt/
          project/                     Copy of git repo (mounted at /home/user/project)
          git-config/                  Copy of git config (mounted at /home/user/.config/git)
```

## Project Configuration

Place a `.ccc.json` file in your project root to configure per-project behavior:

```json
{
  "image": {
    "apt": ["nodejs", "npm", "postgresql-client"],
    "run": ["npm install -g typescript"]
  },
  "auto_prompts": [
    {"condition": {"type": "always"}, "prompt": "Run the test suite. Fix failures."}
  ]
}
```

**image** — custom container image. Extends the base image with additional system packages and setup commands. Built once and cached by content hash.

**auto_prompts** — follow-up tasks that run automatically after every completed task. Same format as the global config (`~/.ccc/config/ccc.json`). Both global and project auto-prompts apply; global runs first.

## Design Decisions

- **Python stdlib only.** No third-party dependencies. Compatible with macOS built-in Python 3.9.
- **Apple Containers only.** No Docker/Podman support.
- **In-memory state is truth.** File persistence exists only for daemon restart recovery.
- **No error recovery.** If something fails, the workspace goes to error state. Delete and start over.
- **Project copies, not worktrees.** Each workspace gets a full copy of the repo for complete isolation.
- **Regular merges, not squash.** Keeps histories compatible across multiple tasks on the same workspace.
- **Five agents, three models.** Namer (Haiku), Worker (Opus), Reviewer, Summarizer, and Evaluator (Sonnet). Each workspace owns its own agent instances.
