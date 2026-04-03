# ccc

Run Claude Code tasks in the background, in containers, in parallel.

```
$ ccc new . "Add a dark mode toggle to the settings page"
Created workspace dark-mode-toggle (a1b2c3d4)
```

That's it. Claude gets its own container, its own copy of your repo on a fresh branch, and works until it's done. A reviewer agent checks the output — if the work isn't finished, Claude keeps going. When it's ready:

```
$ ccc diff a1b2c3d4
  src/components/Settings.tsx  |  42 +++++++++++
  src/styles/theme.ts          |  18 +++++

$ ccc merge a1b2c3d4
Merged dark-mode-toggle into main.
```

## The problem

If you've used Claude Code for real work, you know the drill. You have it working on a feature in one terminal, a bug fix in another, maybe a refactor in a third. You're alt-tabbing between sessions, copy-pasting the same "now run the tests and fix what's broken" prompt into each one, and hoping two agents don't edit the same file.

It works, but it's a lot of babysitting for a tool that's supposed to save you time.

ccc gives each task its own isolated environment and runs them all in the background. You send work, check in when you want, and merge when you're happy with the diff.

## Setup

macOS 26 (Tahoe), Apple silicon, Python 3.9+, the [Apple Container CLI](https://developer.apple.com/documentation/apple-containers), and a Claude Code account.

```
pip install .
ccc setup          # builds container image, configures git, logs into Claude
ccc daemon start   # runs in foreground — background it however you like
```

## Usage

```
ccc new <project> <prompt>     Start a task
ccc list [--cwd]               What's running
ccc status <id>                State, current task, cost so far
ccc diff <id>                  What changed
ccc logs <id>                  Claude's session output
ccc send <id> <message>        Feedback, answers, or a new task
ccc merge <id>                 Merge into your project
ccc sync <id>                  Pull host changes into the workspace
ccc delete <id>                Tear down workspace and container
ccc cleanup                    Remove all finished workspaces
```

Send feedback without re-explaining context:

```
$ ccc send a1b2c3d4 "Use Tailwind instead of custom CSS"
```

Keep a workspace alive for follow-up tasks:

```
$ ccc send a1b2c3d4 "Now add user authentication"
```

## Auto-prompts

Things you'd paste into every session anyway — run tests, lint, etc. — can be automated per-project:

```json
{
  "auto_prompts": [
    { "condition": { "type": "always" }, "prompt": "Run the test suite. Fix failures." },
    { "condition": { "type": "lines_changed", "gt": 50 }, "prompt": "Add tests for the new code." }
  ]
}
```

Drop that in `.ccc.json` at your project root.

## Custom images

If your project needs specific tooling in the container:

```json
{
  "image": {
    "apt": ["nodejs", "npm", "postgresql-client"],
    "run": ["npm install -g typescript"]
  }
}
```

Also in `.ccc.json`. Built once, cached by content hash.

## Under the hood

Each workspace gets a full project copy, its own container, and five agents across three model tiers (Haiku for naming, Opus for the actual work, Sonnet for review). No third-party Python dependencies.

Architecture, state machine, and directory layout are in [DESIGN.md](DESIGN.md).
