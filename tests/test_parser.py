"""
Unit test suite for OpenAPI Specification Parser & Normalizer (Phase 1B).
Verifies structural parsing, $ref resolution, security restrictions,
merging behaviors, and compatibility with mock_api/openapi.json.
"""

import pytest
from pathlib import Path
from backend.app.services.spec_parser import (
    OpenAPIParser,
    SpecParserError,
    SpecParsingError,
    SpecSecurityError,
    SpecValidationError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_OPENAPI_PATH = PROJECT_ROOT / "mock_api" / "openapi.json"


def test_parse_mock_api_openapi_json():
    """Verify that mock_api/openapi.json parses completely with full dereferencing."""
    spec = OpenAPIParser.parse_file(MOCK_OPENAPI_PATH)

    assert spec.title == "Controlled Mock E-Commerce REST API"
    assert spec.openapi_version == "3.1.0"
    assert len(spec.endpoints) == 7

    # Verify all expected endpoints are extracted
    endpoint_keys = {(ep.path, ep.method) for ep in spec.endpoints}
    expected = {
        ("/api/health", "GET"),
        ("/api/auth/login", "POST"),
        ("/api/products", "GET"),
        ("/api/products", "POST"),
        ("/api/products/{product_id}", "GET"),
        ("/api/products/{product_id}", "PUT"),
        ("/api/products/{product_id}", "DELETE"),
    }
    assert endpoint_keys == expected

    # Verify full $ref dereferencing on POST /api/products
    post_product = next(ep for ep in spec.endpoints if ep.path == "/api/products" and ep.method == "POST")
    assert post_product.request_body is not None
    assert post_product.request_body.required is True

    json_schema = post_product.request_body.get_schema("application/json")
    assert json_schema is not None
    # Ensure $ref was dereferenced and replaced with actual properties
    assert "properties" in json_schema
    assert "name" in json_schema["properties"]
    assert "price" in json_schema["properties"]
    assert "category" in json_schema["properties"]
    assert "$ref" not in json_schema

    # Verify responses
    assert "201" in post_product.responses
    assert "422" in post_product.responses
    resp_201_schema = post_product.responses["201"].get_schema("application/json")
    assert resp_201_schema is not None
    assert "id" in resp_201_schema.get("properties", {})


def test_parse_yaml_spec():
    """Verify that YAML specifications parse identically to JSON."""
    yaml_content = """
openapi: "3.0.3"
info:
  title: "YAML Sample API"
  version: "1.0.0"
paths:
  /items:
    get:
      summary: "List Items"
      responses:
        "200":
          description: "Successful response"
          content:
            application/json:
              schema:
                type: array
                items:
                  type: string
"""
    spec = OpenAPIParser().parse_string(yaml_content)
    assert spec.title == "YAML Sample API"
    assert spec.openapi_version == "3.0.3"
    assert len(spec.endpoints) == 1
    assert spec.endpoints[0].path == "/items"
    assert spec.endpoints[0].method == "GET"


def test_parameter_inheritance_and_override():
    """Verify operation parameters override path-level parameters with same (name, in)."""
    spec_dict = {
        "openapi": "3.0.3",
        "info": {"title": "Param Test", "version": "1.0"},
        "paths": {
            "/users/{user_id}": {
                "parameters": [
                    {
                        "name": "user_id",
                        "in": "path",
                        "required": True,
                        "description": "Path level description",
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "X-Trace-Id",
                        "in": "header",
                        "required": False,
                        "description": "Inherited header",
                        "schema": {"type": "string"},
                    },
                ],
                "get": {
                    "parameters": [
                        {
                            "name": "user_id",
                            "in": "path",
                            "required": True,
                            "description": "Operation level description override",
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            }
        },
    }
    spec = OpenAPIParser().parse_dict(spec_dict)
    endpoint = spec.endpoints[0]

    # Should have 2 parameters: user_id (overridden) and X-Trace-Id (inherited)
    assert len(endpoint.parameters) == 2
    param_map = {(p.name, p.in_): p for p in endpoint.parameters}

    user_id_param = param_map[("user_id", "path")]
    assert user_id_param.description == "Operation level description override"
    assert user_id_param.schema_["minimum"] == 1

    trace_param = param_map[("X-Trace-Id", "header")]
    assert trace_param.description == "Inherited header"


def test_security_inheritance_and_override():
    """Verify security inheritance from root and operation-level override."""
    spec_dict = {
        "openapi": "3.0.3",
        "info": {"title": "Security Test", "version": "1.0"},
        "security": [{"globalAuth": ["read"]}],
        "paths": {
            "/inherited": {
                "get": {
                    # No security defined: should inherit globalAuth
                    "responses": {"200": {"description": "OK"}}
                }
            },
            "/overridden": {
                "get": {
                    "security": [{"customAuth": ["admin"]}],
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/unsecured": {
                "get": {
                    "security": [],  # Explicit empty list disables security
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }
    spec = OpenAPIParser().parse_dict(spec_dict)
    ep_map = {ep.path: ep for ep in spec.endpoints}

    assert ep_map["/inherited"].security == [{"globalAuth": ["read"]}]
    assert ep_map["/overridden"].security == [{"customAuth": ["admin"]}]
    assert ep_map["/unsecured"].security == []


def test_response_keys_preservation():
    """Verify preservation of non-numeric response status keys like 'default', '2XX', '4XX'."""
    spec_dict = {
        "openapi": "3.0.3",
        "info": {"title": "Responses Test", "version": "1.0"},
        "paths": {
            "/status-codes": {
                "get": {
                    "responses": {
                        "200": {"description": "Success"},
                        "2XX": {"description": "All 200s"},
                        "4XX": {"description": "Client error range"},
                        "default": {"description": "Default catch-all error"},
                    }
                }
            }
        },
    }
    spec = OpenAPIParser().parse_dict(spec_dict)
    endpoint = spec.endpoints[0]

    assert set(endpoint.responses.keys()) == {"200", "2XX", "4XX", "default"}
    assert endpoint.responses["default"].description == "Default catch-all error"
    assert endpoint.responses["2XX"].description == "All 200s"


def test_multiple_content_types_preservation():
    """Verify preservation of multiple content types for requests and responses."""
    spec_dict = {
        "openapi": "3.0.3",
        "info": {"title": "Content-Type Test", "version": "1.0"},
        "paths": {
            "/upload": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"type": "object"}},
                            "multipart/form-data": {"schema": {"type": "object"}},
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {"schema": {"type": "string"}},
                                "text/plain": {"schema": {"type": "string"}},
                            },
                        }
                    },
                }
            }
        },
    }
    spec = OpenAPIParser().parse_dict(spec_dict)
    endpoint = spec.endpoints[0]

    assert "application/json" in endpoint.request_body.content
    assert "multipart/form-data" in endpoint.request_body.content
    assert "application/json" in endpoint.responses["200"].content
    assert "text/plain" in endpoint.responses["200"].content


def test_json_schema_composition_and_constraints_fidelity():
    """Verify composition keywords (allOf, oneOf, anyOf) and constraints are preserved."""
    spec_dict = {
        "openapi": "3.0.3",
        "info": {"title": "Schema Fidelity", "version": "1.0"},
        "paths": {
            "/composite": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "allOf": [
                                        {"type": "object", "properties": {"id": {"type": "integer"}}},
                                        {
                                            "type": "object",
                                            "properties": {
                                                "code": {
                                                    "type": "string",
                                                    "pattern": "^[A-Z]{3}-[0-9]+$",
                                                    "minLength": 5,
                                                    "maxLength": 10,
                                                }
                                            },
                                            "required": ["code"],
                                        },
                                    ]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    spec = OpenAPIParser().parse_dict(spec_dict)
    schema = spec.endpoints[0].request_body.get_schema("application/json")

    assert "allOf" in schema
    assert len(schema["allOf"]) == 2
    assert schema["allOf"][1]["properties"]["code"]["pattern"] == "^[A-Z]{3}-[0-9]+$"
    assert schema["allOf"][1]["properties"]["code"]["minLength"] == 5


def test_circular_ref_handling():
    """Verify recursive schemas do not trigger infinite recursion and preserve pointer."""
    spec_dict = {
        "openapi": "3.1.0",
        "info": {"title": "Recursive Schema", "version": "1.0"},
        "paths": {
            "/tree": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "Tree response",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TreeNode"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "TreeNode": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TreeNode"},
                        },
                    },
                }
            }
        },
    }
    # Should resolve without RecursionError
    spec = OpenAPIParser().parse_dict(spec_dict)
    schema = spec.endpoints[0].responses["200"].get_schema("application/json")

    assert schema["type"] == "object"
    assert "properties" in schema
    assert "children" in schema["properties"]
    # Internal recursion detected: ref preserved to avoid loop
    assert schema["properties"]["children"]["items"]["$ref"] == "#/components/schemas/TreeNode"


