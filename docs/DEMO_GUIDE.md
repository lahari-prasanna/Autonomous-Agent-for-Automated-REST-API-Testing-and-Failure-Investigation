# Project Demo & Viva Presentation Guide

This guide is written for students and evaluators who want to demonstrate or review the project step-by-step during a presentation, lab evaluation, or project viva.

---

## 1. Quick Setup Before Demo

Ensure all three terminal services are running:

```bash
# Terminal 1: Mock REST API
source venv/bin/activate
uvicorn mock_api.main:app --host 127.0.0.1 --port 8081

# Terminal 2: FastAPI Backend
source venv/bin/activate
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# Terminal 3: React Frontend
cd frontend
npm run dev
```

Open `http://localhost:3000` in Google Chrome or any modern browser.

---

## 2. Step-by-Step Demonstration Walkthrough

### Step 1: Show the Dashboard & Health Status
- **What to show**: The top navigation bar displays a green badge: **Backend Connected**.
- **What to explain**: "The frontend automatically polls `GET /health` on the backend. This ensures the backend and database connection are active before we run any tests."

### Step 2: Create a Project with the Mock API Spec
- **What to show**:
  1. Click **Projects** in the sidebar, then click **New Project** (or **Import OpenAPI**).
  2. Select **Upload File** and pick `mock_api/openapi.json` from the repository root (or copy-paste its content).
  3. Enter:
     - **Project Name**: `Mock Store API`
     - **Base URL Override**: `http://127.0.0.1:8081` (optional, since it is already in the spec)
  4. Click **Create Project**.
- **What to explain**: "The backend receives the OpenAPI 3.0 specification, validates its structure, resolves all internal schema references (`$ref`), and automatically extracts all available endpoints into the database."

### Step 3: Explore Endpoints
- **What to show**: The Project Detail page displays all 7 parsed endpoints:
  - `GET /api/health`
  - `POST /api/auth/login`
  - `GET /api/products`
  - `POST /api/products`
  - `GET /api/products/{product_id}`
  - `PUT /api/products/{product_id}`
  - `DELETE /api/products/{product_id}`
- **What to explain**: "Notice how methods have distinct color badges (`GET` blue, `POST` green, `PUT` amber, `DELETE` red). Each endpoint retains its parameter definitions and request body schemas."

### Step 4: Initiate an Autonomous Test Run
- **What to show**:
  1. Click the **Start Test Run** button in the top right.
  2. In the modal, explain the options:
     - **Concurrency**: Controls how many endpoints the agent tests in parallel (e.g. 2).
     - **Safe Dev Mode**: Allows tests to run against localhost during development.
  3. Click **Start Run**.
- **What to explain**: "The backend records the run as `pending`, returns HTTP 202 immediately, and schedules the testing agent in the background so the browser is never blocked."

### Step 5: Live Execution Dashboard (Real-Time SSE)
- **What to show**: The page automatically redirects to `/runs/:id`.
  1. Point out the top badge showing **Live** in green.
  2. Watch the **Operations Table**: Endpoints transition from `PENDING` &rarr; `RUNNING` &rarr; `PASSED`.
  3. Watch the **Live Event Timeline** on the right side: Shows events as they are emitted in real time (`run_started`, `operation_started`, `attempt_completed`, `run_completed`).
- **What to explain**: "This real-time progress is powered by Server-Sent Events (SSE). Unlike polling every second, the server pushes events to the browser as each test attempt finishes."

### Step 6: View the Diagnostic Report
- **What to show**: When the run finishes, click **View Diagnostic Report** (or go to `/runs/:id/report`).
  1. **Executive Summary**: Shows total operations targeted, pass rate percentage, total HTTP requests sent, and execution duration.
  2. **Operations List**: Click on an operation (e.g., `POST /api/products`) to expand its detailed results:
     - Initial request payload sent
     - HTTP response status and latency
     - Evaluated assertions
- **What to explain**: "Unlike tools like Postman that stop at pass/fail status codes, our platform generates a full diagnostic record including latency, request fingerprints, and validated schema contracts."

### Step 7: Export Reports
- **What to show**:
  1. Click **Export JSON** &rarr; downloads `report-<id>.json`.
  2. Switch to the **Markdown Preview** tab &rarr; shows rendered GitHub-flavored Markdown.
  3. Click **Download Markdown** &rarr; downloads `report-<id>.md`.
- **What to explain**: "Developers can easily attach the Markdown report to a GitHub issue or commit it with their CI/CD artifacts."

### Step 8: Demonstrate Negative Paths & Error Handling
- **What to show**:
  1. Try importing a file with invalid JSON syntax &rarr; displays a clear `400 Bad Request` alert.
  2. Try importing a valid JSON file without OpenAPI fields &rarr; displays a `422 Unprocessable Entity` alert.
  3. Start a new run and click **Cancel** &rarr; status updates to `cancelled` with an SSE event.
  4. Delete the project &rarr; all related test runs and reports are cascade-deleted cleanly.

---

## 3. Common Viva Questions & Model Answers

### Q1: What problem does this project solve?
> **Answer**: Traditional API testing tools require developers to manually write and maintain test cases. When tests fail, they usually just show a status code like `500 Internal Server Error`. Our platform reads the OpenAPI spec, generates tests automatically, and investigates failures to identify the probable root cause and suggest how to fix it.

### Q2: Why use LangGraph for the agent?
> **Answer**: API testing is not a simple linear script; it requires cycles and conditional logic. For example: test an endpoint &rarr; if it fails, investigate &rarr; generate parameter variations &rarr; retest &rarr; if still failing, finalize the hypothesis. LangGraph is built specifically for stateful graphs with controlled loops and termination conditions.

### Q3: What is SSRF protection and why do we need it?
> **Answer**: Server-Side Request Forgery happens when an attacker gives the agent a URL that points to internal private services (like `http://169.254.169.254` for AWS metadata or `http://localhost:5432`). Our HTTP executor checks target IP addresses against private network ranges and blocks them by default.

### Q4: Why did you choose Server-Sent Events (SSE) over WebSockets?
> **Answer**: WebSockets are bidirectional and require connection protocol upgrades. Our live progress stream is strictly one-way: the backend broadcasts progress updates to the frontend. SSE works over standard HTTP, reconnects automatically if dropped, and is lightweight to implement.

### Q5: What happens if the Gemini LLM API is unavailable?
> **Answer**: The application includes fallback heuristic rules. If the LLM call fails or times out, the failure classifier uses deterministic rule-based analysis (e.g. checking status code ranges, CORS headers, and schema validation errors) to ensure the test run finishes and a report is still generated.
