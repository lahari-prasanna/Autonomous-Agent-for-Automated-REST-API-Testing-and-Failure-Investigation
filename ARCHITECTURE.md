# System Architecture

## 1. Architectural Philosophy
The core principle of this platform is **Strict Decoupling of Reasoning and Execution**:
1. **The LLM never directly executes arbitrary code or unconstrained requests.** It outputs strictly typed JSON schemas (validated via Pydantic).
2. **Deterministic engines perform all physical side-effects** (HTTP network requests, Server-Side Request Forgery checks, response timing, JSON schema validation).
3. **The LangGraph state machine orchestrates the lifecycle**, enforcing loop termination guards, human approvals, and structured state persistence.
4. **Failure investigation operates on strict evidence tiers**, formally distinguishing between *Confirmed Facts*, *Likely Hypotheses*, and *Speculative Edge Cases*.

---

## 2. High-Level Component Architecture

```
+-------------------------------------------------------------------------------+
|                             React Frontend (Vite)                             |
|  - Workspace & Spec Explorer   - Live Agent Node Progression   - Reports      |
+---------------------------------------+---------------------------------------+
                                        | HTTP / SSE Stream
+---------------------------------------v---------------------------------------+
|                             FastAPI Backend Gateway                           |
|  - Spec Upload / Dereferencing - Project Management - Realtime SSE Dispatcher |
+---------------------------------------+---------------------------------------+
                                        |
+---------------------------------------v---------------------------------------+
|                    LangGraph State Machine (Agent Core)                       |
|                                                                               |
|  [Parse Spec] -> [Understand API] -> [Plan Tests] -> [Generate Tests]         |
|                                                              |                |
|                                                              v                |
|  [Report Gen] <- [Retest Decision] <- [Investigate] <- [Validate & Execute]  |
+-------------------+-------------------------------------------+---------------+
                    |                                           |
+-------------------v-------------------+   +-------------------v---------------+
|          Knowledge Base / RAG         |   |      Deterministic HTTP Engine    |
|   - ChromaDB (RFCs, Best Practices)   |   |   - HTTPX Async Client            |
|   - Sentence-Transformers Embeddings  |   |   - SSRF Guard & Rate Limiter     |
+---------------------------------------+   |   - JSON Schema / Status Asserter |
                                            +-------------------+---------------+
                                                                |
                                            +-------------------v---------------+
                                            |       Target REST API             |
                                            |   (Mock API or Staging Target)    |
                                            +-----------------------------------+
```

---

## 3. Frontend & Backend Responsibilities

### Backend Responsibilities (`backend/`)
- **OpenAPI Ingestion & Normalization:** Ingests raw OpenAPI YAML/JSON, resolves `$ref` components, and generates standardized endpoint metadata.
- **Agent Orchestration:** Manages the LangGraph state machine, checkpointing, and execution state transitions.
- **Deterministic HTTP Testing:** Sends controlled requests, records high-resolution latency, captures response headers/body, and runs assertions.
- **Security & Sandboxing:** Prevents SSRF attacks by blocking loopback, private, and cloud metadata IP ranges (allowing localhost only in explicit safe dev mode).
- **Failure Investigation & RAG:** Queries vector store for HTTP RFCs and API specifications to support root cause reasoning.
- **Persistence & API Gateway:** Exposes RESTful endpoints and Server-Sent Events (SSE) for frontend consumption.

### Frontend Responsibilities (`frontend/`)
- **Project Configuration:** Manage testing projects, upload specs, and configure authentication credentials.
- **Specification Explorer:** Interactive visualization of endpoints, parameters, and schemas.
- **Testing Workspace:** Review and approve AI-generated test plans and test cases.
- **Real-Time Execution Monitor:** View live agent node progress and test execution status via SSE.
- **Failure Investigation View:** Detailed evidence cards comparing expected vs. actual behavior with AI-generated root causes.
- **Analytics & Reports:** Visual charts for pass/fail distribution, latency metrics, and downloadable reports.

---

## 4. LangGraph Agent Workflow & State

### State Schema (`AgentState`)
The agent workflow operates over a strongly typed state dictionary:

```python
class AgentState(TypedDict):
    project_id: str
    run_id: str
    raw_spec: Dict[str, Any]
    parsed_endpoints: List[Dict[str, Any]]
    testing_strategy: Dict[str, Any]
    generated_tests: List[Dict[str, Any]]
    validated_tests: List[Dict[str, Any]]
    execution_results: List[Dict[str, Any]]
    failure_records: List[Dict[str, Any]]
    investigation_reports: List[Dict[str, Any]]
    retest_queue: List[Dict[str, Any]]
    retest_iteration: int
    max_retest_iterations: int
    final_report: Optional[Dict[str, Any]]
    error_log: List[str]
```

