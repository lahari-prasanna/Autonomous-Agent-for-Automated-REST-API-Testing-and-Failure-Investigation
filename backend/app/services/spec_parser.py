"""
OpenAPI Specification Parser & Normalizer Service.
Supports OpenAPI 3.0.x and 3.1.x with strict security restrictions,
safe circular $ref resolution, and complete JSON Schema fidelity.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import json
import re
import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from backend.app.schemas.spec import (
    EndpointDefinition,
    ParameterDefinition,
    ParsedAPISpec,
    RequestBodyDefinition,
    ResponseDefinition,
    SecuritySchemeDefinition,
)


class SpecParserError(Exception):
    """Base exception for all specification parsing errors."""
    pass


class SpecParsingError(SpecParserError):
    """Raised when raw JSON or YAML cannot be syntactically parsed."""
    pass


class SpecValidationError(SpecParserError):
    """Raised when specification violates OpenAPI 3.x schema or unsupported version."""
    pass


class SpecSecurityError(SpecParserError):
    """Raised when specification contains prohibited external, remote, or file references."""
    pass


class OpenAPIParser:
    """
    Deterministic OpenAPI 3.0.x and 3.1.x specification parser.
    
    Features:
    - Pre-resolution security scan that rejects all remote HTTP/HTTPS and external file references.
    - Safe internal $ref dereferencing supporting recursive/circular schemas without infinite loops.
    - Parameter and security requirement inheritance & override merging.
    - Full preservation of JSON Schema composition keywords and constraints.
    - Zero remote network calls.
    """

    SUPPORTED_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
    SUPPORTED_VERSIONS = ("3.0", "3.1")

    def __init__(self, fallback_base_url: Optional[str] = None):
        """
        Initialize the parser.
        
        Args:
            fallback_base_url: Optional URL to use if the specification defines no servers.
                               Never assumed silently; only used if explicitly provided.
        """
        self.fallback_base_url = fallback_base_url.rstrip("/") if fallback_base_url else None

    @classmethod
    def parse_file(
        cls, file_path: Union[str, Path], fallback_base_url: Optional[str] = None
    ) -> ParsedAPISpec:
        """Parse an OpenAPI specification from a local file path (JSON or YAML)."""
        path = Path(file_path)
        if not path.is_file():
            raise SpecParsingError(f"Specification file not found: {file_path}")

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            raise SpecParsingError(f"Failed to read specification file: {e}") from e

        return cls(fallback_base_url=fallback_base_url).parse_string(content)

    def parse_string(self, content: str) -> ParsedAPISpec:
        """Parse an OpenAPI specification from a JSON or YAML string."""
        if not content or not content.strip():
            raise SpecParsingError("Empty specification content provided.")

        # Attempt JSON parsing first, fallback to YAML
        raw_dict: Optional[Dict[str, Any]] = None
        try:
            raw_dict = json.loads(content)
        except Exception:
            try:
                raw_dict = yaml.safe_load(content)
            except Exception as yaml_err:
                raise SpecParsingError(f"Failed to parse content as JSON or YAML: {yaml_err}") from yaml_err

        if not isinstance(raw_dict, dict):
            raise SpecParsingError("Specification content must evaluate to a JSON/YAML object (dictionary).")

        return self.parse_dict(raw_dict)

    def parse_dict(self, spec_dict: Dict[str, Any]) -> ParsedAPISpec:
        """Parse and normalize an OpenAPI specification dictionary."""
        # 1. Version Compatibility Check
        version = spec_dict.get("openapi")
        if not version or not isinstance(version, str):
            if "swagger" in spec_dict:
                raise SpecValidationError(
                    f"OpenAPI 2.0 (Swagger '{spec_dict['swagger']}') is not supported. "
                    "Only OpenAPI 3.0.x and 3.1.x are supported."
                )
            raise SpecValidationError("Missing or invalid 'openapi' version field in specification.")

        if not any(version.startswith(v) for v in self.SUPPORTED_VERSIONS):
            raise SpecValidationError(
                f"Unsupported OpenAPI version '{version}'. Only OpenAPI 3.0.x and 3.1.x are supported."
            )

        # 2. Strict Security Pre-Scan: Reject external / remote / file references
        self._enforce_security_policy(spec_dict)

        # 3. Structural Validation against OpenAPI Meta-Schema
        try:
            validate(spec_dict)
        except OpenAPIValidationError as e:
            raise SpecValidationError(f"OpenAPI structural validation error: {e}") from e
        except Exception as e:
            raise SpecValidationError(f"Specification failed schema validation: {e}") from e

        # 4. Safe Internal Dereferencing (handling circular structures cleanly)
        dereferenced_spec = self._safe_dereference(spec_dict)

        # 5. Extract Normalized Components
        return self._build_parsed_spec(dereferenced_spec, version)

    # -------------------------------------------------------------------------
    # Security & Reference Resolution
    # -------------------------------------------------------------------------

    def _enforce_security_policy(self, node: Any) -> None:
        """
        Recursively scans the specification tree for any external references.
        Rejects external HTTP/HTTPS, file://, ftp://, and cross-file relative references.
        Only internal local pointer references beginning with '#/' are permitted.
        """
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    if not isinstance(v, str):
                        raise SpecSecurityError(f"Invalid non-string $ref found: {v}")
                    ref_str = v.strip()
                    # Check for remote or file protocol
                    if re.match(r"^(https?|file|ftp):", ref_str, re.IGNORECASE):
                        raise SpecSecurityError(
                            f"Security violation: External remote $ref reference is forbidden: '{ref_str}'"
                        )
                    # Check for cross-file relative references (e.g. 'models.yaml#/User')
                    if not ref_str.startswith("#/"):
                        raise SpecSecurityError(
                            f"Security violation: External file reference is forbidden: '{ref_str}'. "
                            "Only local references starting with '#/' are permitted."
                        )
                else:
                    self._enforce_security_policy(v)
        elif isinstance(node, list):
            for item in node:
                self._enforce_security_policy(item)

    def _resolve_json_pointer(self, root: Dict[str, Any], pointer: str) -> Any:
        """
        Resolves a local JSON pointer (RFC 6901) against the root dictionary.
        Handles standard ~1 (/ ) and ~0 (~ ) unescaping.
        """
        parts = pointer.lstrip("#/").split("/")
        curr = root
        for p in parts:
            p = p.replace("~1", "/").replace("~0", "~")
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            elif isinstance(curr, list) and p.isdigit() and int(p) < len(curr):
                curr = curr[int(p)]
            else:
                raise SpecParsingError(f"Unable to resolve internal $ref pointer: '{pointer}' (missing key '{p}')")
        return curr

    def _safe_dereference(
        self, node: Any, root: Optional[Dict[str, Any]] = None, seen_refs: Tuple[str, ...] = ()
    ) -> Any:
        """
        Recursively dereferences internal $ref pointers.
        If a circular reference is encountered within the current resolution stack,
        it preserves the reference as {"$ref": ref} to prevent infinite recursion.
        Preserves sibling properties defined alongside $ref (OpenAPI 3.1 compliance).
        """
        if root is None:
            root = node

        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                # Circular cycle detection
                if ref in seen_refs:
                    # Return reference pointer intact to break infinite loop
                    return {"$ref": ref}

                # Resolve pointer
                target = self._resolve_json_pointer(root, ref)
                resolved = self._safe_dereference(target, root, seen_refs + (ref,))

                # In OpenAPI 3.1, sibling keys alongside $ref override/supplement target values
                if isinstance(resolved, dict):
                    merged = dict(resolved)
                    for k, v in node.items():
                        if k != "$ref":
                            merged[k] = self._safe_dereference(v, root, seen_refs)
                    return merged
                return resolved

            return {k: self._safe_dereference(v, root, seen_refs) for k, v in node.items()}

        elif isinstance(node, list):
            return [self._safe_dereference(item, root, seen_refs) for item in node]

        return node

    # -------------------------------------------------------------------------
    # Normalization & Model Extraction
    # -------------------------------------------------------------------------

    def _build_parsed_spec(self, spec: Dict[str, Any], openapi_version: str) -> ParsedAPISpec:
        """Constructs the strongly-typed ParsedAPISpec model from dereferenced data."""
        info = spec.get("info", {})
        title = info.get("title", "Untitled API")
        version = info.get("version", "1.0.0")
        description = info.get("description")

        # Root Servers
        root_servers = self._extract_servers(spec.get("servers", []))

        # Root Security
        global_security = spec.get("security", [])

        # Security Schemes
        security_schemes = self._extract_security_schemes(spec.get("components", {}).get("securitySchemes", {}))

        # Endpoints
        endpoints = []
        paths_obj = spec.get("paths", {})
        for path_pattern, path_item in paths_obj.items():
            if not isinstance(path_item, dict):
                continue

            path_servers = self._extract_servers(path_item.get("servers", [])) or root_servers
            path_parameters = path_item.get("parameters", [])

            for method_name, op_item in path_item.items():
                if method_name.lower() not in self.SUPPORTED_METHODS:
                    continue
                if not isinstance(op_item, dict):
                    continue

                endpoint = self._normalize_operation(
                    path=path_pattern,
                    method=method_name.upper(),
                    op_data=op_item,
                    path_parameters=path_parameters,
                    path_servers=path_servers,
                    global_security=global_security,
                )
                endpoints.append(endpoint)

        return ParsedAPISpec(
            title=title,
            version=version,
            openapi_version=openapi_version,
            description=description,
            servers=root_servers,
            endpoints=endpoints,
            security_schemes=security_schemes,
            global_security=global_security,
            raw_spec=spec,
        )

    def _extract_servers(self, servers_list: List[Dict[str, Any]]) -> List[str]:
        """Extracts and cleans server URLs from servers declaration."""
        urls = []
        if isinstance(servers_list, list):
            for s in servers_list:
                if isinstance(s, dict) and "url" in s:
                    url = s["url"].strip().rstrip("/")
                    if url:
                        urls.append(url)
        return urls

    def _extract_security_schemes(
        self, raw_schemes: Dict[str, Any]
    ) -> Dict[str, SecuritySchemeDefinition]:
        """Normalizes components/securitySchemes into Pydantic models."""
        normalized = {}
        for name, scheme in raw_schemes.items():
            if not isinstance(scheme, dict):
                continue
            scheme_type = scheme.get("type")
            if scheme_type not in ("apiKey", "http", "oauth2", "openIdConnect"):
                continue

            normalized[name] = SecuritySchemeDefinition(
                type=scheme_type,
                description=scheme.get("description"),
                name=scheme.get("name"),
                in_=scheme.get("in"),
                scheme=scheme.get("scheme"),
                bearer_format=scheme.get("bearerFormat"),
                flows=scheme.get("flows"),
                open_id_connect_url=scheme.get("openIdConnectUrl"),
            )
        return normalized

    def _normalize_operation(
        self,
        path: str,
        method: str,
        op_data: Dict[str, Any],
        path_parameters: List[Dict[str, Any]],
        path_servers: List[str],
        global_security: List[Dict[str, List[str]]],
    ) -> EndpointDefinition:
        """Normalizes a single HTTP operation, merging parameters, servers, and security."""
        # 1. Merge Parameters: (name, in) unique identity
        # Operation parameters override path-level parameters with the same (name, in).
        op_parameters = op_data.get("parameters", [])
        merged_params: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # First add path-level parameters
        for p in path_parameters:
            if isinstance(p, dict) and "name" in p and "in" in p:
                key = (p["name"], p["in"])
                merged_params[key] = p

        # Operation-level parameters override path-level
        for p in op_parameters:
            if isinstance(p, dict) and "name" in p and "in" in p:
                key = (p["name"], p["in"])
                merged_params[key] = p

        normalized_parameters: List[ParameterDefinition] = []
        for p_dict in merged_params.values():
            normalized_parameters.append(
                ParameterDefinition(
                    name=p_dict["name"],
                    in_=p_dict["in"],
                    required=p_dict.get("required", False if p_dict["in"] != "path" else True),
                    description=p_dict.get("description"),
                    deprecated=p_dict.get("deprecated", False),
                    schema=p_dict.get("schema", {}),
                    example=p_dict.get("example"),
                    examples=p_dict.get("examples"),
                    style=p_dict.get("style"),
                    explode=p_dict.get("explode"),
                )
            )

        # 2. Merge Security:
        # If 'security' key is present in operation (even if empty []), it overrides global.
        # If omitted, inherits global_security.
        if "security" in op_data:
            effective_security = op_data.get("security", [])
        else:
            effective_security = global_security

        # 3. Merge Servers:
        # Operation servers -> Path servers -> Fallback base URL (if explicitly set)
        op_servers = self._extract_servers(op_data.get("servers", []))
        effective_servers = op_servers or path_servers
        if not effective_servers and self.fallback_base_url:
            effective_servers = [self.fallback_base_url]

        # 4. Request Body:
        request_body_def: Optional[RequestBodyDefinition] = None
        raw_rb = op_data.get("requestBody")
        if isinstance(raw_rb, dict):
            request_body_def = RequestBodyDefinition(
                description=raw_rb.get("description"),
                required=raw_rb.get("required", False),
                content=raw_rb.get("content", {}),
            )

        # 5. Responses: Preserve status codes including 'default', '2XX', '4XX'
        normalized_responses: Dict[str, ResponseDefinition] = {}
        raw_responses = op_data.get("responses", {})
        if isinstance(raw_responses, dict):
            for status_key, resp_obj in raw_responses.items():
                if isinstance(resp_obj, dict):
                    normalized_responses[str(status_key)] = ResponseDefinition(
                        status_code=str(status_key),
                        description=resp_obj.get("description", ""),
                        headers=resp_obj.get("headers", {}),
                        content=resp_obj.get("content", {}),
                    )

        return EndpointDefinition(
            path=path,
            method=method,  # type: ignore[arg-type]
            summary=op_data.get("summary"),
            description=op_data.get("description"),
            operation_id=op_data.get("operationId"),
            tags=op_data.get("tags", []),
            deprecated=op_data.get("deprecated", False),
            parameters=normalized_parameters,
            request_body=request_body_def,
            responses=normalized_responses,
            security=effective_security,
            servers=effective_servers,
        )

