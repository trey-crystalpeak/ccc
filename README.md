# ccc

Run Claude Code tasks in the background, in containers, in parallel.

Each task gets its own container with a full copy of your repo on a fresh branch. A reviewer agent checks the output and sends the task back if the work isn't finished.

## Example

```
$ ccc new . "Add input validation to the signup form"
Created workspace 7f2a91c3-b8e4-4d1f-a562-9c0e3f71d804
```

```
$ ccc
ID                                    STATUS               NAME                       UPDATED  PROJECT
7f2a91c3-b8e4-4d1f-a562-9c0e3f71d804  pending_has_changes  signup form validation     4m ago   /Users/me/webapp
```

```
$ ccc status 7f2a91c3-b8e4-4d1f-a562-9c0e3f71d804
signup form validation  [pending_has_changes]  $0.0872
  Add input validation to the signup form

--- Review ---
  All validation rules implemented and tested.

--- Summary ---
  Added email format, password strength, and username length validation
  to the signup form. Wrote unit tests for each rule.
```

```
$ ccc diff 7f2a91c3-b8e4-4d1f-a562-9c0e3f71d804
diff --git a/src/components/SignupForm.tsx b/src/components/SignupForm.tsx
index 3a1b2c4..9d8e7f6 100644
--- a/src/components/SignupForm.tsx
+++ b/src/components/SignupForm.tsx
@@ -1,5 +1,6 @@
 import React, { useState } from 'react';
+import { validateEmail, validatePassword, validateUsername } from '../validation';
...
```

```
$ ccc merge 7f2a91c3-b8e4-4d1f-a562-9c0e3f71d804
Merged.
```

Send follow-up work to the same workspace without re-explaining context:

```
$ ccc send 7f2a91c3-b8e4-4d1f-a562-9c0e3f71d804 "Also validate phone numbers"
Sent.
```

## Setup

Requires macOS 26 (Tahoe), Apple silicon, Python 3.9+, the [Apple Container CLI](https://developer.apple.com/documentation/apple-containers), and a Claude Code account.

```
python3 -m pip install .
ccc setup          # builds container image, configures git, logs into Claude
ccc daemon start   # runs in foreground — background it however you like
```

After installing, restart your shell (or run `hash -r` in bash/zsh) so it picks up the new `ccc` command. To uninstall: `python3 -m pip uninstall ccc`.

`ccc setup` builds the base container image, prompts for your git author name and email, then opens an interactive Claude Code session for `/login`. Credentials are stored in `~/.ccc/config/`.

## Commands

```
ccc                            List all workspaces
ccc new <path> <prompt>        Start a task
ccc list [--cwd]               List workspaces (--cwd filters to current directory)
ccc status <id>                Show state, task, cost, review, and summary
ccc diff <id>                  Show git diff of workspace changes
ccc logs <id>                  Show Claude Code session output
ccc send <id> <message>        Send follow-up work, feedback, or answers
ccc merge <id>                 Merge workspace branch into host project
ccc sync <id>                  Pull host changes into the workspace
ccc delete <id>                Tear down workspace and container
ccc cleanup                    Remove all finished workspaces
ccc daemon start               Start the daemon
ccc daemon stop                Stop the daemon
```

## Auto-prompts

Prompts that should run on every task can be configured per-project in `.ccc.json`:

```json
{
  "auto_prompts": [
    { "condition": { "type": "always" }, "prompt": "Run the test suite. Fix failures." },
    { "condition": { "type": "lines_changed", "gt": 50 }, "prompt": "Add tests for the new code." }
  ]
}
```

Conditions:
- `always` — runs unconditionally.
- `lines_changed` with `gt` — runs if the diff exceeds N lines changed.
- `agent_question` with `question` — an agent evaluates the question against the workspace state.

Auto-prompts can be chained with `then` to run follow-up prompts after the first completes.

Global auto-prompts can be set in `~/.ccc/config/ccc.json` using the same format. Global prompts run first, then project prompts.

## Custom images

If your project needs additional tooling in the container, add an `image` key to `.ccc.json`:

```json
{
  "image": {
    "apt": ["nodejs", "npm", "postgresql-client"],
    "run": ["npm install -g typescript"]
  }
}
```

Custom images extend `ccc-base` (Ubuntu 24.04 with git and curl). They are tagged by content hash and rebuilt only when the config changes.

## Architecture

Each workspace gets a full project copy, its own container, and five agents across three model tiers:

| Agent | Model | Role |
|-------|-------|------|
| namer | Haiku | Generate a short display name for the task |
| worker | Opus | Execute the task (session persists across follow-ups) |
| reviewer | Sonnet | Classify whether work is done, needs input, or should continue |
| summarizer | Sonnet | Summarize what the agent did |
| evaluator | Sonnet | Evaluate auto-prompt conditions |

Workspaces move through a state machine: `naming` -> `running` -> `reviewing` -> `pending_has_changes` / `pending_needs_input` -> `idle`. The reviewer can send work back to `running` if it isn't complete.

No third-party Python dependencies. Full design details are in [DESIGN.md](DESIGN.md).
