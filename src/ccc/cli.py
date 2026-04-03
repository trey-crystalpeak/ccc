"""CLI — thin client that talks to the daemon over a Unix socket."""

import argparse
import json
import os
import socket
import sys
import textwrap
import time

from .workspace import SOCKET_PATH


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
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def format_list(result: dict) -> None:
    workspaces = result["workspaces"]
    if not workspaces:
        print("No workspaces.")
        return
    rows = []
    for ws in workspaces:
        rows.append([
            ws["id"],
            ws["status"],
            ws["name"] or "(naming...)",
            relative_time(ws["updated_at"]),
            ws["project_path"],
        ])
    print_table(["ID", "STATUS", "NAME", "UPDATED", "PROJECT"], rows)


def format_status(result: dict) -> None:
    header = f"{result['name'] or result['id']}  [{result['status']}]"
    if result.get("total_cost_usd"):
        header += f"  ${result['total_cost_usd']:.4f}"
    print(header)

    if result.get("error_message"):
        print()
        print(textwrap.indent(result["error_message"], "  "))

    task = result.get("task")
    if not task:
        return

    print()
    print(textwrap.indent(task["prompt"], "  "))

    messages = task.get("messages", [])
    if messages:
        print("\n--- Follow-up ---")
        for i, msg in enumerate(messages, 1):
            prefix = f"  {i}. "
            padding = " " * len(prefix)
            lines = msg.splitlines()
            if lines:
                print(prefix + lines[0])
                for line in lines[1:]:
                    print(padding + line if line else "")
            else:
                print(prefix)
            if i < len(messages):
                print()

    if task.get("reviewer_comment"):
        print("\n--- Review ---")
        print(textwrap.indent(task["reviewer_comment"], "  "))

    if task.get("summary"):
        print("\n--- Summary ---")
        print(textwrap.indent(task["summary"], "  "))


def main():
    parser = argparse.ArgumentParser(prog="ccc")
    sub = parser.add_subparsers(dest="command")

    # ccc daemon start|stop
    daemon_parser = sub.add_parser("daemon")
    daemon_parser.add_argument("action", choices=["start", "stop"])

    # ccc setup [--login-only] [--git-only]
    setup_parser = sub.add_parser("setup")
    setup_parser.add_argument("--login-only", action="store_true")
    setup_parser.add_argument("--git-only", action="store_true")

    # ccc new <path> <prompt>
    new_parser = sub.add_parser("new")
    new_parser.add_argument("path")
    new_parser.add_argument("prompt")

    # ccc list [--cwd]
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--cwd", action="store_true")

    # ccc status <id>
    status_parser = sub.add_parser("status")
    status_parser.add_argument("id")

    # ccc send <id> <message>
    send_parser = sub.add_parser("send")
    send_parser.add_argument("id")
    send_parser.add_argument("message")

    # ccc merge <id>
    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("id")

    # ccc delete <id>
    delete_parser = sub.add_parser("delete")
    delete_parser.add_argument("id")

    # ccc logs <id>
    logs_parser = sub.add_parser("logs")
    logs_parser.add_argument("id")

    # ccc diff <id>
    diff_parser = sub.add_parser("diff")
    diff_parser.add_argument("id")

    # ccc sync <id>
    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("id")

    # ccc cleanup
    sub.add_parser("cleanup")

    args = parser.parse_args()

    if not args.command:
        result = send_command({"command": "list"})
        if not result["ok"]:
            print_error(result)
        format_list(result)
        return

    if args.command == "daemon":
        if args.action == "start":
            from .daemon import Daemon
            Daemon().serve()
        else:
            send_command({"command": "stop"})
            print("Daemon stopped.")
        return

    if args.command == "setup":
        from .setup import run_setup
        run_setup(login_only=args.login_only, git_only=args.git_only)
        return

    if args.command == "new":
        result = send_command({"command": "new", "path": os.path.abspath(args.path), "prompt": args.prompt})
        if not result["ok"]:
            print_error(result)
        print(f"Created workspace {result['id']}")

    elif args.command == "list":
        request = {"command": "list"}
        if args.cwd:
            request["cwd"] = os.getcwd()
        result = send_command(request)
        if not result["ok"]:
            print_error(result)
        format_list(result)

    elif args.command == "status":
        result = send_command({"command": "status", "id": args.id})
        if not result["ok"]:
            print_error(result)
        format_status(result)

    elif args.command == "send":
        result = send_command({"command": "send", "id": args.id, "message": args.message})
        if not result["ok"]:
            print_error(result)
        print("Sent.")

    elif args.command == "merge":
        result = send_command({"command": "merge", "id": args.id})
        if not result["ok"]:
            print_error(result)
        if result.get("merged"):
            print("Merged.")
        else:
            print("Conflict — resolving...")

    elif args.command == "delete":
        result = send_command({"command": "delete", "id": args.id})
        if not result["ok"]:
            print_error(result)
        print("Deleted.")

    elif args.command == "diff":
        result = send_command({"command": "diff", "id": args.id})
        if not result["ok"]:
            print_error(result)
        print(result.get("diff", ""))

    elif args.command == "logs":
        result = send_command({"command": "logs", "id": args.id})
        if not result["ok"]:
            print_error(result)
        print(result.get("logs", ""))

    elif args.command == "sync":
        result = send_command({"command": "sync", "id": args.id})
        if not result["ok"]:
            print_error(result)
        print(f"Synced. Tell the agent to rebase: ccc send {args.id} 'Rebase on host-main'")

    elif args.command == "cleanup":
        result = send_command({"command": "cleanup"})
        if not result["ok"]:
            print_error(result)
        removed = result.get("removed", [])
        orphaned = result.get("orphaned", 0)
        if removed:
            for ws in removed:
                print(f"Removed: {ws['name']} ({ws['id']})")
        if orphaned:
            print(f"Removed {orphaned} orphaned director{'y' if orphaned == 1 else 'ies'}.")
        if not removed and not orphaned:
            print("Nothing to clean up.")


if __name__ == "__main__":
    main()
