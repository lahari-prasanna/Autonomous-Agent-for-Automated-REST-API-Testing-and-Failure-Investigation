"""
Verification script for Controlled Mock REST API.
Executes positive, negative, boundary, and intentional defect test scenarios.
"""

import sys
from fastapi.testclient import TestClient
from mock_api.main import app

client = TestClient(app)


def run_checks():
    print("=== STARTING MOCK REST API VERIFICATION ===")
    results = []

    # 1. Health check & security header
    r = client.get("/api/health")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert r.headers.get("X-Controlled-Mock-API") == "true", "Missing X-Controlled-Mock-API header"
    assert r.json().get("status") == "healthy"
    results.append(("Health Check & Safety Header", r.status_code, "PASSED"))

    # 2. Valid Login
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "password123"})
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    token = r.json().get("access_token")
    assert token is not None, "Missing access token"
    results.append(("Authentication (Valid)", r.status_code, "PASSED"))

    # 3. Invalid Login
    r = client.post("/api/auth/login", json={"username": "wrong", "password": "bad"})
    assert r.status_code == 401, f"Expected 401, got {r.status_code}"
    results.append(("Authentication (Invalid)", r.status_code, "PASSED"))

    auth_headers = {"Authorization": f"Bearer {token}"}

    # 4. List Products
    r = client.get("/api/products")
    assert r.status_code == 200
    assert len(r.json()) >= 3
    results.append(("List Products", r.status_code, "PASSED"))

    # 5. Get Product by ID (Existing)
    r = client.get("/api/products/1")
    assert r.status_code == 200
    assert r.json()["id"] == 1
    results.append(("Get Product by ID (Found)", r.status_code, "PASSED"))

    # 6. Get Product by ID (Non-existent)
    r = client.get("/api/products/999")
    assert r.status_code == 404
    results.append(("Get Product by ID (Not Found)", r.status_code, "PASSED"))

    # 7. Create Product without Auth (Negative)
    r = client.post(
        "/api/products",
        json={"name": "Gaming Monitor", "price": 350.0, "category": "Electronics"},
    )
    assert r.status_code == 401
    results.append(("Create Product (Unauthorized)", r.status_code, "PASSED"))

    # 8. Create Product with Valid Data & Auth (Positive)
    r = client.post(
        "/api/products",
        headers=auth_headers,
        json={"name": "Gaming Monitor", "price": 350.0, "category": "Electronics"},
    )
    assert r.status_code == 201
    new_id = r.json()["id"]
    results.append(("Create Product (Valid)", r.status_code, "PASSED"))

    # 9. Create Product with Zero Price (Boundary Test - Expect 400)
    r = client.post(
        "/api/products",
        headers=auth_headers,
        json={"name": "Free Sample", "price": 0.0, "category": "Promotions"},
    )
    assert r.status_code == 400
    results.append(("Create Product (Boundary: price=0 -> 400)", r.status_code, "PASSED"))

    # 10. Create Product with Negative Price (Intentional Defect - Expect 500)
    # Using client with raise_server_exceptions=False to capture HTTP 500
    defect_client = TestClient(app, raise_server_exceptions=False)
    r = defect_client.post(
        "/api/products",
        headers=auth_headers,
        json={"name": "Defective Item", "price": -500.0, "category": "Electronics"},
    )
    assert r.status_code == 500, f"Expected 500 for intentional bug, got {r.status_code}"
    results.append(("Create Product (Intentional Defect: price=-500 -> 500)", r.status_code, "PASSED"))

    # 11. Update Product (Positive)
    r = client.put(
        f"/api/products/{new_id}",
        headers=auth_headers,
        json={"name": "Ultra Gaming Monitor", "price": 399.99, "category": "Electronics"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Ultra Gaming Monitor"
    results.append(("Update Product", r.status_code, "PASSED"))

    # 12. Delete Product (Positive)
    r = client.delete(f"/api/products/{new_id}", headers=auth_headers)
    assert r.status_code == 204
    results.append(("Delete Product", r.status_code, "PASSED"))

    print("\nVerification Summary:")
    print("-" * 65)
    print(f"{'Test Scenario':<45} | {'HTTP Code':<9} | {'Status'}")
    print("-" * 65)
    for name, code, status in results:
        print(f"{name:<45} | {code:<9} | {status}")
    print("-" * 65)
    print("ALL 12 SCENARIOS VERIFIED SUCCESSFULLY.")
    return True


if __name__ == "__main__":
    success = run_checks()
    sys.exit(0 if success else 1)

