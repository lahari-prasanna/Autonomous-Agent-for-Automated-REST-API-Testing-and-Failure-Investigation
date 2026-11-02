# Project Implementation Tasks

## Phase 0: Project Foundation & Environment Setup
- [x] Inspect existing workspace and preserve all existing files.
- [x] Create project directory structure (`backend/`, `frontend/`, `mock_api/`, `tests/`).
- [x] Configure `.gitignore` for Python, Node, and environment files.
- [x] Create `.env.example` with security defaults and LLM configuration keys.
- [x] Create mandatory documentation files:
  - [x] `README.md`
  - [x] `PROJECT_STATUS.md`
  - [x] `ARCHITECTURE.md`
  - [x] `DEVELOPMENT_LOG.md`
  - [x] `TASKS.md`
- [x] Assess host environment (Python, Node.js, npm, Git, PostgreSQL).
- [x] Document environment setup and prerequisite installation instructions.

---

## Phase 1A: Backend Environment Setup & Controlled Mock REST API
- [x] Verify and activate Python virtual environment (`venv`).
- [x] Create `backend/requirements.txt` with minimal dependencies for mock API (`fastapi`, `uvicorn`, `pydantic`, `httpx`, `python-dotenv`).
- [x] Install minimal dependencies into virtual environment.
- [x] Build controlled `mock_api`:
  - [x] Implement FastAPI mock service (`mock_api/main.py`) with endpoints (`/api/health`, `/api/auth/login`, `/api/products`, `/api/products/{id}`).
  - [x] Seed intentional real-world validation bugs (status 500 unhandled error on negative price, 400 on zero price, 401 on missing auth).
  - [x] Enforce safety restrictions (`X-Controlled-Mock-API` header, strict loopback host binding).
  - [x] Generate clean OpenAPI 3.0 specification (`mock_api/openapi.json`).
- [x] Create and run automated verification suite (`mock_api/verify_api.py`) verifying all 12 test scenarios.
- [x] Run live HTTP verification against Uvicorn server via curl.
- [x] Update documentation across all 5 mandatory tracking files.

---

## Phase 1B: OpenAPI Specification Parser
- [x] Add parsing dependencies (`prance`, `openapi-spec-validator`, `pytest`) to `backend/requirements.txt`.
- [x] Implement Pydantic normalization models (`backend/app/schemas/spec.py`).
- [x] Implement OpenAPI Specification Parser (`backend/app/services/spec_parser.py`):
  - [x] Pre-resolution security scan rejecting remote HTTP/HTTPS and file references.
  - [x] Safe internal `$ref` dereferencing with circular dependency preservation.
  - [x] Validate spec structure using `openapi-spec-validator`.
  - [x] Parameter inheritance and override merging.
  - [x] Security requirement inheritance and override merging.
  - [x] Preservation of status codes ('default', '2XX', '4XX') and multiple content types.
  - [x] Full JSON Schema constraints and composition preservation.
- [x] Write unit tests for the specification parser (`tests/test_parser.py`).
- [x] Execute unit tests against `mock_api/openapi.json` and edge cases (12/12 passing).
- [x] Re-verify Phase 1A verification suite (12/12 passing).
- [x] Update documentation across all 5 mandatory tracking files.

---

## Phase 2A: SSRF Security Guard & Connection Pinning
- [x] Implement SSRF Security Guard (`backend/app/core/security.py`):
  - [x] Enforce HTTP and HTTPS schemes only; reject non-HTTP protocols.
  - [x] Comprehensive IPv4/IPv6 prohibited range filtering (private, loopback, link-local, reserved, multicast, metadata).
  - [x] Unwrap IPv4-mapped IPv6 (`::ffff:x.x.x.x`) and 6to4 addresses.
  - [x] Multi-address DNS resolution: reject host if ANY address is prohibited.
  - [x] DNS rebinding & TOCTOU mitigation: pin TCP connect directly to verified IP.
  - [x] Post-connection remote socket address verification.
  - [x] Safe dev mode: permit 127.0.0.1/localhost only when `SAFE_DEV_MODE=True`.
  - [x] Enforce `SAFE_DEV_MODE=False` blocking loopback; metadata always blocked.
  - [x] Reject unsafe URL forms, credentials in URLs, and invalid ports.
  - [x] Structured `SSRFSecurityError` with reason codes and URL sanitization.
  - [x] HTTPX custom transport integration (`SSRFSafeAsyncTransport`, `SSRFSafeSyncTransport`) with `trust_env=False`.
