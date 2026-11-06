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

### Phase 3D: End-to-End Integration Testing & Validation
- [x] Implement comprehensive E2E integration test suite (`tests/test_agent_integration.py`):
  - [x] Ephemeral loopback ASGI server lifecycle with pre-bound socket passing (`sockets=[sock]`) eliminating port race conditions.
  - [x] Process-level database isolation (`reset_mock_db`) restoring initial product database state between tests.
  - [x] Deterministic latency simulation via custom ASGI middleware and `X-Test-Delay` header without global monkey-patching.
  - [x] 14 end-to-end integration scenarios verifying compiled LangGraph workflow:
    - [x] Scenario 1: Successful GET `/api/products` (200 OK + JSON schema validation).
    - [x] Scenario 2: Successful POST `/api/products` (201 Created + schema validation).
    - [x] Scenario 3: Successful DELETE `/api/products/{product_id}` (204 No Content).
    - [x] Scenario 4: Operation selection by OpenAPI `operationId` (`list_products_api_products_get`).
    - [x] Scenario 5: HTTP 401 Unauthorized reaches `evaluate_response_node`.
    - [x] Scenario 6: HTTP 404 Not Found reaches `evaluate_response_node`.
    - [x] Scenario 7: HTTP 400 Bad Request boundary reaches `evaluate_response_node`.
    - [x] Scenario 8: HTTP 500 Server defect reaches `evaluate_response_node`.
    - [x] Scenario 9: Invalid OpenAPI spec path halts at `load_spec` -> `END`.
    - [x] Scenario 10: Non-existent operation halts at `select_operation` -> `END`.
    - [x] Scenario 11: Missing base URL halts at `prepare_request` -> `END`.
    - [x] Scenario 12: Deterministic HTTP execution timeout halts at `execute_request` -> `END`.
    - [x] Scenario 13: Connection refused / network error halts at `execute_request` -> `END`.
    - [x] Scenario 14: SSRF private IP blocked before socket creation halts at `execute_request` -> `END`.
- [x] Add integration test suite (`tests/test_agent_integration.py`) (14/14 passing).
- [x] Full regression verification (163 pytest + 12 mock API = 175 passing checks).

---

## Phase 4: Failure Investigation Engine, RAG, & Retest Loop
### Phase 4A: Deterministic Failure Classification Engine
- [x] Implement structured failure schemas (`backend/app/schemas/failure.py`):
  - [x] Failure taxonomy enum (`FailureCategory`) with 15 categories including `EVALUATION_INCOMPLETE`.
  - [x] Severity enum (`FailureSeverity`) and structured conservative retry models (`RetryPolicy`, `RetryGuidance`).
  - [x] Allowlist-based sanitized diagnostic evidence (`SanitizedFailureEvidence`).
  - [x] Top-level serializable classification outcome model (`FailureClassification`).
- [x] Implement deterministic classification service (`backend/app/services/failure_classifier.py`):
  - [x] Strict lifecycle error precedence over execution/assertion envelopes.
  - [x] Mandatory `EVALUATION_INCOMPLETE` flagging on completed HTTP exchange without assertion report (for all status codes).
  - [x] Evaluated contract success trust verification requiring `status == 'evaluated'` and zero active errors.
  - [x] Undeclared 5xx classification as `SERVER_ERROR` with `UNEXPECTED_HTTP_STATUS` secondary (strictly 500-599 and undeclared).
  - [x] Contract-compliant declared 4xx and 5xx responses preserved as successful tests; declared 5xx failing schema check classified as `RESPONSE_SCHEMA_MISMATCH` with zero `SERVER_ERROR` secondary.
  - [x] Fresh execution state contract (`create_fresh_run_state`): rejects transient overrides (`status`, `error`, `execution_result`, `assertion_report`) with `ValueError`, and unconditionally resets transient fields to clean initial values.
  - [x] Safe, bounded, JSON-serializable history: purges raw Pydantic models, request/response bodies, raw headers, auth tokens, cookies, assertion actuals, and details into compact allowlisted records capped at 5 entries.
  - [x] Workflow invocation contract (`run_agent_workflow`): public async wrapper calling `create_fresh_run_state()`, executing `agent_graph.ainvoke()`, and returning final state.
  - [x] Hierarchical precedence between unexpected HTTP statuses and specific client-status categories.
  - [x] Distinct classification of transport failures, timeouts, and SSRF blocks.
  - [x] Idempotency-aware conservative retry guidance (RFC 9110 method heuristic).
  - [x] Allowlist-based evidence sanitization stripping secrets, tokens, credentials, query values, and raw bodies.
- [x] Add comprehensive unit test suite (`tests/test_failure_classifier.py`) (56/56 passing).
- [x] Full regression verification (219 pytest + 12 mock API = 231 passing checks).

