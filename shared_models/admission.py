"""Single-process FIFO admission for the explicitly deployed CPU runners.

No inference or warmup: readiness uses Ollama's read-only /api/ps. Estimates
deliberately over-reserve RAM; they are not measurements of model allocation.
"""
from __future__ import annotations

import json
import os
import contextlib
import threading
import uuid
import asyncio
import inspect
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

GIB = 1024 ** 3
current_lease = ContextVar("inference_lease", default=None)
_lock = threading.Lock()
_leases: dict[str, "Lease"] = {}
_waiting = []
_boot = None
_ceiling = 0


class ResourceBusy(RuntimeError):
    pass


class PayloadLimitError(ValueError):
    pass


@dataclass(frozen=True)
class Lease:
    token: str
    model_ids: tuple[str, ...]
    cancel: object = field(default=None, repr=False, compare=False)

    def check(self):
        if self.cancel is not None and self.cancel():
            raise ResourceBusy("Operation cancelled.")


@dataclass(eq=False)
class _Ticket:
    model_ids: tuple[str, ...]
    cancel: object = None
    lease: Lease | None = None


@contextlib.contextmanager
def bind_lease(lease):
    token = current_lease.set(lease)
    try:
        yield
    finally:
        current_lease.reset(token)


def _policy_path():
    configured = os.environ.get("INFERENCE_CAPACITY_FILE")
    return Path(configured) if configured is not None else None


def _cgroups():
    root = Path("/sys/fs/cgroup")
    relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    current = (root / relative.lstrip("/")).resolve()
    if root not in (current, *current.parents):
        current = root
    while current == root or root in current.parents:
        yield current
        if current == root:
            break
        current = current.parent


def memory():
    """Total budget and currently available bytes, including ancestor cgroup v2 limits."""
    fields = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemTotal:", "MemAvailable:"))}
    total, available = fields["MemTotal"], fields["MemAvailable"]
    for group in _cgroups():
        maximum = (group / "memory.max").read_text().strip()
        if maximum != "max":
            maximum = int(maximum)
            total = min(total, maximum)
            available = min(available, max(0, maximum - int((group / "memory.current").read_text())))
    return total, available


