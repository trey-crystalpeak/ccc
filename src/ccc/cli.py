"""CLI — thin client that talks to the daemon over a Unix socket."""

import argparse
import json
import os
import socket
import sys
import textwrap
import time

from .models import CleanupEntry, StatusDetail, WorkspaceSummary
from .workspace import SOCKET_PATH

# --- Display templates ---

STATUS_HEADER = "{label}  [{status}]{cost}"

STATUS_SECTION = """
--- {title} ---
{body}"""

# --- Helpers ---


def send_command(request: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        print("Daemon is not running. Start it with: ccc daemon start", file=sys.stderr)
        sys.exit(1)
    try:
        sock.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(sock.makefile().readline())
    finally:
        sock.close()


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


def print_error(result: dict) -> None:
    print(f"Error: {result['error']}", file=sys.stderr)
    sys.exit(1)


def relative_time(timestamp: float) -> str:
    delta = int(time.time() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --- Formatters ---


def _format_messages(messages: list[str]) -> str:
    parts = []
    for i, msg in enumerate(messages, 1):
        prefix = f"  {i}. "
        padding = " " * len(prefix)
        lines = msg.splitlines()
        if lines:
            formatted = prefix + lines[0]
            for line in lines[1:]:
                formatted += "\n" + (padding + line if line else "")
            parts.append(formatted)
        else:
            parts.append(prefix)
    return "\n\n".join(parts)


def format_list(workspaces: list[WorkspaceSummary]) -> None:
    if not workspaces:
        print("No workspaces.")
        return
    rows = [
        [ws.id, ws.status, ws.name or "(naming...)", relative_time(ws.updated_at), ws.project_path]
        for ws in workspaces
    ]
    print_table(["ID", "STATUS", "NAME", "UPDATED", "PROJECT"], rows)


def format_status(detail: StatusDetail) -> None:
    label = detail.name or detail.id
    cost = f"  ${detail.total_cost_usd:.4f}" if detail.total_cost_usd else ""
    print(STATUS_HEADER.format(label=label, status=detail.status, cost=cost))

    if detail.error_message:
        print()
        print(textwrap.indent(detail.error_message, "  "))

    if not detail.task:
        return

    print()
    print(textwrap.indent(detail.task.prompt, "  "))

    if detail.task.messages:
        print(
            STATUS_SECTION.format(
                title="Follow-up",
                body=_format_messages(detail.task.messages),
            )
        )

    if detail.task.reviewer_comment:
        print(
            STATUS_SECTION.format(
                title="Review",
                body=textwrap.indent(detail.task.reviewer_comment, "  "),
            )
        )

    if detail.task.summary:
        print(
            STATUS_SECTION.format(
                title="Summary",
                body=textwrap.indent(detail.task.summary, "  "),
            )
        )


# --- Command handlers ---


def _cmd_daemon(args):
    if args.action == "start":
        from .daemon import Daemon

        Daemon().serve()
    else:
        send_command({"command": "stop"})
        print("Daemon stopped.")


def _cmd_setup(args):
    from .setup import run_setup

    run_setup(login_only=args.login_only, git_only=args.git_only)


def _cmd_new(args):
    path = os.path.abspath(args.path)
    git_dir = os.path.join(path, ".git")
    if not os.path.isdir(git_dir):
        print(f"Error: {path} is not a git repository (no .git directory found)")
        sys.exit(1)
    request = {
        "command": "new",
        "path": path,
        "prompt": args.prompt,
    }
    if args.context:
        request["mounts"] = args.context
    result = send_command(request)
    if not result["ok"]:
        print_error(result)
    print(f"Created workspace {result['id']}")


def _cmd_list(args):
    request = {"command": "list"}
    if args.cwd:
        request["cwd"] = os.getcwd()
    result = send_command(request)
    if not result["ok"]:
        print_error(result)
    workspaces = [WorkspaceSummary(**ws) for ws in result["workspaces"]]
    format_list(workspaces)


def _cmd_status(args):
    result = send_command({"command": "status", "id": args.id})
    if not result["ok"]:
        print_error(result)
    format_status(StatusDetail.from_dict(result))


def _cmd_send(args):
    result = send_command({"command": "send", "id": args.id, "message": args.message})
    if not result["ok"]:
        print_error(result)
    print("Sent.")


def _cmd_merge(args):
    result = send_command({"command": "merge", "id": args.id})
    if not result["ok"]:
        print_error(result)
    print("Merged." if result.get("merged") else "Conflict — resolving...")


def _cmd_delete(args):
    result = send_command({"command": "delete", "id": args.id})
    if not result["ok"]:
        print_error(result)
    print("Deleted.")


def _cmd_diff(args):
    result = send_command({"command": "diff", "id": args.id})
    if not result["ok"]:
        print_error(result)
    print(result.get("diff", ""))


def _cmd_logs(args):
    result = send_command({"command": "logs", "id": args.id})
    if not result["ok"]:
        print_error(result)
    print(result.get("logs", ""))


def _cmd_sync(args):
    result = send_command({"command": "sync", "id": args.id})
    if not result["ok"]:
        print_error(result)
    print(f"Synced. Tell the agent to rebase: ccc send {args.id} 'Rebase on host-main'")


def _cmd_cleanup(args):
    result = send_command({"command": "cleanup"})
    if not result["ok"]:
        print_error(result)
    removed = [CleanupEntry(**r) for r in result.get("removed", [])]
    orphaned = result.get("orphaned", 0)
    for entry in removed:
        print(f"Removed: {entry.name} ({entry.id})")
    if orphaned:
        print(f"Removed {orphaned} orphaned director{'y' if orphaned == 1 else 'ies'}.")
    if not removed and not orphaned:
        print("Nothing to clean up.")


COMMANDS = {
    "daemon": _cmd_daemon,
    "setup": _cmd_setup,
    "new": _cmd_new,
    "list": _cmd_list,
    "status": _cmd_status,
    "send": _cmd_send,
    "merge": _cmd_merge,
    "delete": _cmd_delete,
    "diff": _cmd_diff,
    "logs": _cmd_logs,
    "sync": _cmd_sync,
    "cleanup": _cmd_cleanup,
}


# --- Argument parsing ---


def main():
    parser = argparse.ArgumentParser(prog="ccc")
    sub = parser.add_subparsers(dest="command")

    daemon_parser = sub.add_parser("daemon")
    daemon_parser.add_argument("action", choices=["start", "stop"])

    setup_parser = sub.add_parser("setup")
    setup_parser.add_argument("--login-only", action="store_true")
    setup_parser.add_argument("--git-only", action="store_true")

    new_parser = sub.add_parser("new")
    new_parser.add_argument("prompt")
    new_parser.add_argument("--path", default=".", help="Path to git repository (default: current directory)")
    new_parser.add_argument("--context", action="append", help="Include a file or directory as readonly context")

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--cwd", action="store_true")

    status_parser = sub.add_parser("status")
    status_parser.add_argument("id")

    send_parser = sub.add_parser("send")
    send_parser.add_argument("id")
    send_parser.add_argument("message")

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("id")

    delete_parser = sub.add_parser("delete")
    delete_parser.add_argument("id")

    logs_parser = sub.add_parser("logs")
    logs_parser.add_argument("id")

    diff_parser = sub.add_parser("diff")
    diff_parser.add_argument("id")

    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("id")

    sub.add_parser("cleanup")

    args = parser.parse_args()

    if not args.command:
        _cmd_list(argparse.Namespace(cwd=False))
        return

    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
