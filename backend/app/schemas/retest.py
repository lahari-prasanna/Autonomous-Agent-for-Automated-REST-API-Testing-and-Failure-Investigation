"""
Validated, immutable Pydantic schemas for Phase 4C Retest Decisions and Audit Tracking.
"""

from enum import Enum
import re
from typing import Any, Literal, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.schemas.failure import RetryPolicy
from backend.app.schemas.sanitizer import ReportDataSanitizer


class RetestAction(str, Enum):
    NO_ACTION = "no_action"
    RETRY_TRANSIENT = "retry_transient"
    RUN_VARIATION_TEST = "run_variation_test"


class PerturbationStrategy(str, Enum):
    NONE = "none"
    FIX_REQUIRED_FIELDS = "fix_required_fields"
    BOUNDARY_CLAMP = "boundary_clamp"
    ENUM_ALTERNATIVE = "enum_alternative"


class PolicyAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    is_authorized: bool = Field(...)
    policy_name: str = Field(...)
    authorizing_rule: str = Field(...)
    timestamp: str = Field(...)
    audit_token: str = Field(...)

    @field_validator("policy_name", "authorizing_rule", "audit_token", mode="before")
    @classmethod
    def _sanitize_policy(cls, v: str) -> str:
        s = ReportDataSanitizer.sanitize_text(v)
        if not s:
            raise ValueError("Policy authorization fields must be non-empty strings")
        return s


class ParameterPerturbation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    parameter_name: str = Field(...)
    parameter_location: Literal["path", "query", "header", "body"] = Field(...)
    original_value: Optional[Any] = Field(default=None)
    varied_value: Any = Field(...)
    constraint_applied: str = Field(...)
    pre_execution_validated: bool = Field(...)

    @field_validator("parameter_name", "constraint_applied", mode="before")
    @classmethod
    def _sanitize_fields(cls, v: str) -> str:
        return ReportDataSanitizer.sanitize_text(v)

    @field_validator("original_value", "varied_value", mode="before")
    @classmethod
    def _sanitize_vals(cls, v: Any, info: Any) -> Any:
        name = info.data.get("parameter_name", "") if hasattr(info, "data") else ""
        return ReportDataSanitizer.sanitize_parameter_value(str(name), v)


class RetestEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: bool = Field(...)
    action: RetestAction = Field(...)
    is_idempotent_method: bool = Field(...)
    remaining_followup_budget: int = Field(..., ge=0, le=2)
    reason: str = Field(...)

    @field_validator("reason", mode="before")
    @classmethod
    def _sanitize_reason(cls, v: str) -> str:
        return ReportDataSanitizer.sanitize_text(v)


class RetestDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: RetestAction = Field(...)
    retry_of: Optional[str] = Field(default="initial")  # "initial" or candidate_id
    backoff_delay_seconds: float = Field(default=0.0, ge=0.0, le=5.0)
    perturbation_strategy: PerturbationStrategy = Field(default=PerturbationStrategy.NONE)
    planned_perturbations: Tuple[ParameterPerturbation, ...] = Field(default_factory=tuple)
    authorization: Optional[PolicyAuthorization] = Field(default=None)
    rationale: str = Field(...)

    @field_validator("rationale", "retry_of", mode="before")
    @classmethod
    def _sanitize_strings(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("planned_perturbations", mode="before")
    @classmethod
    def _convert_tuple(cls, v: Any) -> Tuple[ParameterPerturbation, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("planned_perturbations must be a sequence")
        return tuple(v)