def cpu_capacity():
    cores = len(os.sched_getaffinity(0))
    for group in _cgroups():
        quota, period = (group / "cpu.max").read_text().split()
        if quota != "max":
            cores = min(cores, max(1, int(quota) // int(period)))
    return max(1, cores)


def _profile():
    global _boot, _ceiling
    value = json.loads(_policy_path().read_text())
    if (value["base_url"] != "http://127.0.0.1:11434/v1"
            or any(type(value[name]) is not int or value[name] <= 0
                   for name in ("ceiling", "reserve_bytes", "lease_bytes", "context_limit", "output_limit"))
            or not isinstance(value["models"], dict) or not value["models"]):
        raise ValueError()
    for item in value["models"].values():
        if type(item["slots"]) is not int or not 1 <= item["slots"] <= value["ceiling"] or not isinstance(item["digest"], str):
            raise ValueError()
    if value["boot"] != _boot:
        if _leases:
            raise ResourceBusy("Inference restarted; wait for active exchanges to finish.")
        _boot, _ceiling = value["boot"], value["ceiling"]
    return value


def _ready(profile):
    # Fixed loopback URL, no system proxy, bounded response, no credentials.
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:11434/api/ps", timeout=0.5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError()
    rows = json.loads(raw)["models"]
    loaded = {row["name"]: row for row in rows}
    for name, spec in profile["models"].items():
        row = loaded.get(name)
        if (row is None or row.get("digest", "").removeprefix("sha256:") != spec["digest"]
                or type(row.get("context_length")) is not int
                or not spec["context_length"] <= row["context_length"] <= spec["context_length"] * spec["slots"]):
            return False
    return True


def _status():
    global _ceiling
    result = {"ceiling": _ceiling, "active": len(_leases), "available_bytes": 0,
              "reserved_bytes": 0, "ready": False, "reason": "Inference capacity is not configured.",
              "context_limit": 4096, "output_limit": 512,
              "queued": len(_waiting), "queue_limit": None}
    try:
        profile = _profile()
        total, available = memory()
        reserve = max(profile["reserve_bytes"], GIB, total // 5)
        reserved = len(_leases) * profile["lease_bytes"]
        slots = max(len(_leases), len(_leases) + max(0, available - reserve - reserved) // profile["lease_bytes"])
        _ceiling = min(profile["ceiling"], slots)
        result.update(ceiling=_ceiling, available_bytes=available, reserved_bytes=reserved,
                      context_limit=profile["context_limit"], output_limit=profile["output_limit"],
                      queued=len(_waiting), queue_limit=max(1, profile["ceiling"] * 8))
        if not _ready(profile):
            result["reason"] = "Load both configured models explicitly before accepting requests."
        elif not _ceiling or available < reserve + reserved:
            result["reason"] = "Waiting for sufficient available RAM."
        else:
            result.update(ready=True, reason="")
        return profile, result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration, ResourceBusy):
        return None, result


def limits():
    if _policy_path() is None:
        return {"ceiling": None, "active": 0, "available_bytes": None, "reserved_bytes": 0,
                "ready": True, "reason": "", "context_limit": None, "output_limit": None,
                "queued": 0, "queue_limit": None}
    with _lock:
        return _status()[1]


def _enqueue(model_ids, cancel=None):
    with _lock:
        try:
            profile = _profile()
        except (OSError, ValueError, KeyError, TypeError, ResourceBusy):
            raise ResourceBusy("Inference capacity is not configured.") from None
        names = tuple(sorted(set(name for name in model_ids if name)))
        if not names or any(name not in profile["models"] for name in names):
            raise ResourceBusy("The selected model is not in the deployed capacity profile.")
        if len(_waiting) >= profile["ceiling"] * 8:
            raise ResourceBusy("The inference queue is full; retry later.")
        ticket = _Ticket(names, cancel)
        _waiting.append(ticket)
        return ticket


def _try_start(ticket):
    with _lock:
        if ticket not in _waiting:
            return ticket.lease, {"position": 0, "reason": ""}
        position = _waiting.index(ticket) + 1
        if position != 1:
            return None, {"position": position, "reason": "Waiting in FIFO order."}
        profile, status = _status()
        if profile is None:
            raise ResourceBusy(status["reason"])
        if any(name not in profile["models"] for name in ticket.model_ids):
            raise ResourceBusy("The capacity profile no longer includes the selected model.")
        if not status["ready"] or len(_leases) >= _ceiling:
            return None, {"position": 1, "reason": status["reason"] or "Waiting for a free inference slot."}
        for name in ticket.model_ids:
            if sum(name in lease.model_ids for lease in _leases.values()) >= profile["models"][name]["slots"]:
                return None, {"position": 1, "reason": "Waiting for the selected model runner."}
        ticket.lease = Lease(uuid.uuid4().hex, ticket.model_ids, ticket.cancel)
        _leases[ticket.lease.token] = ticket.lease
        _waiting.remove(ticket)
        return ticket.lease, {"position": 0, "reason": ""}


def _withdraw(ticket):
    with _lock:
        if ticket in _waiting:
            _waiting.remove(ticket)
        if ticket.lease is not None:
            _leases.pop(ticket.lease.token, None)


def acquire(model_ids: tuple[str, ...], purpose: str, *, cancel=None, on_queue=None) -> Lease:
    """Worker counterpart of acquire_async, sharing the same finite FIFO."""
    if _policy_path() is None:
        return Lease("local", tuple(model_ids), cancel)
    ticket = _enqueue(model_ids, cancel)
    previous = None
    try:
        while True:
            if cancel is not None and cancel():
                raise ResourceBusy("Queued operation cancelled.")
            lease, state = _try_start(ticket)
            if lease is not None:
                return lease
            if on_queue is not None and state != previous:
                on_queue(state)
            previous = state
            time.sleep(0.2)
    except BaseException:
        _withdraw(ticket)
        raise


async def acquire_async(model_ids: tuple[str, ...], purpose: str, *, cancel=None, on_queue=None) -> Lease:
    if _policy_path() is None:
        return Lease("local", tuple(model_ids), cancel)
    ticket = _enqueue(model_ids, cancel)
    previous = None
    try:
        while True:
            if cancel is not None and cancel():
                raise ResourceBusy("Queued operation cancelled.")
            # Read-only readiness I/O is bounded and never blocks the event loop.
            lease, state = await asyncio.to_thread(_try_start, ticket)
            if lease is not None:
                return lease
            if on_queue is not None and state != previous:
                notified = on_queue(state)
                if inspect.isawaitable(notified):
                    await notified
            previous = state
            await asyncio.sleep(0.2)
    except BaseException:
        _withdraw(ticket)
        raise


def release(lease: Lease):
    if lease.token == "local":
        return
    with _lock:
        _leases.pop(lease.token, None)


@contextlib.contextmanager
def slot(model_ids, purpose):
    """Reuse an exchange/worker reservation, or own one standalone HTTP call."""
    held = current_lease.get()
    lease = held or acquire(tuple(model_ids), purpose)
    try:
        lease.check()
        if any(name not in lease.model_ids for name in model_ids):
            raise ResourceBusy("The current reservation does not cover this model.")
        with bind_lease(lease):
            yield lease
    finally:
        if held is None:
            release(lease)


def validate_payload(payload):
    """Conservative UTF-8 byte upper bound, including JSON/framing and output.

    This is an admission estimate, not an exact tokenizer. Never truncate input
    or mutate user parameters to make a request fit.
    """
    from . import endpoint
    held = current_lease.get()
    if held is not None:
        held.check()
    if _policy_path() is None:
        return
    with _lock:
        try:
            profile = _profile()
        except (OSError, ValueError, KeyError, TypeError, ResourceBusy):
            raise ResourceBusy("Inference capacity is not configured.") from None
    if endpoint() != profile["base_url"] or payload.get("model") not in profile["models"]:
        raise ResourceBusy("The selected connection/model is outside the deployed capacity profile.")
    if held is not None and payload.get("model") not in held.model_ids:
        raise ResourceBusy("This exchange has no reservation for the requested model.")
    if payload.get("n", 1) != 1:
        raise PayloadLimitError("Only one completion is supported per reservation.")
    if any(name in payload for name in ("options", "num_ctx", "num_predict", "context_window", "keep_alive", "max_completion_tokens")):
        raise PayloadLimitError("Request overrides of the deployed context/output policy are not supported.")
    output = 0 if "input" in payload else payload.get("max_tokens", profile["output_limit"])
    if type(output) is not int or ("input" not in payload and not 1 <= output <= profile["output_limit"]):
        raise PayloadLimitError("Requested max_tokens exceeds the deployed output limit.")
    estimated = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256 + output
    context = min(profile["context_limit"], profile["models"][payload["model"]]["context_length"])
    if estimated > context:
        raise PayloadLimitError("Full request exceeds the conservative context estimate; shorten it explicitly.")
