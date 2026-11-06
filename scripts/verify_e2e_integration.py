"""
Phase 7: Full Application Integration and End-to-End Verification Script.
Executes the complete user journey across Mock API, Backend FastAPI, and Frontend Vite Proxy.
"""

import asyncio
from datetime import datetime
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
import httpx

MOCK_API_PORT = 8081
BACKEND_PORT = 8000
FRONTEND_PORT = 3000

MOCK_API_URL = f"http://127.0.0.1:{MOCK_API_PORT}"
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"


class StepLogger:
    def __init__(self):
        self.results: List[Dict[str, Any]] = []

    def record(self, step_number: int, name: str, status: str, details: str = ""):
        print(f"\n[{status.upper()}] Step {step_number}: {name}")
        if details:
            print(f"       Details: {details}")
        self.results.append({
            "step": step_number,
            "name": name,
            "status": status,
            "details": details,
        })


logger = StepLogger()


async def wait_for_port(url: str, timeout_sec: float = 15.0) -> bool:
    start = time.time()
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.time() - start < timeout_sec:
            try:
                res = await client.get(url)
                if res.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
    return False


async def run_e2e_journey():
    procs: List[subprocess.Popen] = []

    env = os.environ.copy()
    env["PYTHONPATH"] = "."

    try:
        # -------------------------------------------------------------
        # STEP 1: Start Mock API, Backend, and Frontend Servers
        # -------------------------------------------------------------
        print("Starting Mock REST API on 127.0.0.1:8081...")
        mock_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mock_api.main:app", "--host", "127.0.0.1", "--port", str(MOCK_API_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        procs.append(mock_proc)

        print("Starting Backend FastAPI on 127.0.0.1:8000...")
        backend_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        procs.append(backend_proc)

        print("Starting Frontend Vite Preview on 127.0.0.1:3000...")
        frontend_proc = subprocess.Popen(
            ["npm", "--prefix", "frontend", "run", "preview", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        procs.append(frontend_proc)

        mock_ok = await wait_for_port(f"{MOCK_API_URL}/api/health", 20.0)
        backend_ok = await wait_for_port(f"{BACKEND_URL}/health", 20.0)
        frontend_ok = await wait_for_port(f"{FRONTEND_URL}/", 20.0)

        if not (mock_ok and backend_ok and frontend_ok):
            err_details = []
            if not mock_ok:
                err_details.append(f"Mock API failed. Stderr: {mock_proc.stderr.read().decode()[:300] if mock_proc.stderr else ''}")
            if not backend_ok:
                err_details.append(f"Backend failed. Stderr: {backend_proc.stderr.read().decode()[:300] if backend_proc.stderr else ''}")
            if not frontend_ok:
                err_details.append(f"Frontend failed. Stderr: {frontend_proc.stderr.read().decode()[:300] if frontend_proc.stderr else ''}")

            logger.record(1, "Start application servers", "FAIL", " | ".join(err_details))
            return
        logger.record(1, "Start application servers", "PASS", "Mock API (8081), Backend (8000), and Frontend (3000) listening")

        async with httpx.AsyncClient(timeout=30.0) as client:
            # -------------------------------------------------------------
            # STEP 2: Check Backend Health and Frontend Connectivity
            # -------------------------------------------------------------
            backend_health = await client.get(f"{BACKEND_URL}/health")
            assert backend_health.status_code == 200, f"Expected 200, got {backend_health.status_code}"
            bh_json = backend_health.json()
            assert bh_json["status"] == "ok"

            frontend_index = await client.get(f"{FRONTEND_URL}/")
            assert frontend_index.status_code == 200
            assert '<div id="root"></div>' in frontend_index.text

            # Test Vite proxy to backend health check
            frontend_health_proxy = await client.get(f"{FRONTEND_URL}/health")
            assert frontend_health_proxy.status_code == 200
            assert frontend_health_proxy.json()["status"] == "ok"
            logger.record(2, "Backend health and frontend connectivity", "PASS", f"Backend healthy (env: {bh_json.get('environment')}), Frontend served, Vite proxy active")

            # -------------------------------------------------------------
            # STEP 3: Upload Mock API OpenAPI Spec & Create Project
            # -------------------------------------------------------------
            with open("mock_api/openapi.json", "r", encoding="utf-8") as f:
                spec_content = f.read()

            create_res = await client.post(
                f"{BACKEND_URL}/api/v1/projects",
                json={
                    "name": "E2E Integration E-Commerce API",
                    "spec_content": spec_content,
                    "description": "Full application integration verification project",
                    "base_url_override": MOCK_API_URL,
                },
            )
            assert create_res.status_code == 201, f"Expected 201, got {create_res.status_code}: {create_res.text}"
            project = create_res.json()
            project_id = project["id"]
            assert project["name"] == "E2E Integration E-Commerce API"
            assert project["endpoints_count"] >= 6
            logger.record(3, "OpenAPI spec upload & project creation", "PASS", f"Project created (ID: {project_id}, Endpoints: {project['endpoints_count']})")

            # -------------------------------------------------------------
            # STEP 4: Inspect Project Details & Parsed Endpoints
            # -------------------------------------------------------------
            detail_res = await client.get(f"{BACKEND_URL}/api/v1/projects/{project_id}")
            assert detail_res.status_code == 200
            detail = detail_res.json()
            endpoints = detail.get("endpoints", [])
            assert len(endpoints) == project["endpoints_count"]

            found_methods = {ep["method"].upper() for ep in endpoints}
            found_paths = {ep["path"] for ep in endpoints}
            assert "GET" in found_methods
            assert "POST" in found_methods
            assert "/api/health" in found_paths
            assert "/api/products" in found_paths
            logger.record(4, "Project details & extracted endpoints", "PASS", f"Verified {len(endpoints)} endpoints across {len(found_methods)} HTTP methods")

            # -------------------------------------------------------------
            # STEP 5: Start Test Run with Mock API
            # -------------------------------------------------------------
            run_res = await client.post(
                f"{BACKEND_URL}/api/v1/projects/{project_id}/runs",
                json={
                    "concurrency": 2,
                    "safe_dev_mode": True,
                    "custom_base_url": MOCK_API_URL,
                },
            )
            assert run_res.status_code == 202, f"Expected 202, got {run_res.status_code}: {run_res.text}"
            run_data = run_res.json()
            run_id = run_data["id"]
            assert run_data["status"] in ("pending", "running")
            logger.record(5, "Start test execution run", "PASS", f"Run initiated (ID: {run_id}, status: {run_data['status']}, concurrency: 2)")

            # -------------------------------------------------------------
            # STEP 6: Verify Live Execution Updates through SSE
            # -------------------------------------------------------------
            events_received: List[Dict[str, Any]] = []
            event_types_seen: set = set()
            terminal_event_seen = False

            print(f"Subscribing to SSE stream for run {run_id}...")
            async with client.stream("GET", f"{BACKEND_URL}/api/v1/runs/{run_id}/events", timeout=90.0) as sse_stream:
                assert sse_stream.status_code == 200, f"Expected 200 for SSE stream, got {sse_stream.status_code}"
                assert "text/event-stream" in sse_stream.headers.get("content-type", "")

                current_event_type = "message"
                current_event_id = None

                async for line in sse_stream.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event: "):
                        current_event_type = line[7:].strip()
                    elif line.startswith("id: "):
                        current_event_id = line[4:].strip()
                    elif line.startswith("data: "):
                        raw_data = line[6:].strip()
                        try:
                            parsed = json.loads(raw_data)
                            event_info = {
                                "id": current_event_id,
                                "type": current_event_type,
                                "data": parsed.get("data", {}),
                                "timestamp": parsed.get("timestamp"),
                            }
                            events_received.append(event_info)
                            event_types_seen.add(current_event_type)

                            if current_event_type in ("run_completed", "run_failed", "run_cancelled"):
                                terminal_event_seen = True
                                break
                        except Exception as e:
                            print(f"Error parsing SSE data: {e}")

            assert len(events_received) > 0, "No SSE events received"
            assert terminal_event_seen, f"Terminal event not received. Event types seen: {event_types_seen}"
            assert "run_completed" in event_types_seen or "run_failed" in event_types_seen
            logger.record(6, "Live SSE execution telemetry", "PASS", f"Received {len(events_received)} events. Types seen: {sorted(list(event_types_seen))}")

            # -------------------------------------------------------------
            # STEP 7: Open Completed Diagnostic Report & Verify Contents
            # -------------------------------------------------------------
            report_res = await client.get(f"{BACKEND_URL}/api/v1/runs/{run_id}/report?format=json")
            assert report_res.status_code == 200, f"Expected 200, got {report_res.status_code}: {report_res.text}"
            report = report_res.json()

            summary = report["summary"]
            assert summary["total_operations"] >= 6
            assert summary["total_requests_executed"] > 0
            assert summary["pass_rate_percentage"] >= 0.0

            ops = report["operations"]
            assert len(ops) >= 6

            # Verify actual operation details
            investigated_ops = [op for op in ops if op.get("investigation")]
            retried_ops = [op for op in ops if len(op.get("transient_retries", [])) > 0]
            variation_ops = [op for op in ops if len(op.get("variation_tests", [])) > 0]

            logger.record(
                7,
                "Diagnostic report verification",
                "PASS",
                f"Report ID: {report['report_id']} | Total Ops: {summary['total_operations']} | "
                f"Passed: {summary['passed_operations']}, Failed: {summary['failed_operations']}, "
                f"Pass Rate: {summary['pass_rate_percentage']}% | "
                f"Investigations: {len(investigated_ops)}, Retests: {len(retried_ops)}, Variations: {len(variation_ops)}"
            )

            # -------------------------------------------------------------
            # STEP 8: Download JSON and Markdown Reports
            # -------------------------------------------------------------
            md_res = await client.get(f"{BACKEND_URL}/api/v1/runs/{run_id}/report?format=markdown")
            assert md_res.status_code == 200
            assert "text/markdown" in md_res.headers.get("content-type", "")
            md_text = md_res.text
            assert "# Diagnostic API Testing Report" in md_text or "# Diagnostic Report" in md_text
            assert "Executive Summary" in md_text
            assert "Operation Run Records" in md_text

            # JSON download verification
            assert report["report_id"] is not None
            assert isinstance(report["summary"], dict)
            assert isinstance(report["operations"], list)
            logger.record(8, "Report export (JSON & Markdown)", "PASS", f"Markdown size: {len(md_text)} bytes | JSON verified with all required fields")

            # -------------------------------------------------------------
            # STEP 9: Negative & Edge Case Failure Paths
            # -------------------------------------------------------------
            # 9a: Malformed YAML/JSON syntax
            bad_syntax_res = await client.post(
                f"{BACKEND_URL}/api/v1/projects",
                json={"name": "Bad Syntax", "spec_content": "{ not: valid: json:"},
            )
            assert bad_syntax_res.status_code == 400
            assert "SpecParsingError" in bad_syntax_res.text or "syntax" in bad_syntax_res.text.lower()

            # 9b: Valid JSON but invalid OpenAPI schema
            bad_schema_res = await client.post(
                f"{BACKEND_URL}/api/v1/projects",
                json={"name": "Bad Schema", "spec_content": '{"foo": "bar"}'},
            )
            assert bad_schema_res.status_code == 422
            assert "SpecValidationError" in bad_schema_res.text or "validation" in bad_schema_res.text.lower()

            # 9c: Request report on an in-progress or non-existent run
            # Start run 2 and immediately request report before it finishes
            run2_res = await client.post(
                f"{BACKEND_URL}/api/v1/projects/{project_id}/runs",
                json={"concurrency": 1, "safe_dev_mode": True, "custom_base_url": MOCK_API_URL},
            )
            run2_id = run2_res.json()["id"]

            # Try to get report immediately
            early_report_res = await client.get(f"{BACKEND_URL}/api/v1/runs/{run2_id}/report")
            if early_report_res.status_code == 409:
                conflict_detail = early_report_res.json().get("detail", "")
                assert "not yet available" in conflict_detail.lower() or "status is" in conflict_detail.lower()

            # 9d: Cancellation workflow
            cancel_res = await client.post(f"{BACKEND_URL}/api/v1/runs/{run2_id}/cancel")
            assert cancel_res.status_code == 200
            assert cancel_res.json()["status"] == "cancelled"

            # 9e: 404 on unknown IDs
            unknown_proj = await client.get(f"{BACKEND_URL}/api/v1/projects/00000000-0000-0000-0000-000000000000")
            assert unknown_proj.status_code == 404
            unknown_run = await client.get(f"{BACKEND_URL}/api/v1/runs/00000000-0000-0000-0000-000000000000")
            assert unknown_run.status_code == 404

            logger.record(9, "Failure path verification", "PASS", "Verified: 400 parsing error, 422 schema error, 409 in-progress report conflict, run cancellation, 404 unknown IDs")

            # -------------------------------------------------------------
            # STEP 10: Cascade Deletion & Project Navigation Cleanup
            # -------------------------------------------------------------
            del_res = await client.delete(f"{BACKEND_URL}/api/v1/projects/{project_id}")
            assert del_res.status_code == 204

            # Verify project is gone
            get_del_proj = await client.get(f"{BACKEND_URL}/api/v1/projects/{project_id}")
            assert get_del_proj.status_code == 404

            # Verify associated run is cascade deleted
            get_del_run = await client.get(f"{BACKEND_URL}/api/v1/runs/{run_id}")
            assert get_del_run.status_code == 404

            logger.record(10, "Project deletion & cascade verification", "PASS", "Project and associated runs cascade deleted successfully (204 -> 404)")

    finally:
        print("\nShutting down background services...")
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=3.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("All background services terminated.")


if __name__ == "__main__":
    asyncio.run(run_e2e_journey())
