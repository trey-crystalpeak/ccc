"""Orchestrator — drives the Claude workflow for a workspace."""

import functools
import re
import threading
import time
import traceback
from typing import Optional

from .autoprompt import run_auto_prompts
from .claude import ClaudeResult
from .state import Event
from .workspace import WORKDIR, Workspace

REVIEW_EVENTS: dict[str, Event] = {
    "work_done": Event.WORK_DONE,
    "answer": Event.ANSWER,
    "needs_input": Event.NEEDS_INPUT,
    "not_complete": Event.NOT_COMPLETE,
}

SYSTEM_PROMPT = """You are on git branch '{workspace_id}'. Do not switch branches. Git identity is already configured. When finished, commit all changes with a clear commit message. Do not push.

You are working autonomously. The user is not watching — they will review your work later. If you are truly stuck, you may ask for clarification, but prefer to make reasonable assumptions and keep moving.{context_note}"""

CONTEXT_NOTE = """

Additional context files have been mounted (readonly) at the following paths:
{paths}
Refer to these files as needed to inform your work."""

NAMING_PROMPT = """Give this task a short name, 5 words or fewer. No quotes, no punctuation. Just the name.

Task: {prompt}"""

FORK_SUMMARY_PROMPT = """You were given this task:

{task_context}

Summarize the current state of your work in response to this task. Include what changes you made, what approach you took, and whether anything is unfinished or uncertain."""

REVIEWER_PROMPT = """You are reviewing the output of a coding agent. The agent was given a task and has completed a round of work. Your job is to determine the current state of the task.

{task_context}

The agent's summary of what it did:
{summary}

Changes since the task started:
{diff_stat}

Diff (may be truncated):
{diff}

The agent's summary is a starting point — trust it, but verify. The diff above gives you a head start, but you can also read files, run tests, or do anything else that helps you assess the situation.{context_note}

Based on your assessment, set the status field to one of:

- work_done: The agent has fully completed the task as required. Nothing is missing, no shortcuts were taken. Code changes have been made.
- answer: The task is straightforwardly and clearly a question, and the agent answered it. Only use this status when the task itself is obviously a question and nothing more. No code changes were made.
- needs_input: The agent is asking the user a question or needs clarification before continuing.
- not_complete: The agent made progress but has not fully completed the task. Anything less than full completion is not acceptable.

For not_complete, the comment should tell the agent specifically what is still missing or what to do next. For needs_input, the comment should be the question to surface to the user. For work_done and answer, the comment should be a brief summary for the user."""

MERGE_CONFLICT_MESSAGE = """There are merge conflicts in the working tree. The conflicts are between your changes and changes that were made to the original project while you were working. Examine both sides, consider the intent of each, and resolve all conflicts. If the correct resolution is not clear, ask the user. Commit when resolved."""


def _context_note(workspace: Workspace) -> str:
    if not workspace.context_paths:
        return ""
    paths = "\n".join(f"- {p}" for p in workspace.context_paths)
    return CONTEXT_NOTE.format(paths=paths)


def _system_prompt(workspace: Workspace) -> str:
    return SYSTEM_PROMPT.format(workspace_id=workspace.id, context_note=_context_note(workspace))


def _task_context(task) -> str:
    lines = [f"Original task: {task.prompt}"]
    for i, msg in enumerate(task.messages, 1):
        lines.append(f"User follow-up ({i}): {msg}")
    return "\n\n".join(lines)


def _log(workspace: Workspace, message: str) -> None:
    label = workspace.state.name or workspace.id
    print(f"[{label}] {message}")


_TRANSIENT_API_ERROR = re.compile(r"API Error: (500|502|503|529)\b")
_MAX_RETRIES = 5
_RETRY_DELAYS = [10, 30, 90, 300, 600]
_STATUS_INTERVAL = 30  # seconds between periodic status updates


def _is_transient_error(output: str) -> bool:
    """Check if Claude output indicates a transient API error."""
    return bool(_TRANSIENT_API_ERROR.search(output))


