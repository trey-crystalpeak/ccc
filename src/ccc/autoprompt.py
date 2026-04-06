"""Auto-prompts — automated follow-up tasks triggered by conditions."""

from .config import load_global, load_project
from .state import Event, Status
from .workspace import WORKDIR

CONDITION_PROMPT = """Examine the recent changes in this project (use git diff {base_commit}..HEAD) and answer this question:

{question}

Set result to true if yes, false if no."""


def _lines_changed(workspace, base_commit: str) -> int:
    """Count lines added + removed since base_commit."""
    result = workspace.container.exec(f"git diff --numstat {base_commit}..HEAD", workdir=WORKDIR)
    count = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                count += int(parts[0]) + int(parts[1])
            except ValueError:
                pass  # binary files show "-" instead of numbers
    return count


def _eval_always(workspace, condition, base_commit, exec_claude):
    return True


def _eval_lines_changed(workspace, condition, base_commit, exec_claude):
    count = _lines_changed(workspace, base_commit)
    return "gt" in condition and count > condition["gt"]


def _eval_agent_question(workspace, condition, base_commit, exec_claude):
    prompt = CONDITION_PROMPT.format(
        base_commit=base_commit,
        question=condition["question"],
    )
    result = exec_claude(workspace, workspace.agent_evaluator.command(prompt))
    return result.structured_output.get("result", False)


_CONDITION_EVALUATORS = {
    "always": _eval_always,
    "lines_changed": _eval_lines_changed,
    "agent_question": _eval_agent_question,
}


def _evaluate_condition(workspace, condition: dict, base_commit: str, exec_claude) -> bool:
    evaluator = _CONDITION_EVALUATORS.get(condition["type"])
    return evaluator(workspace, condition, base_commit, exec_claude) if evaluator else False


def _load_auto_prompts(project_path: str) -> list[dict]:
    """Load auto-prompts from global and project configs. Global runs first."""
    prompts = load_global().get("auto_prompts", [])
    prompts.extend(load_project(project_path).get("auto_prompts", []))
    return prompts


def run_auto_prompts(
    workspace, system_prompt: str, base_commit: str, *, exec_claude, review_loop
) -> None:
    prompts = _load_auto_prompts(workspace.state.project_path)
    if not prompts:
        return
    _run(
        workspace,
        system_prompt,
        prompts,
        base_commit,
        exec_claude=exec_claude,
        review_loop=review_loop,
    )


def _run(workspace, system_prompt, prompts, base_commit, *, exec_claude, review_loop) -> None:
    """Inner recursive loop for auto-prompt chains."""
    for ap in prompts:
        if workspace.state.status != Status.PENDING_HAS_CHANGES:
            break
        label = workspace.state.name or workspace.id
        try:
            if not _evaluate_condition(workspace, ap["condition"], base_commit, exec_claude):
                continue
        except Exception as e:
            print(f"[{label}] Auto-prompt condition failed: {e}")
            continue

        prompt_preview = ap["prompt"][:40]
        print(f"[{label}] Auto-prompt: {prompt_preview}")

        auto_base = workspace.head_commit()
        workspace.handle_event(Event.USER_MESSAGE)
        exec_claude(
            workspace, workspace.agent_worker.command(ap["prompt"], system_prompt=system_prompt)
        )
        review_loop(workspace, system_prompt, auto_base)

        if "then" in ap:
            _run(
                workspace,
                system_prompt,
                ap["then"],
                auto_base,
                exec_claude=exec_claude,
                review_loop=review_loop,
            )