### State Machine Nodes
1. **`parse_spec` (Deterministic):** Validates and dereferences OpenAPI schemas without consuming LLM tokens.
2. **`understand_api` (LLM):** Identifies endpoint hierarchies, data constraints, and security requirements.
3. **`plan_tests` (LLM):** Formulates an abstract testing matrix across positive, negative, boundary, and security categories.
4. **`generate_tests` (LLM):** Produces concrete test cases with explicit payloads and assertions using Pydantic structured output.
5. **`validate_tests` (Deterministic):** Validates generated tests against safety policies and endpoint schema definitions.
6. **`execute_tests` (Deterministic Tool):** Executes requests using `HTTPX` and records responses and latency.
7. **`analyze_results` (Deterministic / LLM):** Evaluates assertions and categorizes results into passed and failed.
8. **`investigate_failures` (LLM + RAG):** Examines failed tests, retrieves relevant RFCs and documentation, and generates evidence-grounded hypotheses.
9. **`retest_decision` (Conditional Node):** Evaluates if follow-up tests are needed while strictly enforcing `max_retest_iterations` (default: 2) to prevent infinite loops.
10. **`generate_report` (LLM / Aggregator):** Compiles final summary statistics, failure details, and remediation steps.

---

## 5. Database Architecture

### Primary Database: PostgreSQL (with JSONB)
- **Relational Tables:** Enforce referential integrity for core entities:
  - `users`: User identity and credentials.
  - `projects`: Base URLs, authentication schemes, project metadata.
  - `endpoints`: Normalized endpoints belonging to projects.
  - `agent_runs`: Metadata of execution sessions.
- **JSONB Columns:** Store dynamic, polymorphic test structures:
  - `test_cases`: Request headers, parameters, body payload, and assertions stored as `JSONB`.
  - `test_executions`: Raw response bodies, headers, and assertion results stored as `JSONB`.
  - `failure_investigations`: Evidence chains, hypotheses, and recommendations stored as `JSONB`.

---

## 6. LLM Integration Strategy
- **Provider Interface:** Modular abstraction supporting Google Gemini (default: `gemini-1.5-flash` / `gemini-2.0-flash` for fast tasks, `gemini-1.5-pro` for deep reasoning).
- **Structured Outputs:** Enforce Pydantic schema validation via `ChatModel.with_structured_output(...)` to prevent syntax and formatting errors.
- **Prompt Isolation:** Secrets and auth credentials are strictly redacted before passing context to LLM prompts.

---

## 7. RAG Integration (Upcoming Phase)
- **Vector Database:** ChromaDB (embedded or client-server).
- **Knowledge Sources:**
  - Standard HTTP specification documents (RFC 9110, RFC 7231).
  - OpenAPI specification descriptions and user-supplied notes.
  - Common API validation anti-patterns and security guidelines.
- **Retrieval Trigger:** RAG retrieval is triggered conditionally during the failure investigation phase to contextualize observed status codes and header mismatches.

---