### Phase 4B: RAG Knowledge Base & Standard Specification Ingestion
#### Phase 4B.1: Knowledge Document and Chunk Schemas (Completed & Approved)
- [x] Create strongly-typed Pydantic v2 knowledge models (`backend/app/schemas/knowledge.py`):
  - [x] `KnowledgeSourceType` enum (`openapi_spec`, `api_documentation`, `failure_history`).
  - [x] `KnowledgeDocument` model with document identifier, source type, title, source reference, operation metadata, timestamp, and bounded metadata.
  - [x] `KnowledgeChunk` model with chunk identifier, parent document link, content, position index, operation metadata, failure category, provenance, and property alias `text`.
  - [x] Strict validation & security corrections:
    - [x] Explicit `ALLOWED_METADATA_KEYS` allowlist (`tag`, `category`, `version`, `status_code`, `section`, `doc_format`, `is_deprecated`, `content_type`, `scenario`, `verified`).
    - [x] Key-specific value validation (`status_code` in 100..599, booleans for flags, bounded non-empty strings).
    - [x] Sensitive value rejection for bearer tokens, JWTs, private keys, and credential assignments.
    - [x] Secure `source_ref` handling: strip query tokens, strip unsafe fragments, preserve safe section anchors (`^[A-Za-z0-9_.-]+$`), reject embedded/percent-encoded credentials, reject absolute paths, home directories, and traversal segments.
- [x] Add comprehensive unit test suite (`tests/test_knowledge_schemas.py`) (28/28 passing).
- [x] Full regression verification (247 pytest + 12 mock API = 259 passing checks).

#### Phase 4B.2: Document Ingestion and Chunking (Completed - Audit Corrections Applied)
- [x] Implement secure `DocumentIngestionService` (`backend/app/services/document_ingestion.py`):
  - [x] Three supported source types: OpenAPI specifications (via `OpenAPIParser`), local Markdown/text documentation, sanitized `FailureClassification` and history.
  - [x] Workspace-rooted component-by-component traversal with `O_DIRECTORY | O_NOFOLLOW` directory FDs, `fstat()` regular file validation, and bounded 1MB read loop.
  - [x] Collision-resistant source and document identity with Unicode NFC normalization, root namespacing, and path fingerprinting.
  - [x] Chunk identity independent of whole-document content hash, based on source identity, semantic scope, part index, and chunk content hash.
  - [x] OpenAPI extraction safety: `MAX_SCHEMA_DEPTH = 4`, `MAX_OPERATION_PARAMETERS = 30`, `MAX_OPERATION_RESPONSES = 10`, cumulative `MAX_OPERATION_TOTAL_PROPERTIES = 80`, max description 1024 chars, local circular `$ref` preservation, sensitive property stripping.
  - [x] Three-tier content sanitization policy (Category A structural drop, Category B redaction, Category C quarantine) with separate typed `IngestionResult` and `IngestionWarning` representations.
  - [x] Strict chunk size ceilings (2048 chars max), max 100 chunks per document, soft 50-char minimum, hierarchical sentence-aware splitting, deterministic metadata, and fixed timestamp injection.
  - [x] Endpoint provenance verification for failure history against `spec.endpoints`.
  - [x] Targeted Audit Corrections:
    - [x] F-01: Update `compute_source_slug` and document ID construction to incorporate full 16-character source fingerprint while preserving readability and `MAX_ID_LEN <= 128`.
    - [x] F-02: Add focused directory-as-file rejection test through non-regular file check, verifying zero documents or chunks produced.
    - [x] F-03: Add focused file-growth test simulating concurrent post-`fstat` size expansion, verifying bounded read raises `DocumentSizeLimitError` without returning oversized data.
    - [x] F-04: Add focused file-descriptor leak test tracking operation-specific descriptors during partial traversal failure, verifying all descriptors are closed and invalid (`EBADF`).
- [x] Add comprehensive unit test suite (`tests/test_document_ingestion.py`) (40/40 passing).
- [x] Full regression verification (287 pytest + 12 mock API = 299 passing checks).

#### Phase 4B.3: Local Knowledge Storage and Indexing
- [x] Complete Architecture & Design Specification:
  - [x] Final consolidated architecture specification delivered (`PHASE_4B_3_ARCHITECTURE_FINAL.md`) resolving items C1–C15 and all 20 crash boundaries.
  - [x] Architecture Revision 17 reviewed and approved.
