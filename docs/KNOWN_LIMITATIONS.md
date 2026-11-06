# Known Limitations and Future Scope

This document provides an honest assessment of current limitations in the system and outlines future improvements.

---

## 1. Current Limitations

### 1.1 Single-Process Event Manager (SSE)
- **Current Behavior**: The live progress stream uses an in-memory `asyncio.Queue` inside the FastAPI application process.
- **Limitation**: This design works well for single-server and development deployments. However, if the backend is scaled horizontally across multiple worker processes (for example, using `gunicorn -w 4`), SSE connections on Worker A cannot receive events published by Worker B without an external pub/sub broker (such as Redis Pub/Sub or PostgreSQL `LISTEN/NOTIFY`).

### 1.2 SQLite Concurrency
- **Current Behavior**: By default, the application runs on SQLite (`sqlite+aiosqlite`) for zero-configuration local setup.
- **Limitation**: While SQLite is fast and reliable for local testing, it uses database-level locking during writes. If multiple high-concurrency test runs are triggered simultaneously, write contention can occur. For high-throughput team environments, switching `DATABASE_URL` to PostgreSQL is recommended.

### 1.3 Supported API Specifications
- **Current Behavior**: Supports REST APIs documented with OpenAPI 2.0 (Swagger), OpenAPI 3.0.x, and OpenAPI 3.1.x in either YAML or JSON format.
- **Limitation**: Non-RESTful protocols such as GraphQL, gRPC, WebSocket APIs, and SOAP are not supported.

### 1.4 Complex Authentication Flows
- **Current Behavior**: Supports Bearer tokens, API keys, and custom HTTP headers. In the test run configuration, users can provide authorization tokens or let the agent attempt login endpoints.
- **Limitation**: Multi-step interactive authentication flows (such as OAuth2 Authorization Code flow requiring an interactive browser redirect and two-factor SMS/email prompts) cannot be completed autonomously without pre-generated tokens.

### 1.5 Deep Stateful Endpoint Dependencies
- **Current Behavior**: The agent tests endpoints with schema-valid payloads and can link basic operations.
- **Limitation**: Highly stateful multi-step business logic (for example: creating a user &rarr; verifying an email &rarr; funding a wallet &rarr; adding items to cart &rarr; applying a discount &rarr; checking out) requires explicit scenario definition or pre-seeded database states. If an endpoint requires an existing database entity ID that cannot be queried publicly, the agent may receive an expected 404.

### 1.6 External LLM Rate Limits & Offline Mode
- **Current Behavior**: Deep root-cause investigation and natural language remediation suggestions utilize the Google Gemini API.
- **Limitation**: When running without an internet connection or with an invalid API key, the agent cannot execute LLM prompt chains. In these scenarios, the system automatically falls back to deterministic rule-based classification (analyzing status codes and schema differences) to complete the report.

---

## 2. Planned Future Improvements

1. **Distributed Event Broker**: Add a Redis adapter to the `EventManager` for seamless multi-worker clustering.
2. **Interactive OAuth2 Helper**: Allow users to paste dynamic session tokens or run a local token exchange proxy.
3. **Graph-Based Dependency Modeling**: Automatically derive dependency trees from OpenAPI `operationId` and path parameters to execute endpoints in natural dependency order.
4. **CI/CD Integration**: Add a GitHub Actions workflow or CLI runner command (`agy-test run --spec ...`) that returns non-zero exit codes when critical bugs are discovered.
5. **Local Small Language Model Support**: Integrate Ollama or llama.cpp for fully offline LLM reasoning without external API calls.