## 8. Security & Sandboxing Architecture
Implemented in `backend/app/core/security.py`:
- **Protocol Whitelisting:** Permitted protocols strictly limited to `http` and `https`. Unsafe protocols (`file`, `ftp`, `gopher`, `dict`, `data`, `ldap`) are rejected with `SSRFSecurityError`.
- **IP Range Filtering:** Multi-range blacklist covering both IPv4 and IPv6:
  - Private networks (RFC 1918 `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, RFC 4193 `fc00::/7`).
  - Loopback (`127.0.0.0/8`, `::1`).
  - Link-local and cloud metadata (`169.254.0.0/16`, `169.254.169.254`, `fe80::/10`, GCP `fd00:ec1::254`).
  - Shared / CGNAT (`100.64.0.0/10`), Benchmarking (`198.18.0.0/15`), Multicast, Reserved, Broadcast.
- **IPv4-Mapped IPv6 Normalization:** Unwraps embedded IPv4 addresses (`::ffff:x.x.x.x` and 6to4 `2002::/16`) to subject them to identical IPv4 security rules.
- **DNS Resolution & Multi-Address Enforcement:** Resolves all DNS `A` and `AAAA` records; rejects the hostname if ANY returned IP is prohibited.
- **DNS Rebinding & TOCTOU Mitigation (Connection Pinning):**
  - Outbound TCP sockets (`SSRFSafeAsyncNetworkBackend` / `connect_tcp`) are pinned directly to pre-verified literal IP addresses, preventing secondary DNS lookups from switching to private targets.
  - Post-connection verification inspects `stream.get_extra_info("server_addr")` to confirm the socket remote address matches the verified safe address pool.
  - Preserves TLS SNI by passing original hostname to `start_tls()`.
- **Safe Dev Mode:**
  - `SAFE_DEV_MODE=True` permits verified loopback (127.0.0.1 and ::1) strictly for local mock API testing. All other private and metadata addresses remain blocked.
  - `SAFE_DEV_MODE=False` unconditionally blocks all loopback addresses.
- **URL & Credential Hygiene:** URLs containing embedded userinfo/passwords (`user:pass@host`) are rejected; credentials are never logged or stored in exceptions.
- **Proxy Isolation:** Transports enforce `trust_env=False` to block ambient environment proxy hijacking.

---

## 9. Controlled Mock API & Defect Injection Model
To evaluate agent failure investigation and retesting loops without external dependencies, a local mock REST API (`mock_api/main.py`) provides seeded edge-case flaws:
- **Unhandled Server Defect:** `POST /api/products` with `price < 0` triggers an unhandled `RuntimeError`, producing an HTTP 500 response instead of the expected HTTP 400 validation response. This tests the agent's ability to diagnose unhandled validation defects.
- **Boundary Validation:** `POST /api/products` with `price == 0` returns an HTTP 400 Bad Request, enabling boundary contrast analysis against the negative price defect.
- **Authentication Guard:** Endpoints enforce Bearer token verification, returning HTTP 401 Unauthorized when tokens are missing or invalid.
- **Safety Marker:** Middleware injects `X-Controlled-Mock-API: true` to confirm target authorization.

---

## 10. Deterministic OpenAPI Specification Parser Architecture
Implemented in `backend/app/services/spec_parser.py` and `backend/app/schemas/spec.py`:
- **Deterministic Node 1:** Operates with zero LLM token consumption, providing clean contract ingestion before LLM reasoning begins.
- **Security Pre-Scan Guard:** Scans raw JSON/YAML specifications prior to resolution, rejecting any external HTTP/HTTPS, `file://`, `ftp://`, or cross-file relative references to eliminate Server-Side Request Forgery (SSRF) and Local File Inclusion (LFI).
- **Safe Recursive Dereferencing:** Resolves local JSON pointers (`#/components/...`) using RFC 6901 standards while tracking call-stack references to break cycles and preserve recursive schemas without `RecursionError`.
- **Parameter & Security Normalization:** Merges path-level and operation-level parameters using `(name, in)` identity and operation-level security overrides.
- **Schema & Status Code Fidelity:** Preserves all JSON Schema composition keywords (`allOf`, `oneOf`, `anyOf`, `not`), constraints (`minimum`, `pattern`, `enum`), and non-numeric status codes (`default`, `2XX`, `4XX`).

---

## 11. Deterministic HTTP Execution Engine Architecture
Implemented in `backend/app/services/http_executor.py` and `backend/app/schemas/execution.py`:
- **Deterministic Tool Layer:** Safely executes test requests synthesized by the agent without delegating network transport logic to unconstrained LLM calls.
- **SSRF Safe Transport Binding:** Employs `SSRFSafeAsyncTransport` exclusively. Standard default HTTP transports and environment proxies (`trust_env=False`) are disallowed.
- **Connection Pinning & Revalidation:** Enforces pre-connection URL and multi-address DNS validation. Every redirect hop is re-validated through the Phase 2A SSRF guard before establishing any TCP socket connection.
- **Manual Redirect Security:** Automatic HTTP client redirects are strictly disabled (`follow_redirects=False`). Manual redirect traversal allows at most 3 hops, automatically converting 303/301/302 methods to GET and stripping sensitive authentication headers (`Authorization`, `Cookie`, `Proxy-Authorization`) across cross-origin hops.
- **Decompression Bomb & Response Size Protection:** Streams incoming response chunks via `client.stream(...)` up to a hard 10 MB limit (`MAX_RESPONSE_BYTES = 10 * 1024 * 1024`). Decompression is evaluated on-the-fly, aborting immediately if decompressed byte streams exceed the ceiling.
- **Strict Bounded Timeouts:** Enforces a hard total duration limit of 10.0 seconds (`MAX_TOTAL_TIMEOUT = 10.0s`), clamped across connect (3.0s), read (7.0s), and write (3.0s).
- **Concurrency Throttling:** Restricts active concurrent requests via an `asyncio.Semaphore(max_concurrency)` (default: 5) to prevent overwhelming target APIs or local test runners.
- **High-Precision Telemetry:** Accurately measures round-trip latency in milliseconds using monotonic `time.perf_counter()`.
- **Credential & Secret Redaction:** Sanitizes authorization headers and removes URL credentials prior to structured result packaging and logging.




