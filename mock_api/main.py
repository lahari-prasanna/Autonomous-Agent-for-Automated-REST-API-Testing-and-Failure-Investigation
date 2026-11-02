"""
Controlled Mock REST API for Autonomous Agent Testing & Failure Investigation.
Designed with realistic e-commerce endpoints and intentional testable defects.
"""

from typing import Dict, List, Optional
import os
import uvicorn
from fastapi import FastAPI, HTTPException, Header, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Controlled Mock E-Commerce REST API",
    description="Target API for Autonomous Testing Agent with intentional validation and server defects.",
    version="1.0.0",
)

# Enable CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers_and_verify(request: Request, call_next):
    """Enforces safety identification on all responses and restricts host access."""
    # Ensure all responses identify this as the authorized local mock environment
    response: Response = await call_next(request)
    response.headers["X-Controlled-Mock-API"] = "true"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------
# In-Memory Data Models & Seed Data
# ---------------------------------------------------------

VALID_TOKEN = "mock-bearer-token-12345"


class LoginRequest(BaseModel):
    username: str = Field(..., example="testuser")
    password: str = Field(..., example="password123")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in_seconds: int = 3600


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, example="Laptop")
    price: float = Field(..., example=999.99)
    category: str = Field(..., min_length=1, example="Electronics")
    description: Optional[str] = Field(None, example="High-performance laptop")


class ProductResponse(BaseModel):
    id: int
    name: str
    price: float
    category: str
    description: Optional[str] = None


PRODUCTS_DB: Dict[int, Dict] = {
    1: {
        "id": 1,
        "name": "Wireless Mouse",
        "price": 25.50,
        "category": "Electronics",
        "description": "Ergonomic wireless optical mouse",
    },
    2: {
        "id": 2,
        "name": "Mechanical Keyboard",
        "price": 85.00,
        "category": "Electronics",
        "description": "RGB mechanical gaming keyboard",
    },
    3: {
        "id": 3,
        "name": "Coffee Mug",
        "price": 12.00,
        "category": "Home & Kitchen",
        "description": "Ceramic coffee mug 350ml",
    },
}

_next_id = 4


def verify_auth_token(authorization: Optional[str]) -> None:
    """Helper to check Bearer token."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != VALID_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------


@app.get("/api/health", tags=["System"])
def health_check():
    """System health check and safety confirmation."""
    return {
        "status": "healthy",
        "service": "controlled-mock-api",
        "version": "1.0.0",
        "safe_dev_mode": True,
    }


@app.post(
    "/api/auth/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    tags=["Authentication"],
)
def login(creds: LoginRequest):
    """Authenticate and receive a Bearer token."""
    if creds.username == "testuser" and creds.password == "password123":
        return LoginResponse(access_token=VALID_TOKEN)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
    )


@app.get(
    "/api/products",
    response_model=List[ProductResponse],
    status_code=status.HTTP_200_OK,
    tags=["Products"],
)
def list_products(
    category: Optional[str] = Query(None, description="Filter products by category")
):
    """Retrieve all products, optionally filtered by category."""
    if category:
        return [p for p in PRODUCTS_DB.values() if p["category"].lower() == category.lower()]
    return list(PRODUCTS_DB.values())


@app.get(
    "/api/products/{product_id}",
    response_model=ProductResponse,
    status_code=status.HTTP_200_OK,
    tags=["Products"],
)
def get_product(product_id: int):
    """Retrieve a single product by ID."""
    if product_id not in PRODUCTS_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found",
        )
    return PRODUCTS_DB[product_id]


@app.post(
    "/api/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Products"],
)
def create_product(
    product: ProductCreate,
    authorization: Optional[str] = Header(None),
):
    """
    Create a new product. Requires authentication.
    
    INTENTIONAL TESTABLE DEFECTS:
    1. Negative price (price < 0): Unhandled exception causes HTTP 500 (Server Error)
       instead of documented HTTP 400 Bad Request validation response.
    2. Zero price (price == 0): Returns HTTP 400 Bad Request with validation error.
    3. Missing Auth: Returns HTTP 401 Unauthorized.
    """
    global _next_id

    # 1. Authentication Check
    verify_auth_token(authorization)

    # 2. INTENTIONAL DEFECT: Unhandled Server Error on Negative Price
    # A robust API should return HTTP 400 with a validation error.
    # Here, an unhandled exception causes FastAPI / Uvicorn to produce HTTP 500.
    if product.price < 0:
        raise RuntimeError(
            f"CRITICAL: Unhandled database constraint violation for negative price: {product.price}"
        )

    # 3. Boundary validation check
    if product.price == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Validation Error: Product price must be greater than zero",
        )

    new_product = {
        "id": _next_id,
        "name": product.name,
        "price": product.price,
        "category": product.category,
        "description": product.description,
    }
    PRODUCTS_DB[_next_id] = new_product
    _next_id += 1

    return new_product


@app.put(
    "/api/products/{product_id}",
    response_model=ProductResponse,
    status_code=status.HTTP_200_OK,
    tags=["Products"],
)
def update_product(
    product_id: int,
    product: ProductCreate,
    authorization: Optional[str] = Header(None),
):
    """Update an existing product."""
    verify_auth_token(authorization)

    if product_id not in PRODUCTS_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found",
        )

    if product.price < 0:
        raise RuntimeError(
            f"CRITICAL: Unhandled database constraint violation for negative price: {product.price}"
        )

    updated = {
        "id": product_id,
        "name": product.name,
        "price": product.price,
        "category": product.category,
        "description": product.description,
    }
    PRODUCTS_DB[product_id] = updated
    return updated


@app.delete(
    "/api/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Products"],
)
def delete_product(
    product_id: int,
    authorization: Optional[str] = Header(None),
):
    """Delete a product by ID."""
    verify_auth_token(authorization)

    if product_id not in PRODUCTS_DB:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found",
        )

    del PRODUCTS_DB[product_id]
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":
    # Restrict binding strictly to 127.0.0.1 for local development security
    uvicorn.run("mock_api.main:app", host="127.0.0.1", port=8081, reload=True)