- [x] Implement `LocalKnowledgeStore` and Vector Storage (`backend/app/services/knowledge_store.py`):
  - [x] Setup SQLite ACID coordinator (`coordinator.sqlite3`) with WAL mode, `synchronous=FULL`, foreign keys, strict PRAGMAs, and thread-local connection manager.
  - [x] Integrate ChromaDB 0.5.5 derived vector store with generation-scoped collections (`chunks_gen_<G>`).
  - [x] Implement deterministic 384-dimensional hash embedding baseline with symbol fallback, cancellation zero fallback, and unit norm.
  - [x] Implement injective Chroma document and chunk ID encoders/decoders.
  - [x] Implement global lock hierarchy ($\mathcal{L}_1 \implies \mathcal{L}_2$: `.writer.lock EX` $\implies$ `.lifecycle.lock EX`) with POSIX `flock` and Win32 `LockFileEx` support.
  - [x] Implement kernel pin locks, 3-state fail-closed pin probing (`STALE`, `LIVE`, `UNKNOWN`), and query reader session management (`QueryReaderSession`).
  - [x] Implement atomic cleanup state machine (`RETIRED -> TOMBSTONED -> PURGING -> CANONICAL PURGE`) with verified row-count transitions (`rowcount == 1`).
  - [x] Implement 18-step migration workflow with durable staging intent and safe retired-generation pruning hierarchy.
  - [x] Implement startup crash reconciliation (`reconcile_startup`) recovering from all 20 crash boundaries.
  - [x] Implement filesystem security traversal validation (`openat`/`O_NOFOLLOW` on POSIX, DACL/reparse on Windows, remote mount rejection).
  - [x] Implement comprehensive 28-test architecture verification suite (`tests/test_local_knowledge_store.py`, 28/28 passing).
  - [x] Full regression verification (315 passing tests across repository: 28 Phase 4B.3 + 287 existing, 0 regressions).

#### Phase 4B.4: Retrieval and Metadata Filtering (Completed)
- [x] Implement semantic retrieval in `LocalKnowledgeStore` (`backend/app/services/knowledge_store.py`):
  - [x] Normalized relevance score computation (`compute_relevance_score(distance)` mapping $[0.0, 2.0]$ to $[0.0, 1.0]$).
  - [x] Stable, immutable `RetrievalResult` model (`chunk: KnowledgeChunk`, `score: float`, `distance: float`).
  - [x] Validated `RetrievalFilter` model with field allowlists, alias resolution (`path` -> `path_template`), and strict parameter rejection.
  - [x] SQLite canonical hydration ensuring content and rich metadata come strictly from SQLite `canonical_chunks`.
  - [x] Strict reader snapshot isolation validating candidates against `captured_active_doc_ids`.
  - [x] Generation and commit-sequence consistency adhering to pinned reader session tokens.
  - [x] Bounded candidate expansion preventing premature result truncation when Chroma candidates are filtered by SQLite metadata.
  - [x] Comprehensive input validation for query text, top_k range, filter keys, and values.
  - [x] Backward-compatible delegation in `query_chunks()` supporting `return_scores` flag and session helper methods.
  - [x] Add comprehensive 24-test retrieval verification suite (`tests/test_knowledge_retrieval.py`, 24/24 passing).
  - [x] Full regression verification (344 passed, 4 skipped on Linux, 0 failures across repository).

### Phase 4C: Retest Controller, Failure Investigation Orchestration & Diagnostic Reporting
- [x] Implement central boundary sanitization engine (`backend/app/schemas/sanitizer.py`, `ReportDataSanitizer`):
  - [x] Secret assignment regex, bearer token, JWT, and cookie masking.
  - [x] Safe URL sanitization with userinfo stripping, query parameter masking, port validation, and bracketed IPv6 reconstruction.
  - [x] Safe relative source reference validation preserving Phase 4B relative paths and URLs while rejecting traversal and unsafe schemes.
- [x] Implement immutable retest and diagnostic report schemas (`backend/app/schemas/retest.py`, `backend/app/schemas/report.py`):
  - [x] Deeply immutable frozen Pydantic models with immutable `Tuple` collections.
  - [x] Complete DiagnosticReport root validator enforcing mathematical identities, candidate metric partitions, biunique ledger reconciliation, and valid state transitions.
- [x] Implement Retest Controller (`backend/app/services/retest_controller.py`):
  - [x] Ephemeral HMAC-SHA256 request fingerprinting for identical-request verification.
  - [x] RFC-compliant integer delta-seconds and HTTP-date `Retry-After` parsing with bounded delays (max 5.0s, never shortened).
  - [x] Strict budget tracking with combined ceiling of 2 follow-ups.
  - [x] Fail-closed variation candidate synthesis denying POST/PATCH mutations by default.
- [x] Implement Safe Markdown Diagnostic Report Generator (`backend/app/services/report_generator.py`):
  - [x] HTML and Markdown active character escaping for inline strings.
  - [x] Inert code-span rendering for URLs, paths, and IDs.
  - [x] Multiline blockquote formatting for untrusted evidence and recommendations without Markdown table pipe confusion.
- [x] Implement Outer Workflow Orchestrator (`backend/app/agent/orchestrator.py`):
  - [x] External wrap treating compiled Phase 3 `agent_graph` as an immutable black box.
  - [x] Single operation and batch suite execution with bounded concurrency semaphore (1–5).
  - [x] Unified ordered follow-up ledger (`followup_decisions`) bijectively reconciling with execution records.
  - [x] Non-fatal failure investigation degradation upon knowledge store exceptions (`investigation_degraded=True`).
- [x] Implement comprehensive Phase 4C test suites (`tests/test_retest_controller.py`, `tests/test_report_generator.py`, `tests/test_workflow_orchestrator.py`, 37/37 passing).
- [x] Verify full regression across repository (407 passed, 4 skipped on Linux, 0 failures).

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

