# Setup and Installation Guide

This guide walks you through setting up and running the API testing and failure investigation project on your local machine.

---

## 1. Prerequisites

Before starting, ensure you have the following installed on your computer:

- **Python 3.10 or higher**: Check with `python3 --version`.
- **Node.js 18 or higher** and **npm**: Check with `node -v` and `npm -v`.
- **Git**: Check with `git --version`.

You do **not** need to install PostgreSQL or Docker for local development. By default, the project runs on **SQLite** (`sqlite+aiosqlite`), which is built directly into Python.

---

## 2. Step-by-Step Installation

### Step 2.1: Clone the Repository and Enter the Folder

```bash
cd "Agent Project"
```

### Step 2.2: Set Up Python Virtual Environment

Create and activate a Python virtual environment:

```bash
# Create virtual environment named 'venv'
python3 -m venv venv

# Activate it (Linux / macOS)
source venv/bin/activate

# On Windows PowerShell:
# .\venv\Scripts\Activate.ps1
```

Upgrade `pip` and install all backend Python dependencies:

```bash
pip install --upgrade pip
pip install -r backend/requirements.txt
```

### Step 2.3: Set Up the Frontend

Navigate to the `frontend` folder and install Node dependencies:

```bash
cd frontend
npm install
cd ..
```

---

## 3. Environment Configuration

Copy the sample environment file to create your local `.env`:

```bash
cp .env.example .env
```

Open `.env` in any text editor. The file contains helpful comments explaining each setting. Here are the key variables:

```ini
# Application mode
ENVIRONMENT=development
LOG_LEVEL=INFO

# Backend host and port
BACKEND_HOST=0.0.0.0
BACKEND_PORT=8000

# LLM reasoning configuration
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here

# Local database (SQLite file in project root)
DATABASE_URL=sqlite+aiosqlite:///./api_agent.db

# SSRF safety settings (allows testing against localhost mock API during dev)
SAFE_DEV_MODE=true
ALLOWED_DEV_HOSTS=localhost,127.0.0.1

# Mock API port
MOCK_API_PORT=8081

# Frontend API base URL
VITE_API_BASE_URL=http://localhost:8000
```

> **Note on GEMINI_API_KEY:**
> If you have a Google Gemini API key, paste it in `GEMINI_API_KEY`. If you do not have an API key right now, the application will still parse specifications, execute HTTP tests, evaluate assertions, and generate reports using rule-based fallback heuristics.

---

## 4. Running the Application

To run the complete system, you need three separate terminal windows or tabs.

### Terminal 1: Start the Target Mock API

The project includes a controlled mock e-commerce REST API with intentional bugs. Start it first:

```bash
source venv/bin/activate
uvicorn mock_api.main:app --host 127.0.0.1 --port 8081 --reload
```
You should see: `Uvicorn running on http://127.0.0.1:8081`.

### Terminal 2: Start the FastAPI Backend

In a second terminal:

```bash
source venv/bin/activate
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```
You should see: `Uvicorn running on http://127.0.0.1:8000`. You can visit `http://127.0.0.1:8000/docs` in your browser to inspect the interactive Swagger API documentation.

### Terminal 3: Start the React Frontend

In a third terminal:

```bash
cd frontend
npm run dev
```
You should see: `Local: http://localhost:3000/`.

Open `http://localhost:3000` in your browser to view the application dashboard.

---

## 5. Building Frontend for Production

If you want to test the production build instead of the development server:

```bash
cd frontend
npm run build
npm run preview -- --port 3000 --host 127.0.0.1
```

The production preview uses the reverse proxy configured in `frontend/vite.config.ts` to automatically forward requests to `http://127.0.0.1:8000`.

---

## 6. Running the Automated E2E Test

To verify that all services and workflows work together from start to finish, run the automated integration test script:

```bash
source venv/bin/activate
python scripts/verify_e2e_integration.py
```

This script will:
1. Start temporary instances of the Mock API, Backend, and Frontend.
2. Check backend health and frontend proxy connectivity.
3. Upload `mock_api/openapi.json` to create a project.
4. Verify parsed endpoints.
5. Trigger a test run.
6. Listen to live Server-Sent Events (SSE) telemetry.
7. Verify the completed diagnostic report and test Markdown/JSON export.
8. Test failure paths (malformed syntax, invalid schema, 409 conflict, run cancellation).
9. Test project deletion and database cascade cleanup.
10. Shut down all temporary test processes cleanly.

---

## 7. Troubleshooting Common Issues

### Issue 1: "Address already in use" (Port 8000, 8081, or 3000)
A previous process might still be running on that port. Find and stop it:
```bash
# Find process on port 8000 (Linux)
lsof -i :8000
# Kill process by PID
kill -9 <PID>
```

### Issue 2: "Database table not found" or "database is locked"
If SQLite is locked by an interrupted process:
1. Terminate any running Python processes.
2. Remove the local database file: `rm api_agent.db`.
3. Restart the backend: `uvicorn backend.app.main:app ...`. The backend automatically recreates all tables on startup.

### Issue 3: Frontend cannot reach backend
- Check the top header in the web browser at `http://localhost:3000`. It should show a green badge saying **Backend Connected**.
- If it says **Backend Disconnected**, ensure the backend is running in Terminal 2 on port 8000.
