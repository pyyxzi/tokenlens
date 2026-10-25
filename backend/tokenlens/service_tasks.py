"""Bounded, cancellable local AI jobs. Never stores prompts or credentials."""
from __future__ import annotations

import re
import threading
import time
import uuid


class TaskRegistry:
    def __init__(self, limit=4):
        self.limit = limit
        self._lock = threading.RLock()
        self._tasks = {}

    def create(self, kind, task_id=""):
        task_id = task_id or uuid.uuid4().hex
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id):
            raise ValueError("invalid task_id")
        with self._lock:
            now = time.time()
            self._tasks = {k: v for k, v in self._tasks.items()
                           if not v.get("ended_at") or now - v["ended_at"] < 300}
            if task_id in self._tasks:
                raise ValueError("task_id already exists")
            if sum(not v.get("ended_at") for v in self._tasks.values()) >= self.limit:
                raise RuntimeError("too many active AI tasks; cancel or wait for an existing task")
            event = threading.Event()
            self._tasks[task_id] = {"task_id": task_id, "kind": kind,
                                    "started_at": now, "state": "running", "cancel": event}
        return task_id, event

    def finish(self, task_id, failed=False):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task["state"] = ("cancelled" if task["cancel"].is_set()
                                 else "failed" if failed else "finished")
                task["ended_at"] = time.time()

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.get("ended_at"):
                return False
            task["cancel"].set()
            task["state"] = "cancelling"
            return True

    def cancel_all(self):
        with self._lock:
            for task in self._tasks.values():
                if not task.get("ended_at"):
                    task["cancel"].set()

    def public(self):
        with self._lock:
            return [{k: v for k, v in task.items() if k != "cancel"}
                    for task in self._tasks.values()]