- [x] Implement unit tests in `tests/test_security.py` (38/38 passing).
- [x] Re-verify Phase 1A and Phase 1B test suites (zero regressions).
- [x] Update documentation across all 5 mandatory tracking files.

---

## Phase 2B: Deterministic HTTP Executor
- [x] Define execution Pydantic schemas (`backend/app/schemas/execution.py`):
  - [x] Structured `ExecutionRequest` and `ExecutionResult` models.
  - [x] Sensitive header and credential sanitization (`Authorization`, `Cookie`, `X-API-Key`).
- [x] Implement Deterministic HTTP Executor (`backend/app/services/http_executor.py`):
  - [x] Asynchronous request execution using `httpx.AsyncClient` and custom `SSRFSafeAsyncTransport`.
  - [x] High-resolution response latency measurement (`time.perf_counter()`).
  - [x] Hard 10s upper timeout bound clamped across connect (3s), read (7s), and write (3s).
  - [x] Max concurrency throttling (default 5 concurrent connections via asyncio semaphore).
  - [x] Streaming response size enforcement (10 MB hard limit) on decompressed byte chunks.
  - [x] Manual redirect traversal with max 3 hops, SSRF revalidation per hop, and sensitive header stripping across origins.
  - [x] Safe JSON/text response body decoding with error recovery.
  - [x] Clean client resource teardown via async context managers.
- [x] Write unit tests for HTTP executor (`tests/test_executor.py`) (26/26 passing).
- [x] Re-verify Phase 1A, Phase 1B, and Phase 2A test suites (88/88 total tests passing).
- [x] Update documentation across all 5 mandatory tracking files.

---