def test_external_ref_rejection_security():
    """Verify external HTTP/HTTPS, file, and cross-file references are blocked."""
    # 1. HTTP reference
    http_spec = {
        "openapi": "3.0.3",
        "info": {"title": "SSRF Test", "version": "1.0"},
        "paths": {
            "/test": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "http://169.254.169.254/latest/meta-data/"}
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    with pytest.raises(SpecSecurityError) as exc_info:
        OpenAPIParser().parse_dict(http_spec)
    assert "External remote $ref reference is forbidden" in str(exc_info.value)

    # 2. File protocol reference
    file_spec = {
        "openapi": "3.0.3",
        "info": {"title": "LFI Test", "version": "1.0"},
        "paths": {
            "/test": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "file:///etc/passwd"}
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    with pytest.raises(SpecSecurityError) as exc_info:
        OpenAPIParser().parse_dict(file_spec)
    assert "External remote $ref reference is forbidden" in str(exc_info.value)

    # 3. Cross-file relative reference
    cross_file_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Relative Ref Test", "version": "1.0"},
        "paths": {
            "/test": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "common.yaml#/models/User"}
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    with pytest.raises(SpecSecurityError) as exc_info:
        OpenAPIParser().parse_dict(cross_file_spec)
    assert "External file reference is forbidden" in str(exc_info.value)


def test_unsupported_version_rejection():
    """Verify rejection of Swagger 2.0 and unsupported versions."""
    swagger_2 = {
        "swagger": "2.0",
        "info": {"title": "Swagger 2", "version": "1.0"},
        "paths": {},
    }
    with pytest.raises(SpecValidationError) as exc:
        OpenAPIParser().parse_dict(swagger_2)
    assert "OpenAPI 2.0 (Swagger '2.0') is not supported" in str(exc.value)

    openapi_4 = {
        "openapi": "4.0.0",
        "info": {"title": "Future OpenAPI", "version": "1.0"},
        "paths": {},
    }
    with pytest.raises(SpecValidationError) as exc:
        OpenAPIParser().parse_dict(openapi_4)
    assert "Unsupported OpenAPI version '4.0.0'" in str(exc.value)


def test_malformed_syntax_rejection():
    """Verify malformed JSON/YAML raises SpecParsingError."""
    # Syntax error that is neither valid JSON nor valid YAML
    with pytest.raises(SpecParsingError):
        OpenAPIParser().parse_string("{\ninvalid json syntax: [unclosed list")

    with pytest.raises(SpecParsingError):
        OpenAPIParser().parse_string("")

    with pytest.raises(SpecParsingError):
        OpenAPIParser().parse_string("   ")


def test_fallback_base_url_optional():
    """Verify fallback_base_url is optional and never silently assumed."""
    spec_no_servers = {
        "openapi": "3.0.3",
        "info": {"title": "No Servers", "version": "1.0"},
        "paths": {
            "/test": {
                "get": {"responses": {"200": {"description": "OK"}}}
            }
        },
    }

    # Without fallback: servers must remain empty list
    parsed_no_fallback = OpenAPIParser().parse_dict(spec_no_servers)
    assert parsed_no_fallback.servers == []
    assert parsed_no_fallback.endpoints[0].servers == []

    # With explicit fallback: fallback is used
    parsed_with_fallback = OpenAPIParser(fallback_base_url="http://127.0.0.1:8081").parse_dict(spec_no_servers)
    assert parsed_with_fallback.endpoints[0].servers == ["http://127.0.0.1:8081"]
