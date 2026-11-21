"""Session-scoped source registry, task lifecycle, and one emission gateway."""

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

from .clock import AgentClock
from .types import EventGenerator
from .restoration import decode_registration
from .timer import TimerGenerator
from .program import ProgramGenerator, ProgramRateLimited, validate_emission


class EventGeneratorManager:
    MAX_ACTIVE = 64
    MAX_FINISHED = 256

    def __init__(self, enqueue: Callable[[dict], Awaitable[str | None]],
                 trace: Callable[[str, dict], None],
                 on_change: Callable[[], Awaitable[None]],
                 program_root: Path | None = None,
                 pending_program_events: Callable[[], int] | None = None, *, clock_path: Path | None = None) -> None:
        self.clock = AgentClock(clock_path)
        self.sandbox = None
        self._enqueue = enqueue
        self._trace = trace
        self._on_change = on_change
        self._records: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._generators: dict[str, EventGenerator | None] = {}
        self.closed = False
        self.program_root = (program_root or Path.cwd() / ".harness" / "programs").resolve()
        self._pending_program_events = pending_program_events or (lambda: 0)

    @property
    def has_active(self) -> bool:
        return not self.closed and any(record["status"] == "active" for record in self._records.values())

    def ensure_capacity(self) -> None:
        if self.closed:
            raise ValueError("Event generator manager is closed")
        if sum(r["status"] == "active" for r in self._records.values()) >= self.MAX_ACTIVE:
            raise ValueError("Active event generator limit reached")

    @staticmethod
    def metadata(name, notes):
        metadata = {}
        for key, value, limit in (("name", name, 128), ("notes", notes, 1024)):
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError(f"{key} must be nonempty text up to {limit} characters")
                metadata[key] = value.strip()
        return metadata

    def register(self, generator: EventGenerator, *, name: str | None = None,
                 notes: str | None = None, resource: str | None = None) -> dict:
        metadata = self.metadata(name, notes)
        if resource is not None:
            metadata["resource"] = resource
        self.ensure_capacity()
        self._prune_finished()
        ident = f"gen_{uuid4().hex}"
        if isinstance(generator, ProgramGenerator):
            generator.sandbox = self.sandbox
            generator.bind(ident, self.program_root)
        record = {**metadata, "generator_id": ident, "kind": generator.kind, "status": "active",
                  "configuration": deepcopy(generator.describe()), "events_emitted": 0,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        self._records[ident] = record
        self._generators[ident] = generator
        self._tasks[ident] = asyncio.create_task(self._run(ident, generator))
        self._trace("generator_created", {"generator": deepcopy(record)})
        return self._display(record)

    def _prune_finished(self) -> None:
        finished = [key for key, value in self._records.items() if value["status"] != "active" and "resource" not in value]
        for key in finished[:max(0, len(finished) - self.MAX_FINISHED)]:
            self._records.pop(key)
            self._generators.pop(key, None)

    def checkpoint(self) -> list[dict]:
        return [{"record": deepcopy(record), "state": (
                    self._generators[ident].checkpoint()
                    if isinstance(self._generators.get(ident), TimerGenerator) else None)}
                for ident, record in self._records.items()]

    def restore(self, snapshots: list[dict]) -> None:
        if self._records or self._tasks or self.closed:
            raise ValueError("Restore requires an empty open generator manager")
        decoded = [decode_registration(snapshot) for snapshot in snapshots]
        identifiers = [record["generator_id"] for record, _ in decoded]
        if (len(set(identifiers)) != len(identifiers)
                or sum(record["status"] == "active" for record, _ in decoded) > self.MAX_ACTIVE
                or sum(record["status"] != "active" and "resource" not in record for record, _ in decoded) > self.MAX_FINISHED):
            raise ValueError("Invalid generator checkpoint size or duplicate ID")
        for record, generator in decoded:
            ident = record["generator_id"]
            if isinstance(generator, ProgramGenerator):
                generator.sandbox = self.sandbox
                generator.bind(ident, self.program_root)
                record["configuration"] = deepcopy(generator.describe())
            self._records[ident] = record
            self._generators[ident] = generator

    async def start_restored(self) -> None:
        # Called inside the Session's event loop, not during synchronous construction.
        if self.closed:
            return
        for ident, record in list(self._records.items()):
            if record["status"] != "active" or ident in self._tasks:
                continue
            generator = self._generators[ident]
            if generator is None:
                await self._emit(ident, "generator_unavailable", {
                    "kind": record["kind"], "configuration": deepcopy(record["configuration"]),
                    "note": "This source's live dependency was not restored. Do not wait for it "
                            "or reuse its old command handle; inspect the task before recreating it."})
                record["status"] = "failed"
                record["error"] = "Live dependency cannot be restored"
                self._trace("generator_failed", {"generator": deepcopy(record)})
            else:
                self._tasks[ident] = asyncio.create_task(self._run(ident, generator))
                self._trace("generator_restored", {"generator": deepcopy(record)})
        self._prune_finished()

    async def stop_programs_for_policy(self):
        active=[ident for ident,record in self._records.items() if record['kind']=='program' and record['status']=='active']
        for ident in active:
            task=self._tasks.get(ident)
            if task is not None:task.cancel()
        await asyncio.gather(*(self._tasks[i] for i in active if i in self._tasks),return_exceptions=True)
        return active

    async def restart_programs_for_policy(self, active):
        restarted=[];interrupted=[]
        for ident in active:
            record=self._records[ident];generator=self._generators[ident]
            if generator.mode=='once':
                await self._emit(ident,'generator_interrupted',{'outcome_unknown':True,'note':'Policy changed; one-shot source is not replayed.'})
                record['status']='failed';record['error']='Interrupted by filesystem policy change'
                interrupted.append(ident)
            else:
                generator.sandbox=self.sandbox
                self._tasks[ident]=asyncio.create_task(self._run(ident,generator))
                restarted.append(ident)
        return restarted,interrupted

    async def pause(self) -> None:
        """Stop producers without changing registration status/progress."""
        self.closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self._on_change()

    async def _emit_program(self, ident: str, generator: ProgramGenerator,
                            kind: str, data: dict) -> dict:
        if self.closed or self._records[ident]["status"] != "active":
            return {"status": "rejected", "reason": "generator_stopping"}
        validate_emission(generator.attempts, kind, data)
        if self._pending_program_events() >= 128:
            return {"status": "rejected", "reason": "queue_full"}
        event_id = await self._emit(ident, "program_event", {"event_kind": kind, "payload": data})
        return {"status": "accepted", "event_id": event_id}

    async def _emit(self, ident: str, kind: str, data: dict) -> str | None:
        record = self._records[ident]
        if self.closed or record["status"] != "active":
            return
        event = {"source": "event_generator", "generator_id": ident, "kind": kind,
                 "sequence": record["events_emitted"] + 1,
                 "occurred_at": datetime.now(self.clock.zone).isoformat(), "data": deepcopy(data)}
        event_id = await self._enqueue(event)
        record["events_emitted"] += 1
        self._trace("generator_fired", {"generated_event": event})
        return event_id

    async def _run(self, ident: str, generator: EventGenerator) -> None:
        record = self._records[ident]
        try:
            await generator.run(lambda kind, data: (
                self._emit_program(ident, generator, kind, data)
                if isinstance(generator, ProgramGenerator) else self._emit(ident, kind, data)))
            if record["status"] == "active":
                record["status"] = "completed"
                self._trace("generator_completed", {"generator": deepcopy(record)})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            limited = isinstance(error, ProgramRateLimited)
            try:
                await self._emit(ident, "generator_rate_limited" if limited else "generator_failed",
                    {"error": record["error"], "configuration": deepcopy(record["configuration"]),
                     "note": ("Inspect/fix the program, then explicitly restart it. No automatic retry."
                              if isinstance(generator, ProgramGenerator) else
                              "Inspect this source and reassess the task. No automatic retry.")})
            except Exception:
                # A failed/closing Session may no longer accept events. Trace remains.
                pass
            record["status"] = "paused" if limited else "failed"
            self._trace("generator_paused" if limited else "generator_failed", {"generator": deepcopy(record)})
        finally:
            self._tasks.pop(ident, None)
            if record["status"] != "active" and not isinstance(generator, (TimerGenerator, ProgramGenerator)):
                self._generators[ident] = None  # Do not retain completed process buffers.
            self._prune_finished()
            await self._on_change()

    def _display(self, record):
        view = deepcopy(record)
        view["created_at"] = datetime.fromisoformat(record["created_at"]).astimezone(self.clock.zone).isoformat()
        timer = self._generators.get(record["generator_id"])
        if record["status"] == "active" and isinstance(timer, TimerGenerator) and timer.next_fire_at is not None:
            view["next_fire_at"] = datetime.fromtimestamp(timer.next_fire_at, self.clock.zone).isoformat()
        return view

    def inventory(self) -> list[dict]:
        fields = ("generator_id", "kind", "status", "name", "notes", "resource")
        return [{key: record[key] for key in fields if key in record}
                for record in self._records.values()
                if record["status"] in {"active", "paused", "failed"}]

    def list(self, include_finished: bool = False) -> list[dict]:
        if type(include_finished) is not bool:
            raise ValueError("include_finished must be a boolean")
        return [self._display(r) for r in self._records.values()
                         if include_finished or r["status"] in {"active", "paused"}]

    def check_agent_control(self, generator_id):
        if self._records.get(generator_id, {}).get("resource"):
            raise ValueError("This event generator is managed by its suite")

    async def retire_resource(self, ident):
        await self.cancel(ident)
        self._records.pop(ident, None)
        self._generators.pop(ident, None)
        self._tasks.pop(ident, None)

    async def restart_program(self, generator_id: str) -> dict:
        if not isinstance(generator_id, str) or generator_id not in self._records:
            raise ValueError("Unknown event generator_id")
        record = self._records[generator_id]
        if record["kind"] != "program" or record["status"] not in {"paused", "failed", "completed"}:
            raise ValueError("Only paused/failed/completed programs can be restarted")
        self.ensure_capacity()
        generator = ProgramGenerator.restore(record["configuration"])
        generator.sandbox = self.sandbox
        generator.bind(generator_id, self.program_root)
        self._generators[generator_id] = generator
        record["status"] = "active"
        record.pop("error", None)
        self._tasks[generator_id] = asyncio.create_task(self._run(generator_id, generator))
        self._trace("generator_restarted", {"generator": deepcopy(record)})
        return self._display(record)

    async def cancel(self, generator_id: str) -> dict:
        if not isinstance(generator_id, str) or generator_id not in self._records:
            raise ValueError("Unknown event generator_id")
        record = self._records[generator_id]
        if record["status"] in {"active", "paused"}:
            record["status"] = "cancelled"
            task = self._tasks.get(generator_id)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._tasks.pop(generator_id, None)
            if not isinstance(self._generators.get(generator_id), (TimerGenerator, ProgramGenerator)):
                self._generators[generator_id] = None
            self._trace("generator_cancelled", {"generator": deepcopy(record)})
            self._prune_finished()
            await self._on_change()
        return self._display(record)

    async def close(self) -> None:
        self.closed = True  # Fence emission before cancelling any source.
        active = [key for key, record in self._records.items() if record["status"] == "active"]
        await asyncio.gather(*(self.cancel(ident) for ident in active))
        await self._on_change()
