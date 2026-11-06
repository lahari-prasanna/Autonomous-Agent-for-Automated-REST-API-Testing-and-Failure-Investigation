"""
Idempotency-aware, bounded retest controller and perturbation engine (Phase 4C).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import email.utils
import hashlib
import hmac
import json
import logging
import math
import re
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union
import urllib.parse

from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.failure import FailureCategory, FailureClassification, RetryPolicy
from backend.app.schemas.retest import (
    ParameterPerturbation,
    PerturbationStrategy,
    PolicyAuthorization,
    RetestAction,
    RetestDecision,
    RetestEligibility,
)
from backend.app.schemas.sanitizer import ReportDataSanitizer
from backend.app.schemas.spec import EndpointDefinition, ParameterDefinition, ParsedAPISpec

logger = logging.getLogger(__name__)

IDEMPOTENT_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
NON_IDEMPOTENT_HTTP_METHODS = {"POST", "PATCH"}
DELTA_SECONDS_PATTERN = re.compile(r"^\d+$")


class RetestController:
    """
    Stateless policy engine for Phase 4C follow-ups.
    Enforces the combined 2-follow-up budget, RFC 9110 method idempotency,
    safe Retry-After evaluation, canonical request fingerprinting, and fail-closed variation gates.
    """

    MAX_FOLLOWUPS: int = 2
    MAX_RETRY_DELAY_SECONDS: float = 5.0
    DEFAULT_BACKOFF_ATTEMPT_1: float = 1.0
    DEFAULT_BACKOFF_ATTEMPT_2: float = 2.0

    @classmethod
    def compute_canonical_request_fingerprint(
        cls,
        req: ExecutionRequest,
        ephemeral_hmac_key: bytes,
    ) -> str:
        """
        Computes an ephemeral 64-hex HMAC-SHA256 fingerprint from the actual effective request.
        Normalizes method, effective URL (without query), ordered query params,
        relevant headers, and body bytes. Never logs the key or raw payload.
        """
        method = req.method.strip().upper()

        # Split effective URL to separate query parameters
        parsed_url = urllib.parse.urlsplit(req.url)
        url_without_query = urllib.parse.urlunsplit((parsed_url.scheme.lower(), parsed_url.netloc, parsed_url.path, "", ""))

        # Combine URL query string parameters and req.params in deterministic ordered fashion
        combined_params: List[Tuple[str, str]] = []
        if parsed_url.query:
            combined_params.extend(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))
        if req.params:
            for k, v in req.params.items():
                if isinstance(v, (list, tuple)):
                    for item in v:
                        combined_params.append((str(k), str(item)))
                else:
                    combined_params.append((str(k), str(v)))
        # Sort params by key, then value for canonical form
        combined_params.sort(key=lambda pair: (pair[0], pair[1]))

        # Normalize relevant headers (lowercase names, sorted, excluding volatile transport headers)
        EXCLUDED_HEADERS = {"date", "authorization", "proxy-authorization", "cookie", "user-agent"}
        normalized_headers: List[Tuple[str, str]] = []
        if req.headers:
            for k, v in req.headers.items():
                k_lower = str(k).strip().lower()
                if k_lower not in EXCLUDED_HEADERS:
                    normalized_headers.append((k_lower, str(v).strip()))
        normalized_headers.sort(key=lambda pair: pair[0])

        # Compute body hash
        body_bytes = b""
        if req.raw_body:
            body_bytes = req.raw_body if isinstance(req.raw_body, bytes) else str(req.raw_body).encode("utf-8")
        elif req.json_body is not None:
            body_bytes = json.dumps(req.json_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()

        canonical_text = (
            f"METHOD:{method}\n"
            f"URL:{url_without_query}\n"
            f"PARAMS:{json.dumps(combined_params, separators=(',', ':'))}\n"
            f"HEADERS:{json.dumps(normalized_headers, separators=(',', ':'))}\n"
            f"BODY_SHA256:{body_sha256}\n"
        )

        return hmac.new(ephemeral_hmac_key, canonical_text.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def parse_retry_after(cls, header_val: Optional[str]) -> Optional[float]:
        """
        Parses RFC 9110 §10.2.3 Retry-After header.
        Accepts integer delta-seconds or HTTP-date. Rejects decimal seconds as malformed.
        Returns float seconds, or None if malformed or absent.
        """
        if not header_val:
            return None
        val = str(header_val).strip()

        # 1. Integer delta-seconds
        if DELTA_SECONDS_PATTERN.match(val):
            return float(int(val))

        # Rejects decimal seconds like "1.5"
        if "." in val and val.replace(".", "", 1).isdigit():
            logger.warning(f"Malformed Retry-After rejected (decimal seconds): '{val}'")
            return None

        # 2. HTTP-date parsing
        try:
            target_dt = email.utils.parsedate_to_datetime(val)
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=timezone.utc)
            now_dt = datetime.now(timezone.utc)
            diff_seconds = (target_dt - now_dt).total_seconds()
            # If past date, return 0.0s
            return max(0.0, diff_seconds)
        except Exception:
            logger.warning(f"Malformed Retry-After rejected (unparseable HTTP-date): '{val}'")
            return None

    @classmethod
    def evaluate_retry_eligibility(
        cls,
        http_method: str,
        classification: FailureClassification,
        retry_after_header: Optional[str] = None,
        remaining_deadline: Optional[float] = None,
        followup_count: int = 0,
        retry_of: str = "initial",
    ) -> RetestEligibility:
        """
        Evaluates whether an operation attempt is eligible for an identical transient retry.
        Enforces RFC 9110 idempotency, max follow-up budget, and safe Retry-After bounds.
        """
        method = http_method.strip().upper()
        is_idempotent = method in IDEMPOTENT_HTTP_METHODS

        # 1. Budget check
        remaining_budget = max(0, cls.MAX_FOLLOWUPS - followup_count)
        if remaining_budget <= 0:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=is_idempotent,
                remaining_followup_budget=0,
                reason="Follow-up budget exhausted (maximum 2 follow-ups reached).",
            )

        # 2. Method Idempotency Check
        if not is_idempotent:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=False,
                remaining_followup_budget=remaining_budget,
                reason=f"HTTP method {method} is non-idempotent; automated transient retry is prohibited.",
            )

        # 3. Category & Policy Check
        cat = classification.primary_category
        # SSRF block, 500 server crash, or contract schema mismatches are unrecoverable via retry
        if cat == FailureCategory.SSRF_SECURITY_BLOCK:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=is_idempotent,
                remaining_followup_budget=remaining_budget,
                reason="Destination was intercepted by SSRF security guard; retries are forbidden.",
            )

        if cat == FailureCategory.RESPONSE_SCHEMA_MISMATCH or cat == FailureCategory.RESPONSE_CONTENT_TYPE_MISMATCH:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=is_idempotent,
                remaining_followup_budget=remaining_budget,
                reason="Server response violated contract schema; identical retry cannot resolve server defect.",
            )

        if cat == FailureCategory.REQUEST_VALIDATION_FAILURE or cat == FailureCategory.RESOURCE_NOT_FOUND or cat == FailureCategory.AUTHENTICATION_FAILURE:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=is_idempotent,
                remaining_followup_budget=remaining_budget,
                reason=f"Failure category {cat.value if cat else 'client_error'} requires parameter variation or external change, not identical retry.",
            )

        # Only transient categories on idempotent methods qualify for transient retry
        TRANSIENT_CATEGORIES = {
            FailureCategory.REQUEST_TIMEOUT,
            FailureCategory.NETWORK_FAILURE,
            FailureCategory.RATE_LIMITING,
            FailureCategory.SERVER_ERROR,  # 502, 503, 504 gateway transient
        }
        if cat not in TRANSIENT_CATEGORIES:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=is_idempotent,
                remaining_followup_budget=remaining_budget,
                reason=f"Failure category {cat.value if cat else 'unknown'} is not eligible for transient retry.",
            )

        # Check status code for 500 vs 502/503/504
        status_code = classification.evidence.status_code if classification.evidence else None
        if status_code == 500:
            return RetestEligibility(
                eligible=False,
                action=RetestAction.NO_ACTION,
                is_idempotent_method=is_idempotent,
                remaining_followup_budget=remaining_budget,
                reason="Server returned 500 Internal Server Error; repeating identical request cannot resolve server bug.",
            )

        # 4. Retry-After & Deadline Check
        parsed_delay = cls.parse_retry_after(retry_after_header)
        if parsed_delay is not None:
            if parsed_delay > cls.MAX_RETRY_DELAY_SECONDS:
                return RetestEligibility(
                    eligible=False,
                    action=RetestAction.NO_ACTION,
                    is_idempotent_method=is_idempotent,
                    remaining_followup_budget=remaining_budget,
                    reason=f"Server requested Retry-After delay ({parsed_delay:.1f}s) exceeds maximum allowable threshold ({cls.MAX_RETRY_DELAY_SECONDS}s); retry skipped to honor server constraint.",
                )
            if remaining_deadline is not None and parsed_delay > remaining_deadline:
                return RetestEligibility(
                    eligible=False,
                    action=RetestAction.NO_ACTION,
                    is_idempotent_method=is_idempotent,
                    remaining_followup_budget=remaining_budget,
                    reason=f"Server requested Retry-After delay ({parsed_delay:.1f}s) exceeds remaining test suite deadline ({remaining_deadline:.1f}s); retry skipped.",
                )

        # Eligible for identical transient retry
        return RetestEligibility(
            eligible=True,
            action=RetestAction.RETRY_TRANSIENT,
            is_idempotent_method=is_idempotent,
            remaining_followup_budget=remaining_budget,
            reason=f"Eligible transient failure on idempotent method {method}; retrying {retry_of}.",
        )

    @classmethod
    def decide_transient_retry(
        cls,
        http_method: str,
        classification: FailureClassification,
        retry_after_header: Optional[str] = None,
        remaining_deadline: Optional[float] = None,
        followup_count: int = 0,
        retry_of: str = "initial",
    ) -> RetestDecision:
        """Constructs an actionable RetestDecision for an identical transient retry."""
        eligibility = cls.evaluate_retry_eligibility(
            http_method=http_method,
            classification=classification,
            retry_after_header=retry_after_header,
            remaining_deadline=remaining_deadline,
            followup_count=followup_count,
            retry_of=retry_of,
        )

        if not eligibility.eligible:
            return RetestDecision(
                action=RetestAction.NO_ACTION,
                retry_of=retry_of,
                backoff_delay_seconds=0.0,
                perturbation_strategy=PerturbationStrategy.NONE,
                planned_perturbations=tuple(),
                authorization=None,
                rationale=eligibility.reason,
            )

        # Determine exact backoff delay
        parsed_delay = cls.parse_retry_after(retry_after_header)
        if parsed_delay is not None:
            delay = parsed_delay
        else:
            delay = cls.DEFAULT_BACKOFF_ATTEMPT_1 if followup_count == 0 else cls.DEFAULT_BACKOFF_ATTEMPT_2

        return RetestDecision(
            action=RetestAction.RETRY_TRANSIENT,
            retry_of=retry_of,
            backoff_delay_seconds=min(delay, cls.MAX_RETRY_DELAY_SECONDS),
            perturbation_strategy=PerturbationStrategy.NONE,
            planned_perturbations=tuple(),
            authorization=None,
            rationale=eligibility.reason,
        )

    @classmethod
    def synthesize_variation_candidate(
        cls,
        candidate_id: str,
        operation: EndpointDefinition,
        classification: FailureClassification,
        followup_count: int = 0,
        policy_authorization: Optional[PolicyAuthorization] = None,
    ) -> Tuple[Optional[RetestDecision], Optional[Dict[str, Any]], Dict[str, Any]]:
        """
        Synthesizes an OpenAPI schema-constrained variation candidate for client validation errors (400, 422).
        Fails closed on non-idempotent methods unless authorized.
        Returns: (RetestDecision, candidate_record_data, candidate_metadata).
        """
        method = operation.method.strip().upper()
        is_non_idempotent = method in NON_IDEMPOTENT_HTTP_METHODS

        # Check Gate 1: Budget
        budget_approved = (followup_count < cls.MAX_FOLLOWUPS)

        # Check Gate 2: Authorization (Fail closed for POST/PATCH)
        authorization_approved = True
        if is_non_idempotent:
            if policy_authorization is None or not policy_authorization.is_authorized:
                authorization_approved = False

        # Check Gate 3: Schema validation & parameter perturbation synthesis
        schema_validation_passed = False
        strategy = PerturbationStrategy.NONE
        target_param = "unknown"
        param_location: Literal["path", "query", "header", "body"] = "query"
        applied_constraint = "none"
        planned_perturbations: List[ParameterPerturbation] = []

        # Inspect operation parameters and request body to synthesize schema-valid perturbation
        ev = classification.evidence
        failed_keywords = set(ev.schema_keywords) if ev and ev.schema_keywords else set()
        failed_paths = list(ev.json_paths) if ev and ev.json_paths else []

        # Strategy A: Boundary clamp or fix integer parameter
        perturbed = False
        for p in operation.parameters:
            p_schema = p.schema_dict or {}
            p_type = p_schema.get("type")
            p_min = p_schema.get("minimum")
            p_max = p_schema.get("maximum")

            if p_type in ("integer", "number") and (p_min is not None or p_max is not None):
                target_param = p.name
                param_location = p.in_
                strategy = PerturbationStrategy.BOUNDARY_CLAMP
                clamped_val = p_min if p_min is not None else p_max
                applied_constraint = f"boundary_clamp(type={p_type}, min={p_min}, max={p_max})"
                planned_perturbations.append(
                    ParameterPerturbation(
                        parameter_name=p.name,
                        parameter_location=p.in_,
                        original_value=None,
                        varied_value=clamped_val,
                        constraint_applied=applied_constraint,
                        pre_execution_validated=True,
                    )
                )
                schema_validation_passed = True
                perturbed = True
                break

            elif p_schema.get("enum"):
                target_param = p.name
                param_location = p.in_
                strategy = PerturbationStrategy.ENUM_ALTERNATIVE
                enum_vals = p_schema.get("enum", [])
                alt_val = enum_vals[0] if enum_vals else "valid_option"
                applied_constraint = f"enum_choice({alt_val})"
                planned_perturbations.append(
                    ParameterPerturbation(
                        parameter_name=p.name,
                        parameter_location=p.in_,
                        original_value=None,
                        varied_value=alt_val,
                        constraint_applied=applied_constraint,
                        pre_execution_validated=True,
                    )
                )
                schema_validation_passed = True
                perturbed = True
                break

        if not perturbed:
            # Fallback to query parameter default or example if available
            for p in operation.parameters:
                if p.example is not None or p.schema_dict.get("default") is not None:
                    val = p.example if p.example is not None else p.schema_dict.get("default")
                    target_param = p.name
                    param_location = p.in_
                    strategy = PerturbationStrategy.FIX_REQUIRED_FIELDS
                    applied_constraint = f"schema_example({val})"
                    planned_perturbations.append(
                        ParameterPerturbation(
                            parameter_name=p.name,
                            parameter_location=p.in_,
                            original_value=None,
                            varied_value=val,
                            constraint_applied=applied_constraint,
                            pre_execution_validated=True,
                        )
                    )
                    schema_validation_passed = True
                    perturbed = True
                    break

        if not perturbed:
            target_param = "parameters"
            strategy = PerturbationStrategy.NONE
            applied_constraint = "no_applicable_schema_constraint_found"
            schema_validation_passed = False

        approved_for_dispatch = schema_validation_passed and authorization_approved and budget_approved

        rejection_reason = None
        if not approved_for_dispatch:
            if not schema_validation_passed:
                rejection_reason = "SchemaValidationFailed: No applicable OpenAPI constraints could be satisfied."
            elif not authorization_approved:
                rejection_reason = "PolicyAuthorizationDenied: Non-idempotent mutations prohibited by default."
            elif not budget_approved:
                rejection_reason = "BudgetExhausted: Maximum 2 follow-ups reached."

        candidate_data = {
            "candidate_id": candidate_id,
            "strategy": strategy,
            "target_parameter": target_param,
            "parameter_location": param_location,
            "applicable_constraint": applied_constraint,
            "schema_validation_passed": schema_validation_passed,
            "authorization_approved": authorization_approved,
            "budget_approved": budget_approved,
            "approved_for_dispatch": approved_for_dispatch,
            "dispatched": False,  # Updated when dispatch is attempted
            "rejection_reason": rejection_reason,
            "dispatch_error": None,
        }

        if not approved_for_dispatch:
            decision = RetestDecision(
                action=RetestAction.NO_ACTION,
                retry_of=None,
                backoff_delay_seconds=0.0,
                perturbation_strategy=strategy,
                planned_perturbations=tuple(planned_perturbations),
                authorization=policy_authorization,
                rationale=rejection_reason or "Candidate not approved for dispatch.",
            )
            return decision, candidate_data, {}

        decision = RetestDecision(
            action=RetestAction.RUN_VARIATION_TEST,
            retry_of=None,
            backoff_delay_seconds=0.0,
            perturbation_strategy=strategy,
            planned_perturbations=tuple(planned_perturbations),
            authorization=policy_authorization,
            rationale=f"Approved variation test '{candidate_id}' exploring {strategy.value} on {target_param}.",
        )

        return decision, candidate_data, {"perturbations": planned_perturbations}

