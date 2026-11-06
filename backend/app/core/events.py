"""
Server-Sent Events (SSE) Pub/Sub Manager (Phase 5D).
Provides lightweight, asynchronous, in-memory event dispatching for live run progress.

Architecture & Concurrency Invariant:
This event broker is designed for single-process asynchronous FastAPI deployments.
Subscriber queues and replay buffers are maintained in-memory within the active Python process.
In a multi-worker deployment (e.g. multiple Uvicorn worker processes), a shared distributed
message broker (such as Redis Pub/Sub) would be required to broadcast across workers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

from backend.app.models.base import utc_now
from backend.app.schemas.sanitizer import ReportDataSanitizer

logger = logging.getLogger(__name__)

TERMINAL_EVENT_TYPES: Set[str] = {"run_completed", "run_failed", "run_cancelled"}


def sanitize_event_data(data: Any) -> Any:
    """
    Recursively scrubs sensitive keys, tokens, passwords, cookies, and malformed URLs
    from all progress event payloads before serialization and dispatch.
    """
    if isinstance(data, dict):
        sanitized: Dict[str, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            if ReportDataSanitizer.SENSITIVE_KEY_PATTERN.search(key_str):
                sanitized[key_str] = "[REDACTED]"
            elif key_str.lower() in ("url", "uri", "endpoint_url") and isinstance(v, str):
                sanitized[key_str] = ReportDataSanitizer.sanitize_url(v) or "[INVALID_URL_REDACTED]"
            elif isinstance(v, str):
                sanitized[key_str] = ReportDataSanitizer.sanitize_text(v)
            elif isinstance(v, dict):
                sanitized[key_str] = sanitize_event_data(v)
            elif isinstance(v, (list, tuple)):
                sanitized[key_str] = [
                    sanitize_event_data(item)
                    if isinstance(item, dict)
                    else (ReportDataSanitizer.sanitize_text(item) if isinstance(item, str) else item)
                    for item in v
                ]
            else:
                sanitized[key_str] = v
        return sanitized
    elif isinstance(data, str):
        return ReportDataSanitizer.sanitize_text(data)
    elif isinstance(data, (list, tuple)):
        return [sanitize_event_data(item) for item in data]
    return data


@dataclass(frozen=True)
class SSEEvent:
    """Structured immutable Server-Sent Event."""
    event_id: str
    run_id: str
    event_type: str
    timestamp: str
    data: Dict[str, Any]

    def format_sse(self) -> str:
        """Formats the event into standard text/event-stream protocol wire format."""
        payload = json.dumps(
            {
                "run_id": self.run_id,
                "event_type": self.event_type,
                "timestamp": self.timestamp,
                "data": self.data,
            },
            separators=(",", ":"),
        )
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {payload}\n\n"


class EventManager:
    """
    In-memory Pub/Sub broker managing active run progress event streams,
    subscriber queues, historical event replay, and periodic heartbeats.
    """

    def __init__(
        self,
        max_history_per_run: int = 500,
        heartbeat_interval_seconds: float = 15.0,
    ) -> None:
        self._subscribers: Dict[str, Set[asyncio.Queue[Optional[SSEEvent]]]] = {}
        self._history: Dict[str, List[SSEEvent]] = {}
        self._max_history = max_history_per_run
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._counter: int = 0
        self._lock = asyncio.Lock()

    async def publish(
        self,
        run_id: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> SSEEvent:
        """
        Constructs, sanitizes, caches, and fans out a progress event to all active subscribers.
        """
        clean_data = sanitize_event_data(data or {})

        async with self._lock:
            self._counter += 1
            event = SSEEvent(
                event_id=str(self._counter),
                run_id=run_id,
                event_type=event_type,
                timestamp=utc_now().isoformat(),
                data=clean_data,
            )

            # Store in historical replay buffer
            if run_id not in self._history:
                self._history[run_id] = []
            hist = self._history[run_id]
            hist.append(event)
            if len(hist) > self._max_history:
                hist.pop(0)

            # Copy active subscriber queues
            active_queues = list(self._subscribers.get(run_id, set()))

        # Fan out non-blockingly to all subscriber queues
        for q in active_queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full for run %s; dropping event %s", run_id, event.event_id)

        return event

    async def subscribe(
        self,
        run_id: str,
        last_event_id: Optional[str] = None,
    ) -> AsyncGenerator[SSEEvent, None]:
        """
        Yields an asynchronous stream of SSEEvents for the specified run_id.
        Replays buffered historical events, then yields live events, periodically sending heartbeats.
        Cleans up the subscriber queue upon exit or client disconnect.
        """
        queue: asyncio.Queue[Optional[SSEEvent]] = asyncio.Queue(maxsize=1000)
        replayed_events: List[SSEEvent] = []

        async with self._lock:
            if run_id not in self._subscribers:
                self._subscribers[run_id] = set()
            self._subscribers[run_id].add(queue)

            # Replay historical events
            history = self._history.get(run_id, [])
            if last_event_id is not None:
                # Find events strictly after last_event_id
                start_idx = 0
                for idx, ev in enumerate(history):
                    if ev.event_id == str(last_event_id):
                        start_idx = idx + 1
                        break
                replayed_events = history[start_idx:]
            else:
                replayed_events = list(history)

        try:
            # 1. Yield replayed events
            is_terminal = False
            for ev in replayed_events:
                yield ev
                if ev.event_type in TERMINAL_EVENT_TYPES:
                    is_terminal = True
                    break

            # If replayed events already reached terminal state, stop streaming
            if is_terminal:
                return

            # 2. Live event listening loop with heartbeats
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=self.heartbeat_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    # Emit heartbeat event to keep connection alive
                    hb_event = SSEEvent(
                        event_id="hb",
                        run_id=run_id,
                        event_type="heartbeat",
                        timestamp=utc_now().isoformat(),
                        data={"heartbeat": True},
                    )
                    yield hb_event
                    continue

                if event is None:
                    # Sent to signal stream termination
                    break

                yield event

                if event.event_type in TERMINAL_EVENT_TYPES:
                    break

        finally:
            # Cleanup subscriber queue
            async with self._lock:
                sub_set = self._subscribers.get(run_id)
                if sub_set is not None:
                    sub_set.discard(queue)
                    if not sub_set:
                        self._subscribers.pop(run_id, None)

    def _is_run_finished(self, run_id: str) -> bool:
        """Checks whether the historical event stream for run_id ended with a terminal event."""
        history = self._history.get(run_id, [])
        if not history:
            return False
        return history[-1].event_type in TERMINAL_EVENT_TYPES

    def get_history(self, run_id: str) -> List[SSEEvent]:
        """Returns a snapshot of historical events recorded for a run."""
        return list(self._history.get(run_id, []))

    def clear(self) -> None:
        """Clears all subscribers and history (primarily for tests)."""
        self._subscribers.clear()
        self._history.clear()
        self._counter = 0


# Global process-level event manager instance
event_manager = EventManager()