## Phase 2C: Deterministic Assertion Evaluator
- [x] Implement Deterministic Assertion Evaluator (`backend/app/services/assertion_evaluator.py`):
  - [x] Structured assertion models (`AssertionResult`, `AssertionReport`).
  - [x] HTTP status code matching (exact, ranges `2XX`, `4XX`, `5XX`, and `default`).
  - [x] Response `Content-Type` validation (exact, wildcards `*/*`, `application/*`, structured `+json`).
  - [x] JSON Schema validation with `Draft202012Validator` and OAS 3.0 `nullable: true` / OAS 3.1 null unions.
  - [x] Complete constraint support: `type`, `required`, `properties`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`, `oneOf`, `anyOf`, `allOf`.
  - [x] Empty responses and missing schema handling.
  - [x] Useful JSON Pointer paths on failures (`/items/0/price`).
  - [x] Safe reference resolution using Phase 1B resolved structures.
- [x] Write unit tests for assertion evaluation (`tests/test_assertion_evaluator.py`) (29/29 passing).
- [x] Re-verify Phase 1A, Phase 1B, Phase 2A, and Phase 2B test suites (117/117 total tests passing).
- [x] Update documentation across mandatory tracking files.

---

## Phase 3: LangGraph Agent Core (Understand -> Plan -> Generate -> Validate)
### Phase 3A: Agent State & Shared Data Models
- [x] Define unified `AgentState` schema and lifecycle statuses (`backend/app/agent/state.py`).
- [x] Implement structured error envelope `AgentError` with diagnostics and exception conversion.
- [x] Implement safe state constructor and merger `create_initial_state` preserving data and isolating mutable containers.
- [x] Ensure zero live HTTP resources (clients, sockets, open streams) are retained in state.
- [x] Add focused tests in `tests/test_agent_state.py` (7/7 passing).
- [x] Full test suite regression verification (112 pytest + 12 mock API = 124 passing checks).

### Phase 3B: Deterministic LangGraph Individual Nodes
- [x] Implement deterministic node functions (`backend/app/agent/nodes.py`):
  - [x] `load_spec_node`: Ingest and validate OpenAPI 3.0.x / 3.1.x specifications via existing `OpenAPIParser`.
  - [x] `select_operation_node`: Lookup operation by operation ID or method/path from `ParsedAPISpec.endpoints`.
  - [x] `prepare_request_node`: Construct `ExecutionRequest` with path param substitution, auth token, and clamped limits.
  - [x] `execute_request_node`: Asynchronously invoke existing `HTTPExecutor` with SSRF protection, timeout, and size limits.
  - [x] `evaluate_response_node`: Deterministically evaluate status codes and schema assertions via `DeterministicAssertionEvaluator`.
- [x] Add focused node test suite in `tests/test_agent_nodes.py` (23/23 passing).
- [x] Full test suite regression verification (135 pytest + 12 mock API = 147 passing checks).

### Phase 3C: LangGraph Workflow Assembly & Conditional Routing
- [x] Implement StateGraph assembly and explicit conditional routing (`backend/app/agent/graph.py`):
  - [x] Register existing 5 nodes: `load_spec`, `select_operation`, `prepare_request`, `execute_request`, `evaluate_response`.
  - [x] Explicit conditional edge functions: `route_after_load_spec`, `route_after_select_operation`, `route_after_prepare_request`, `route_after_execute_request`, `route_after_evaluate_response`.
  - [x] Error termination at `END` preserving original `AgentError` without continuing downstream nodes.
  - [x] Execution timeouts and network failures terminate at `END` without invoking `evaluate_response`.
  - [x] Schema/status assertion failures produce evaluated `AssertionReport` with `is_success=False`, not a routing error.
  - [x] Expose `build_agent_graph()`, `compile_agent_graph()`, and compiled `agent_graph`.
- [x] Add graph test suite in `tests/test_agent_graph.py` (14/14 passing).
- [x] Full test suite regression verification (149 pytest + 12 mock API = 161 passing checks).

---

## Phase 4: Failure Investigation Engine, RAG, & Retest Loop
- [ ] Setup ChromaDB vector store (`backend/app/services/rag_service.py`):
  - [ ] Ingest standard HTTP specifications (RFC 9110) and API validation guidelines.
  - [ ] Implement vector similarity search for failed status codes and headers.
- [ ] Implement `investigate_failures_node`:
  - [ ] Gather deterministic failure evidence (sent payload, response body, headers).
  - [ ] Query RAG knowledge base for contextual standards.
  - [ ] Formulate grounded root-cause hypotheses, evidence links, and remediation steps.
- [ ] Implement `retest_decision_node`:
  - [ ] Conditional logic to queue targeted follow-up boundary tests.
  - [ ] Strict loop termination guard (`retest_iteration >= max_retest_iterations`).
- [ ] Implement `generate_report_node`:
  - [ ] Aggregate overall metrics, test breakdown, and failure investigation summaries.
- [ ] Test end-to-end agentic feedback loop against mock API flaws.
- [ ] Update documentation and log progress in `DEVELOPMENT_LOG.md`.

---

## Phase 5: Backend REST API, Database Integration, & SSE Streaming
- [ ] Configure PostgreSQL database engine and session management with SQLAlchemy (`backend/app/core/database.py`).
- [ ] Define database models (Users, Projects, Endpoints, Runs, TestCases, Executions, Investigations).
- [ ] Implement database migrations or schema synchronization.
- [ ] Implement FastAPI routers:
  - [ ] `/api/v1/projects`: Project CRUD and spec upload.
  - [ ] `/api/v1/agent`: Run initiation, status polling, and execution cancellation.
  - [ ] `/api/v1/reports`: Retrieve structured run reports.
- [ ] Implement Server-Sent Events (SSE) router (`backend/app/core/sse.py`) for live streaming node updates.
- [ ] Update documentation and log progress in `DEVELOPMENT_LOG.md`.

---

## Phase 6: Interactive React Frontend Dashboard
- [ ] Initialize React frontend with Vite and Tailwind CSS.
- [ ] Build core layout components (Navbar, Sidebar, Container).
- [ ] Build **Dashboard View**: Recent testing sessions, overall pass/fail metrics, and project shortcuts.
- [ ] Build **Project Creation & Spec Ingestion View**: OpenAPI file upload and base URL setup.
- [ ] Build **API Explorer View**: Visual endpoint list, request parameters, and schemas.
- [ ] Build **AI Testing Workspace View**: Review and approve test plans and generated test cases.
- [ ] Build **Live Agent Execution View**: Real-time LangGraph node progression indicator and log stream via SSE.
- [ ] Build **Failure Investigation View**: Split view of actual vs. expected results, evidence cards, and suggested fixes.
- [ ] Build **Analytics & Reports View**: Visual charts (Recharts) and export buttons (JSON/PDF).
- [ ] Connect frontend services to backend REST & SSE endpoints.
- [ ] Update documentation and log progress in `DEVELOPMENT_LOG.md`.

---

## Phase 7: End-to-End Verification & Hardening
- [ ] Run full end-to-end validation test against the mock API.
- [ ] Validate that SSRF protections block private IP ranges while allowing safe dev mode.
- [ ] Validate that the retesting loop terminates at the iteration threshold.
- [ ] Verify error handling and graceful fallbacks for LLM rate limits or network dropouts.
- [ ] Final documentation update and project wrap-up.

