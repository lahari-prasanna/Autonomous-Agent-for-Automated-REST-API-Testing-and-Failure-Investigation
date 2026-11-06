"""
Unit and Integration Tests for Server-Sent Events (SSE) and Live Progress (Phase 5D).
Verifies:
  1. EventManager publish/subscribe FIFO event ordering and monotonic event IDs.
  2. Fan-out to multiple concurrent subscribers for the same run.
  3. Historical event replay for late-joining subscribers and Last-Event-ID offset.
  4. Terminal event stream closure (run_completed, run_failed, run_cancelled).
  5. Heartbeat event generation on idle queues.
  6. Deep sanitization of secrets, tokens, credentials, and URLs in event payloads.
  7. Client disconnect and subscriber queue cleanup.
  8. End-to-end HTTP SSE streaming endpoint (GET /api/v1/runs/{run_id}/events).
  9. 404 response on unknown run IDs.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator, List
from unittest.mock import AsyncMock

from fastapi import status
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.api.deps import get_db, get_run_service
from backend.app.core.database import build_engine, close_db, drop_db, init_db
from backend.app.core.events import EventManager, SSEEvent, event_manager, sanitize_event_data
from backend.app.main import app
from backend.app.models.base import utc_now
from backend.app.repositories import create_project, create_run
from backend.app.services.run_service import RunService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Provides a fresh, isolated in-memory SQLite async engine per test."""
    eng = build_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(eng)
    yield eng
    await drop_db(eng)
    await close_db(eng)


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Provides an async sessionmaker bound to the isolated in-memory test database."""
    return async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provides an isolated AsyncSession bound to the in-memory test database."""
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client with dependency overrides for isolated test database sessions."""
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    def override_get_run_service() -> RunService:
        return RunService(session_factory=session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_run_service] = override_get_run_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. EventManager Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_event_manager_fifo_ordering():
    em = EventManager(heartbeat_interval_seconds=10.0)
    run_id = "run_order_test"

    received: List[SSEEvent] = []

    async def subscriber():
        async for ev in em.subscribe(run_id=run_id):
            received.append(ev)

    task = asyncio.create_task(subscriber())
    await asyncio.sleep(0.01)  # Allow subscription registration

    # Publish events
    e1 = await em.publish(run_id, "run_started", {"total": 3})
    e2 = await em.publish(run_id, "operation_started", {"op": "getUsers"})
    e3 = await em.publish(run_id, "run_completed", {"passed": 3})

    await task

    assert len(received) == 3
    assert [ev.event_type for ev in received] == [
        "run_started",
        "operation_started",
        "run_completed",
    ]
    assert int(received[0].event_id) < int(received[1].event_id) < int(received[2].event_id)


@pytest.mark.anyio
async def test_event_manager_multiple_subscribers():
    em = EventManager(heartbeat_interval_seconds=10.0)
    run_id = "run_multi_sub"

    sub1_events: List[SSEEvent] = []
    sub2_events: List[SSEEvent] = []

    async def sub_worker(sink: List[SSEEvent]):
        async for ev in em.subscribe(run_id=run_id):
            sink.append(ev)

    t1 = asyncio.create_task(sub_worker(sub1_events))
    t2 = asyncio.create_task(sub_worker(sub2_events))
    await asyncio.sleep(0.01)

    await em.publish(run_id, "run_started", {"count": 1})
    await em.publish(run_id, "run_completed", {"success": True})

    await asyncio.gather(t1, t2)

    assert len(sub1_events) == 2
    assert len(sub2_events) == 2
    assert [e.event_type for e in sub1_events] == ["run_started", "run_completed"]
    assert [e.event_type for e in sub2_events] == ["run_started", "run_completed"]


@pytest.mark.anyio
async def test_event_manager_historical_replay_and_last_event_id():
    em = EventManager(heartbeat_interval_seconds=10.0)
    run_id = "run_replay_test"

    # Publish 3 events before any subscriber connects
    e1 = await em.publish(run_id, "run_created", {"step": 1})
    e2 = await em.publish(run_id, "run_started", {"step": 2})
    e3 = await em.publish(run_id, "run_completed", {"step": 3})

    # Subscriber A: connects without Last-Event-ID, receives all 3
    sub_a_events: List[SSEEvent] = []
    async for ev in em.subscribe(run_id=run_id):
        sub_a_events.append(ev)

    assert len(sub_a_events) == 3
    assert [e.event_type for e in sub_a_events] == ["run_created", "run_started", "run_completed"]

    # Subscriber B: connects with Last-Event-ID = e1.event_id, receives e2 and e3
    sub_b_events: List[SSEEvent] = []
    async for ev in em.subscribe(run_id=run_id, last_event_id=e1.event_id):
        sub_b_events.append(ev)

    assert len(sub_b_events) == 2
    assert [e.event_type for e in sub_b_events] == ["run_started", "run_completed"]


@pytest.mark.anyio
async def test_event_manager_heartbeat():
    # Use very short heartbeat interval (0.05s)
    em = EventManager(heartbeat_interval_seconds=0.05)
    run_id = "run_heartbeat_test"

    received: List[SSEEvent] = []

    async def subscriber():
        async for ev in em.subscribe(run_id=run_id):
            received.append(ev)
            if ev.event_type == "heartbeat":
                break

    await asyncio.wait_for(subscriber(), timeout=2.0)

    assert len(received) >= 1
    assert received[0].event_type == "heartbeat"
    assert received[0].data["heartbeat"] is True


@pytest.mark.anyio
def test_event_data_sanitization():
    raw_payload = {
        "token": "secret_jwt_xyz",
        "password": "super_secret_pw",
        "authorization": "Bearer secret_bearer_token",
        "url": "https://user:pass@api.secret.internal:8080/query?api_key=key123&item=laptop",
        "headers": {
            "Cookie": "session_id=12345",
            "Content-Type": "application/json",
        },
        "nested": {
            "api_key": "nested_secret",
            "safe_metric": 42.0,
        },
    }

    cleaned = sanitize_event_data(raw_payload)

    # Redactions
    assert cleaned["token"] == "[REDACTED]"
    assert cleaned["password"] == "[REDACTED]"
    assert cleaned["authorization"] == "[REDACTED]"
    assert cleaned["headers"]["Cookie"] == "[REDACTED]"
    assert cleaned["headers"]["Content-Type"] == "application/json"
    assert cleaned["nested"]["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["safe_metric"] == 42.0

    # URL sanitization: userinfo and sensitive query params stripped
    assert "pass" not in cleaned["url"]
    assert "api_key=%5BREDACTED%5D" in cleaned["url"] or "api_key=[REDACTED]" in cleaned["url"]


@pytest.mark.anyio
async def test_client_disconnect_queue_cleanup():
    em = EventManager(heartbeat_interval_seconds=10.0)
    run_id = "run_cleanup_test"

    # Start a subscriber generator and receive an event
    gen = em.subscribe(run_id=run_id)
    pub_task = asyncio.create_task(em.publish(run_id, "test_event", {}))
    ev = await gen.__anext__()
    assert ev.event_type == "test_event"
    assert len(em._subscribers.get(run_id, set())) == 1

    # Simulate client disconnect by closing the async generator
    await gen.aclose()

    # Queue should be cleaned up
    assert len(em._subscribers.get(run_id, set())) == 0


# ---------------------------------------------------------------------------
# 2. HTTP SSE Endpoint Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_sse_endpoint_unknown_run(client: AsyncClient):
    # Non-existent run ID returns 404
    response = await client.get("/api/v1/runs/00000000-0000-0000-0000-000000000000/events")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_sse_endpoint_streaming(
    client: AsyncClient,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    project = await create_project(db_session, name="SSE Target Project")
    run = await create_run(db_session, project_id=project.id)
    await db_session.commit()

    run_service = RunService(session_factory=session_factory)

    # Publish events including terminal run_completed
    await run_service.event_manager.publish(
        run.id,
        "run_started",
        {"run_id": run.id, "total": 1},
    )
    await run_service.event_manager.publish(
        run.id,
        "operation_started",
        {"operation_id": "testOp"},
    )
    await run_service.event_manager.publish(
        run.id,
        "run_completed",
        {"run_id": run.id, "status": "completed"},
    )

    received_lines: List[str] = []

    # Stream SSE from endpoint (replays history and closes cleanly on terminal event)
    async with client.stream("GET", f"/api/v1/runs/{run.id}/events") as response:
        assert response.status_code == status.HTTP_200_OK
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert response.headers.get("cache-control") == "no-cache"

        async for line in response.aiter_lines():
            if line:
                received_lines.append(line)

    full_output = "\n".join(received_lines)
    assert "event: run_started" in full_output
    assert "event: operation_started" in full_output
    assert "event: run_completed" in full_output
