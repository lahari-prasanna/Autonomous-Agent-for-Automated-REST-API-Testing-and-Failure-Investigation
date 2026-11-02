# Autonomous Agent for Automated REST API Testing and Failure Investigation

## 1. Overview
The **Autonomous Agent for Automated REST API Testing and Failure Investigation** is an AI-powered testing and diagnosis platform. Unlike traditional API testing tools that rely on manual test case authoring and static assertion checks, this platform autonomously parses REST API specifications (OpenAPI/Swagger), formulates structured testing plans, executes tests in a safe, controlled sandbox, and performs evidence-based failure investigation using LLM reasoning and selective Retrieval-Augmented Generation (RAG).

When an endpoint fails or exhibits anomalous behavior, the agent does not merely report a status code error; it analyzes the request payload, response body, HTTP headers, and API specification to formulate grounded root-cause hypotheses and recommended remediation actions.

---

## 2. Main Objectives
- **Automated Specification Understanding:** Parse and dereference OpenAPI 2.0/3.0/3.1 specifications into normalized endpoint schemas.
- **Autonomous Strategy & Planning:** Automatically synthesize positive, negative, boundary, and security test plans.
- **Controlled & Safe Execution:** Execute generated tests through a strictly deterministic HTTP executor with active SSRF protection.
- **Intelligent Failure Investigation:** Evaluate failed assertions, distinguish server errors from validation rejections, and link observed facts to potential root causes.
- **Targeted Retesting Loops:** Safely generate focused follow-up tests to confirm or refute failure hypotheses within strict iteration limits.
- **Comprehensive Developer Reporting:** Provide executive summaries and detailed technical failure reports with downloadable artifacts.

---

## 3. Technology Stack

### Backend
- **Language & Framework:** Python 3.10+, FastAPI (Asynchronous REST API Gateway)
- **Agent Orchestration:** LangGraph (State machine & controlled cycles)
- **LLM Integration:** LangChain Core, Gemini API (via `langchain-google-genai` with structured Pydantic outputs)
- **HTTP Engine:** HTTPX (Async client with timeout and rate management)
- **Validation:** Pydantic v2, JSON Schema
- **Database:** PostgreSQL (Relational metadata + JSONB for dynamic test payloads)
- **RAG & Embeddings (Upcoming Phase):** ChromaDB, Sentence-Transformers

### Frontend
- **Framework:** React.js 18+ with Vite
- **Styling:** Tailwind CSS
- **Visualization:** Recharts / Lucide Icons
- **Realtime Updates:** Server-Sent Events (SSE)

---

## 4. Key Features & Implementation Status

| Feature Module | Description | Status |
| :--- | :--- | :--- |
| **Foundation & Architecture** | Project structure, environment configuration, mandatory docs | **Completed** |
| **Mock Target REST API** | Deliberately flawed API with edge-case bugs for local evaluation | **Completed (Phase 1A)** |
| **OpenAPI Spec Parser** | Deterministic parser with `$ref` resolution and validation | **Completed (Phase 1B)** |
| **SSRF Security Guard** | Network & DNS rebinding protection with verified IP pinning | **Completed (Phase 2A)** |
| **Deterministic HTTP Executor** | Async test execution engine with SSRF transport & streaming size limits | **Completed (Phase 2B)** |
| **Assertion Evaluator** | Deterministic status code, header, and JSON Schema assertion evaluation | **Completed (Phase 2C)** |
| **LangGraph Test Generation** | Planning and generation nodes producing validated Pydantic test cases | Planned (Phase 3) |
| **Failure Investigation Engine** | Evidence-linked root cause analysis and RAG retrieval | Planned (Phase 4) |
| **Retest Controller** | Controlled feedback loops with strict iteration limits | Planned (Phase 4) |
| **Backend REST & SSE API** | FastAPI routes and real-time execution progress streaming | Planned (Phase 5) |
| **Interactive Web Dashboard** | React dashboard for workspace management and investigation views | Planned (Phase 6) |

---

## 5. Environment Configuration

Copy the example environment file and configure variables:

```bash
cp .env.example .env
```

Key environment settings:
- `GEMINI_API_KEY`: Google Gemini API key for LLM reasoning nodes.
- `DATABASE_URL`: PostgreSQL connection string (`postgresql+asyncpg://<user>:<password>@<host>:<port>/<dbname>`).
- `SAFE_DEV_MODE`: Must be set to `true` during local development to allow tests against the mock API on localhost.

---

## 6. Installation & Setup Instructions

### Prerequisites
- Python `>= 3.10`
- Node.js `>= 18.0` (npm `>= 9.0`)
- PostgreSQL `>= 14` (or local PostgreSQL service)

### 1. Backend Setup
```bash
# Navigate to project root
cd "/home/rguktrkvalley/Agent Project"

# Create and activate Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies (once requirements.txt is created in Phase 1)
# pip install -r backend/requirements.txt
```

### 2. Frontend Setup
```bash
# Navigate to frontend directory
cd "/home/rguktrkvalley/Agent Project/frontend"

# Install Node modules (once package.json is initialized in Phase 6)
# npm install
```

---

## 7. Running the Application (Once Implemented)

### Start Backend Service
```bash
source venv/bin/activate
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Start Frontend Dev Server
```bash
cd frontend
npm run dev
```

### Start Local Mock API
```bash
source venv/bin/activate
python -m mock_api.main
```

---

## 8. Security & Safe Testing Policy
- **SSRF Defense:** The execution engine resolves hostnames and blocks private/link-local IP addresses (`10.0.0.0/8`, `192.168.0.0/16`, `169.254.169.254`). Localhost (`127.0.0.1`) is strictly whitelisted only in `SAFE_DEV_MODE=true` for mock API verification.
- **No Arbitrary Code Execution:** The LLM outputs static declarative JSON test definitions, never raw Python or Bash commands.
- **Secret Isolation:** Auth credentials and API keys are injected via environment configurations and never logged in LLM prompts.