def _status_timer(workspace: Workspace, stop: threading.Event) -> None:
    """Log a heartbeat every _STATUS_INTERVAL seconds until *stop* is set."""
    start = time.monotonic()
    while not stop.wait(_STATUS_INTERVAL):
        elapsed = int(time.monotonic() - start)
        mins, secs = divmod(elapsed, 60)
        _log(workspace, f"Still working... ({mins}m{secs:02d}s elapsed)")


def _exec_claude(workspace: Workspace, command: str) -> ClaudeResult:
    last_error: Optional[RuntimeError] = None

    for attempt in range(_MAX_RETRIES + 1):
        stop = threading.Event()
        timer = threading.Thread(
            target=_status_timer, args=(workspace, stop), daemon=True
        )
        timer.start()
        try:
            result = workspace.container.exec(command, workdir=WORKDIR)
        finally:
            stop.set()

        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            if _is_transient_error(detail) and attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                _log(
                    workspace,
                    f"Transient API error, retrying in {delay}s (attempt {attempt + 1}/{_MAX_RETRIES})...",
                )
                last_error = RuntimeError(f"Claude exited with code {result.exit_code}: {detail}")
                time.sleep(delay)
                continue
            raise RuntimeError(f"Claude exited with code {result.exit_code}: {detail}")

        claude_result = ClaudeResult.from_json(result.stdout)

        if claude_result.is_error:
            if _is_transient_error(claude_result.result) and attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                _log(
                    workspace,
                    f"Transient API error, retrying in {delay}s (attempt {attempt + 1}/{_MAX_RETRIES})...",
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Claude returned error: {claude_result.result}")

        workspace.state.total_cost_usd += claude_result.total_cost_usd
        return claude_result

    # All retries exhausted — raise the last error or a generic one
    raise last_error or RuntimeError("Claude failed after retries")


MAX_DIFF_LINES = 200


def _diff_context(workspace: Workspace, base_commit: str) -> tuple[str, str]:
    stat = workspace.container.exec(f"git diff --stat {base_commit}..HEAD", workdir=WORKDIR)
    diff = workspace.container.exec(f"git diff {base_commit}..HEAD", workdir=WORKDIR)
    diff_lines = diff.stdout.splitlines()
    if len(diff_lines) > MAX_DIFF_LINES:
        truncated = (
            "\n".join(diff_lines[:MAX_DIFF_LINES])
            + f"\n\n... truncated ({len(diff_lines)} total lines)"
        )
    else:
        truncated = diff.stdout
    return stat.stdout.strip(), truncated.strip()


def _review_loop(workspace: Workspace, system_prompt: str, base_commit: str) -> None:
    """Run the fork → review → classify loop until a terminal state is reached."""
    task = workspace.state.current_task

    while True:
        workspace.handle_event(Event.WORKER_EXITED)

        # Fork and summarize
        _log(workspace, "Summarizing...")
        context = _task_context(task)
        fork_prompt = FORK_SUMMARY_PROMPT.format(task_context=context)
        summary_result = _exec_claude(
            workspace, workspace.agent_summarizer.command(fork_prompt, fork=True)
        )
        task.summary = summary_result.result

        # Review — structured_output is enforced by --json-schema
        _log(workspace, "Reviewing...")
        diff_stat, diff = _diff_context(workspace, base_commit)
        reviewer_prompt = REVIEWER_PROMPT.format(
            task_context=context,
            summary=task.summary,
            diff_stat=diff_stat or "(no changes)",
            diff=diff or "(no changes)",
            context_note=_context_note(workspace),
        )
        review_result = _exec_claude(workspace, workspace.agent_reviewer.command(reviewer_prompt))
        classification = review_result.structured_output
        task.reviewer_comment = classification["comment"]

        status = classification["status"]
        first_line = classification["comment"].split("\n")[0][:40]
        _log(workspace, f"Reviewer: {status} — {first_line}")

        event = REVIEW_EVENTS.get(status)
        if event is None:
            workspace.handle_event(Event.FAILURE)
            return

        if status == "not_complete":
            workspace.handle_event(event)
            retry_result = _exec_claude(
                workspace,
                workspace.agent_worker.command(
                    classification["comment"], system_prompt=system_prompt
                ),
            )
            _log(workspace, f"Worker finished (${retry_result.total_cost_usd:.4f})")
            continue

        if status == "work_done":
            workspace.container.exec(
                "git add -A && git diff --cached --quiet || git commit -m 'workspace changes'",
                workdir=WORKDIR,
            )
        elif status == "answer":
            task.completed = True

        workspace.handle_event(event)
        return


def _auto_prompts(workspace: Workspace, system_prompt: str, base_commit: str) -> None:
    run_auto_prompts(
        workspace, system_prompt, base_commit, exec_claude=_exec_claude, review_loop=_review_loop
    )


def _catch_failures(fn):
    """Wrap an orchestrator entry point so uncaught exceptions move the workspace to ERROR."""

    @functools.wraps(fn)
    def wrapper(workspace: Workspace, *args, **kwargs):
        try:
            return fn(workspace, *args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            workspace.state.error_message = str(e)
            workspace.handle_event(Event.FAILURE)

    return wrapper


@_catch_failures
def create_task(workspace: Workspace) -> None:
    """Run naming, then execute the task and enter the review loop."""
    task = workspace.state.current_task
    system_prompt = _system_prompt(workspace)

    # Generate display name
    naming_result = _exec_claude(
        workspace, workspace.agent_namer.command(NAMING_PROMPT.format(prompt=task.prompt))
    )
    workspace.state.name = naming_result.result
    workspace.handle_event(Event.NAME_GENERATED)
    print(f"[{workspace.id}] Named → {workspace.state.name}")

    # Run worker — state owns session_id
    _log(workspace, "Starting task...")
    base_commit = workspace.head_commit()
    worker_result = _exec_claude(
        workspace, workspace.agent_worker.command(task.prompt, system_prompt=system_prompt)
    )
    workspace.state.session_id = worker_result.session_id
    _log(workspace, f"Worker finished (${worker_result.total_cost_usd:.4f})")

    _review_loop(workspace, system_prompt, base_commit)
    _auto_prompts(workspace, system_prompt, base_commit)


@_catch_failures
def resume_task(workspace: Workspace, message: str) -> None:
    """Send a message to the worker and enter the review loop."""
    msg_preview = message[:40]
    _log(workspace, f"Resuming: {msg_preview}")
    base_commit = workspace.head_commit()
    system_prompt = _system_prompt(workspace)
    worker_result = _exec_claude(
        workspace, workspace.agent_worker.command(message, system_prompt=system_prompt)
    )
    _log(workspace, f"Worker finished (${worker_result.total_cost_usd:.4f})")
    _review_loop(workspace, system_prompt, base_commit)
    _auto_prompts(workspace, system_prompt, base_commit)


@_catch_failures
def resolve_conflicts(workspace: Workspace) -> None:
    """Merge from host into container and have the worker resolve conflicts."""
    _log(workspace, "Merging host changes into workspace...")
    merged_clean = workspace.merge_from_host()

    if merged_clean:
        _log(workspace, "No conflicts — retrying merge...")
        if workspace.merge_from_container():
            workspace.state.current_task.completed = True
            workspace.handle_event(Event.MERGE_SUCCEEDED)
        else:
            workspace.handle_event(Event.FAILURE)
        return

    _log(workspace, "Conflicts detected — worker resolving...")
    base_commit = workspace.head_commit()
    workspace.handle_event(Event.MERGE_CONFLICTED)
    system_prompt = _system_prompt(workspace)
    worker_result = _exec_claude(
        workspace,
        workspace.agent_worker.command(MERGE_CONFLICT_MESSAGE, system_prompt=system_prompt),
    )
    _log(workspace, f"Worker finished (${worker_result.total_cost_usd:.4f})")
    _review_loop(workspace, system_prompt, base_commit)
    _auto_prompts(workspace, system_prompt, base_commit)
