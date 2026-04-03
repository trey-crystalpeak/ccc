"""Daemon — Unix socket server that manages workspaces."""

import json
import os
import shutil
import socket
import subprocess
import threading
from typing import Optional

from .container import ensure_system
from .orchestrator import create_task, resume_task, resolve_conflicts
from .state import Event, Status, Task
from .workspace import Workspace, BASE_DIR, WORKSPACES_DIR, SOCKET_PATH


NOTIFICATION_MESSAGES = {
    Status.PENDING_HAS_CHANGES: "{name} is done — ready for review",
    Status.PENDING_NEEDS_INPUT: "{name} needs your input",
    Status.IDLE_ANSWERED:       "{name} answered your question",
    Status.ERROR:               "{name} hit an error",
}


def _notify(title: str, message: str) -> None:
    """Send a macOS desktop notification. Best-effort, never raises."""
    try:
        def _osa_quote(s: str) -> str:
            return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
        script = f"display notification {_osa_quote(message)} with title {_osa_quote(title)}"
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


class Daemon:

    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.active_threads: set[str] = set()
        self.running = False
        self._caffeinate: Optional[subprocess.Popen] = None

    def _start_caffeinate(self) -> None:
        if self._caffeinate is None:
            try:
                self._caffeinate = subprocess.Popen(
                    ["caffeinate", "-dius"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def _stop_caffeinate(self) -> None:
        if self._caffeinate is not None:
            self._caffeinate.terminate()
            self._caffeinate = None

    def _spawn(self, workspace: Workspace, target, *args) -> None:
        def wrapper():
            self.active_threads.add(workspace.id)
            self._start_caffeinate()
            try:
                target(*args)
            finally:
                self.active_threads.discard(workspace.id)
                if not self.active_threads:
                    self._stop_caffeinate()
                name = workspace.state.name or workspace.id
                message = NOTIFICATION_MESSAGES.get(workspace.state.status)
                if message:
                    _notify("ccc", message.format(name=name))

        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()

    def _get_workspace(self, id: str, check_busy: bool = False):
        """Look up a workspace. Returns (workspace, None) or (None, error_response)."""
        ws = self.workspaces.get(id)
        if not ws:
            return None, {"ok": False, "error": "Workspace not found"}
        if check_busy and ws.id in self.active_threads:
            return None, {"ok": False, "error": "Workspace is busy"}
        return ws, None

    def handle_new(self, path: str, prompt: str) -> dict:
        workspace = Workspace(path)
        workspace.create(prompt)
        self.workspaces[workspace.id] = workspace
        self._spawn(workspace, create_task, workspace)
        return {"ok": True, "id": workspace.id, "name": workspace.state.name}

    def handle_list(self, cwd: Optional[str] = None) -> dict:
        results = []
        for ws in self.workspaces.values():
            if cwd and ws.state.project_path != cwd:
                continue
            results.append({
                "id": ws.id,
                "name": ws.state.name,
                "status": ws.state.status.value,
                "project_path": ws.state.project_path,
                "total_cost_usd": ws.state.total_cost_usd,
                "updated_at": ws.state.updated_at,
            })
        return {"ok": True, "workspaces": results}

    def handle_status(self, id: str) -> dict:
        ws, error = self._get_workspace(id)
        if error:
            return error
        task_data = None
        if ws.state.tasks:
            task = ws.state.current_task
            task_data = {
                "prompt": task.prompt,
                "completed": task.completed,
                "summary": task.summary,
                "reviewer_comment": task.reviewer_comment,
                "messages": task.messages,
            }
        return {
            "ok": True,
            "id": ws.id,
            "name": ws.state.name,
            "status": ws.state.status.value,
            "total_cost_usd": ws.state.total_cost_usd,
            "error_message": ws.state.error_message or None,
            "task": task_data,
        }

    def handle_send(self, id: str, message: str) -> dict:
        ws, error = self._get_workspace(id, check_busy=True)
        if error:
            return error

        # If the current task is done, start a new one. The new Task
        # becomes current_task (tasks[-1]), and the orchestrator picks
        # it up from there.
        if ws.state.current_task.completed:
            ws.state.tasks.append(Task(prompt=message))
        else:
            ws.state.current_task.messages.append(message)

        ws.handle_event(Event.USER_MESSAGE)
        self._spawn(ws, resume_task, ws, message)
        return {"ok": True}

    def handle_merge(self, id: str) -> dict:
        ws, error = self._get_workspace(id, check_busy=True)
        if error:
            return error

        if ws.state.status == Status.IDLE:
            return {"ok": True, "merged": True}

        ws.handle_event(Event.USER_MERGES)

        if ws.merge_from_container():
            ws.state.current_task.completed = True
            ws.handle_event(Event.MERGE_SUCCEEDED)
            return {"ok": True, "merged": True}
        else:
            self._spawn(ws, resolve_conflicts, ws)
            return {"ok": True, "merged": False, "conflict": True}

    def handle_delete(self, id: str) -> dict:
        ws, error = self._get_workspace(id, check_busy=True)
        if error:
            return error
        ws.delete()
        del self.workspaces[id]
        return {"ok": True}

    def handle_diff(self, id: str) -> dict:
        ws, error = self._get_workspace(id)
        if error:
            return error
        return {"ok": True, "diff": ws.diff()}

    def handle_logs(self, id: str) -> dict:
        ws, error = self._get_workspace(id)
        if error:
            return error
        return {"ok": True, "logs": ws.logs() or "No logs yet."}

    def handle_cleanup(self) -> dict:
        terminal = {Status.IDLE, Status.IDLE_ANSWERED, Status.ERROR}
        removed = []
        for id in list(self.workspaces):
            ws = self.workspaces[id]
            if ws.state.status in terminal:
                name = ws.state.name or id
                ws.delete()
                del self.workspaces[id]
                removed.append({"id": id, "name": name})

        # Remove orphaned workspace directories not tracked by the daemon
        orphaned = 0
        if WORKSPACES_DIR.exists():
            known_paths = {ws.path for ws in self.workspaces.values()}
            for project_dir in WORKSPACES_DIR.iterdir():
                if not project_dir.is_dir():
                    continue
                for ws_dir in project_dir.iterdir():
                    if ws_dir.is_dir() and ws_dir not in known_paths:
                        shutil.rmtree(ws_dir)
                        orphaned += 1
                if project_dir.is_dir() and not any(project_dir.iterdir()):
                    project_dir.rmdir()

        return {"ok": True, "removed": removed, "orphaned": orphaned}

    def handle_sync(self, id: str) -> dict:
        ws, error = self._get_workspace(id)
        if error:
            return error
        ws.sync_from_host()
        return {"ok": True}

    def dispatch(self, request: dict) -> dict:
        command = request.get("command")
        if command == "new":
            return self.handle_new(request["path"], request["prompt"])
        elif command == "list":
            return self.handle_list(request.get("cwd"))
        elif command == "status":
            return self.handle_status(request["id"])
        elif command == "send":
            return self.handle_send(request["id"], request["message"])
        elif command == "merge":
            return self.handle_merge(request["id"])
        elif command == "delete":
            return self.handle_delete(request["id"])
        elif command == "diff":
            return self.handle_diff(request["id"])
        elif command == "logs":
            return self.handle_logs(request["id"])
        elif command == "cleanup":
            return self.handle_cleanup()
        elif command == "sync":
            return self.handle_sync(request["id"])
        elif command == "stop":
            self.running = False
            return {"ok": True}
        else:
            return {"ok": False, "error": f"Unknown command: {command}"}

    def recover(self) -> None:
        """Scan for existing workspaces and recover what we can."""
        if not WORKSPACES_DIR.exists():
            return
        state_files = list(WORKSPACES_DIR.glob("*/*/state.json"))
        if state_files:
            print(f"Recovering {len(state_files)} workspace(s)...")
        for state_file in state_files:
            ws = Workspace.restore(state_file)
            self.workspaces[ws.id] = ws

    def _handle_connection(self, conn: socket.socket) -> None:
        with conn:
            try:
                line = conn.makefile().readline()
                request = json.loads(line)
                response = self.dispatch(request)
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            conn.sendall(json.dumps(response).encode() + b"\n")

    def serve(self) -> None:
        """Accept loop on Unix socket. Runs in foreground."""
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)

        ensure_system()
        self.recover()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(SOCKET_PATH)
        sock.listen(5)
        sock.settimeout(1.0)
        self.running = True
        print(f"Daemon listening on {SOCKET_PATH}")

        try:
            while self.running:
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                self._handle_connection(conn)
        finally:
            sock.close()
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
            print("Daemon stopped.")
