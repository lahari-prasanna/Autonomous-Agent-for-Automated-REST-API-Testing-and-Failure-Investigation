"""
Diagnostic report generation and secure Markdown serialization service (Phase 4C).
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, List, Optional, Sequence, Tuple
import uuid

from backend.app.schemas.report import (
    DiagnosticReport,
    OperationRunRecord,
    OverallRunStatus,
    RunSummaryStatistics,
    VariationMetricsSummary,
)
from backend.app.schemas.sanitizer import ReportDataSanitizer

logger = logging.getLogger(__name__)


class DiagnosticReportGenerator:
    """
    Compiles operation test records into validated DiagnosticReport schemas,
    JSON exports, and safe, neutralized GitHub-flavored Markdown.
    """

    @classmethod
    def build_report(
        cls,
        operations: Sequence[OperationRunRecord],
        api_title: str,
        api_version: str,
        base_url: Optional[str] = None,
        total_duration_ms: float = 0.0,
        global_remediation_summary: Optional[Sequence[str]] = None,
        report_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> DiagnosticReport:
        """
        Calculates exact metric reconciliations and instantiates a validated DiagnosticReport.
        """
        ops_tuple = tuple(operations)
        total_ops = len(ops_tuple)

        # 1. Operation category counts
        passed_cnt = sum(1 for op in ops_tuple if op.final_status == OverallRunStatus.PASSED)
        resolved_retry_cnt = sum(1 for op in ops_tuple if op.final_status == OverallRunStatus.RESOLVED_AFTER_TRANSIENT_RETRY)
        failed_cnt = sum(1 for op in ops_tuple if op.final_status == OverallRunStatus.FAILED)
        error_cnt = sum(1 for op in ops_tuple if op.final_status == OverallRunStatus.ERROR)

        # 2. Network requests dispatched
        total_requests = (
            sum(1 for op in ops_tuple if op.initial_attempt.dispatched)
            + sum(1 for op in ops_tuple for r in op.transient_retries if r.outcome.dispatched)
            + sum(1 for op in ops_tuple for v in op.variation_tests if v.outcome.dispatched)
        )
        total_retries = sum(len(op.transient_retries) for op in ops_tuple)

        # 3. Candidate metrics reconciliation
        act_c_gen = sum(len(op.variation_candidates) for op in ops_tuple)
        act_c_val = sum(sum(1 for c in op.variation_candidates if c.schema_validation_passed) for op in ops_tuple)
        act_c_app = sum(sum(1 for c in op.variation_candidates if c.approved_for_dispatch) for op in ops_tuple)
        act_c_rej = sum(sum(1 for c in op.variation_candidates if not c.approved_for_dispatch) for op in ops_tuple)
        act_c_disp = sum(sum(1 for c in op.variation_candidates if c.dispatched) for op in ops_tuple)
        act_c_fail = sum(sum(1 for c in op.variation_candidates if c.approved_for_dispatch and not c.dispatched) for op in ops_tuple)

        act_rej_schema = sum(sum(1 for c in op.variation_candidates if not c.schema_validation_passed) for op in ops_tuple)
        act_rej_auth = sum(sum(1 for c in op.variation_candidates if c.schema_validation_passed and not c.authorization_approved) for op in ops_tuple)
        act_rej_budget = sum(sum(1 for c in op.variation_candidates if c.schema_validation_passed and c.authorization_approved and not c.budget_approved) for op in ops_tuple)

        act_v_pass = sum(sum(1 for v in op.variation_tests if v.outcome.passed) for op in ops_tuple)
        act_v_fail = sum(sum(1 for v in op.variation_tests if not v.outcome.passed) for op in ops_tuple)

        var_success_rate = round((act_v_pass / act_c_disp) * 100.0, 2) if act_c_disp > 0 else 0.0
        op_pass_rate = round(((passed_cnt + resolved_retry_cnt) / total_ops) * 100.0, 2) if total_ops > 0 else 0.0

        variation_metrics = VariationMetricsSummary(
            candidates_generated=act_c_gen,
            candidates_schema_valid=act_c_val,
            candidates_rejected=act_c_rej,
            candidates_approved=act_c_app,
            candidates_dispatched=act_c_disp,
            candidates_dispatch_failed=act_c_fail,
            rejections_schema_invalid=act_rej_schema,
            rejections_unauthorized=act_rej_auth,
            rejections_budget_exhausted=act_rej_budget,
            variation_tests_passed=act_v_pass,
            variation_tests_failed=act_v_fail,
            variation_success_rate_percentage=var_success_rate,
        )

        summary = RunSummaryStatistics(
            total_operations=total_ops,
            passed_operations=passed_cnt,
            resolved_by_transient_retry=resolved_retry_cnt,
            failed_operations=failed_cnt,
            error_operations=error_cnt,
            total_transient_retries_dispatched=total_retries,
            total_requests_executed=total_requests,
            pass_rate_percentage=op_pass_rate,
            total_duration_ms=ReportDataSanitizer.ensure_finite_non_negative(total_duration_ms, "total_duration_ms"),
            variation_metrics=variation_metrics,
        )

        from datetime import datetime, timezone
        rep_id = report_id or f"rep_{uuid.uuid4().hex[:12]}"
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        return DiagnosticReport(
            report_id=rep_id,
            timestamp=ts,
            api_title=api_title,
            api_version=api_version,
            base_url=base_url,
            summary=summary,
            operations=ops_tuple,
            global_remediation_summary=tuple(global_remediation_summary or ()),
        )

    @classmethod
    def to_json(cls, report: DiagnosticReport, indent: int = 2) -> str:
        """Serializes report model to formatted JSON string."""
        return report.model_dump_json(indent=indent)

    # -----------------------------------------------------------------------
    # Context-Specific Safe Markdown Rendering
    # -----------------------------------------------------------------------

    @classmethod
    def render_safe_inline(cls, text: Optional[str]) -> str:
        """Escapes HTML entities and Markdown formatting syntax in inline text."""
        if not text:
            return ""
        s = html.escape(str(text), quote=True)
        # Escape Markdown span triggers: * _ ~ [ ] ` ( )
        s = re.sub(r"([*_~\[\]`()])", r"\\\1", s)
        # Escape Markdown line-start block triggers: # > - +
        s = re.sub(r"(?m)^([#>+\-])", r"\\\1", s)
        return s

    @classmethod
    def render_safe_code_span(cls, text: Optional[str]) -> str:
        """Renders inert backtick code span, using double backticks if backticks are present."""
        if text is None:
            return "`None`"
        s = str(text)
        if "`" in s:
            return f"`` {s} ``"
        return f"`{s}`"

    @classmethod
    def render_inert_url(cls, url: Optional[str]) -> str:
        """Renders URLs strictly as inert code spans, never as clickable autolinks or markdown links."""
        if not url:
            return "`None`"
        clean = ReportDataSanitizer.sanitize_source_reference(url) or "[UNSAFE URI SUPPRESSED]"
        return cls.render_safe_code_span(clean)

    @classmethod
    def render_fenced_block(cls, content: Optional[str], lang: str = "text") -> str:
        """
        Dynamically chooses a backtick fence longer than any run in content,
        and prefixes every line with '> ' so it cannot escape an enclosing blockquote.
        """
        if not content:
            content = ""
        s = str(content)

        # Find longest run of backticks or tildes
        backtick_runs = [len(m.group(0)) for m in re.finditer(r"`+", s)]
        tilde_runs = [len(m.group(0)) for m in re.finditer(r"~+", s)]
        max_run = max([0] + backtick_runs + tilde_runs)
        fence_len = max(4, max_run + 1)
        fence = "`" * fence_len

        lines = s.splitlines() if s else [""]
        out_lines = [f"> {fence}{lang}"]
        for line in lines:
            out_lines.append(f"> {line}")
        out_lines.append(f"> {fence}")
        return "\n".join(out_lines)

    @classmethod
    def to_markdown(cls, report: DiagnosticReport) -> str:
        """
        Renders an end-to-end GitHub-flavored Markdown report.
        Avoids fragile dynamic tables, neutralizes all active formatting,
        and keeps untrusted URLs and evidence strictly inert.
        """
        r = report
        s = r.summary
        vm = s.variation_metrics

        lines: List[str] = [
            f"# Diagnostic API Testing Report: {cls.render_safe_inline(r.api_title)} (v{cls.render_safe_inline(r.api_version)})",
            "",
            f"- **Report ID**: {cls.render_safe_code_span(r.report_id)}",
            f"- **Timestamp**: {cls.render_safe_code_span(r.timestamp)}",
            f"- **Base URL**: {cls.render_inert_url(r.base_url)}",
            f"- **Total Duration**: `{s.total_duration_ms:.2f} ms`",
            "",
            "## 1. Executive Summary",
            "",
            f"- **Total Operations Targeted**: `{s.total_operations}`",
            f"- **Operations Passed (Initial)**: `{s.passed_operations}`",
            f"- **Operations Resolved after Transient Retry**: `{s.resolved_by_transient_retry}`",
            f"- **Operations Failed**: `{s.failed_operations}`",
            f"- **Operations with Pre-Dispatch/Eval Error**: `{s.error_operations}`",
            f"- **Overall Operation Pass Rate**: **`{s.pass_rate_percentage:.2f}%`**",
            "",
            "### Network & Execution Metrics",
            f"- **Total Network HTTP Requests Executed**: `{s.total_requests_executed}`",
            f"- **Total Transient Retries Dispatched**: `{s.total_transient_retries_dispatched}`",
            "",
            "### Parameter Variation Explorations",
            f"- **Variation Candidates Generated**: `{vm.candidates_generated}`",
            f"- **Schema-Valid Candidates**: `{vm.candidates_schema_valid}`",
            f"- **Candidates Approved for Dispatch**: `{vm.candidates_approved}`",
            f"- **Candidates Rejected before Dispatch**: `{vm.candidates_rejected}`",
            f"  - Schema Invalid: `{vm.rejections_schema_invalid}`",
            f"  - Unauthorized (e.g. POST/PATCH): `{vm.rejections_unauthorized}`",
            f"  - Budget Exhausted: `{vm.rejections_budget_exhausted}`",
            f"- **Candidates Dispatched over HTTP**: `{vm.candidates_dispatched}`",
            f"- **Candidates Dispatch-Failed (Pre-Socket)**: `{vm.candidates_dispatch_failed}`",
            f"- **Dispatched Variation Tests Passed**: `{vm.variation_tests_passed}`",
            f"- **Dispatched Variation Tests Failed**: `{vm.variation_tests_failed}`",
            f"- **Variation Exploration Success Rate**: `{vm.variation_success_rate_percentage:.2f}%`",
            "",
            "---",
            "## 2. Operation Run Records",
            "",
        ]

        if not r.operations:
            lines.append("*No operations were executed in this suite.*")
            lines.append("")

        for idx, op in enumerate(r.operations, start=1):
            status_badge = f"**[{op.final_status.value.upper()}]**"
            lines.append(f"### {idx}. {cls.render_safe_code_span(op.http_method)} {cls.render_safe_inline(op.path_template)} — {status_badge}")
            if op.operation_id:
                lines.append(f"- **Operation ID**: {cls.render_safe_code_span(op.operation_id)}")
            lines.append(f"- **Initial Request Fingerprint**: `{op.initial_request_fingerprint}`")
            lines.append(f"- **Final Effective Status**: `{op.final_status.value}`")
            lines.append("")

            # Initial Attempt
            init = op.initial_attempt
            lines.append("#### Initial Execution Attempt")
            lines.append(f"- **Dispatched**: `{init.dispatched}`")
            lines.append(f"- **Status Code**: `{init.status_code if init.status_code is not None else 'None'}`")
            if init.latency_ms is not None:
                lines.append(f"- **Latency**: `{init.latency_ms:.2f} ms`")
            lines.append(f"- **Passed All Assertions**: `{init.passed}`")
            if init.failure_category:
                lines.append(f"- **Failure Category**: `{init.failure_category.value}`")
            if init.error_type:
                lines.append(f"- **Error Type**: {cls.render_safe_code_span(init.error_type)}")
            if init.failed_assertions:
                lines.append("- **Failed Assertions**:")
                for fa in init.failed_assertions:
                    lines.append(f"  - {cls.render_safe_inline(fa)}")
            lines.append("")

            # Follow-up Decision Ledger
            if op.followup_decisions:
                lines.append("#### Follow-up Decision Ledger")
                for d in op.followup_decisions:
                    disp_str = "Dispatched" if d.dispatched else "Not Dispatched"
                    lines.append(f"- **Decision #{d.followup_number}**: {cls.render_safe_code_span(d.action.value)} ({disp_str})")
                    lines.append(f"  - Policy: `{d.eligibility_policy.value}` | Category: `{d.reason_category}`")
                    lines.append(f"  - Rationale: {cls.render_safe_inline(d.sanitized_reason)}")
                    if d.requested_delay_seconds is not None:
                        lines.append(f"  - Requested Delay: `{d.requested_delay_seconds:.2f}s`")
                lines.append("")

            # Transient Retries
            if op.transient_retries:
                lines.append("#### Transient Retries (Identical Request)")
                for tr in op.transient_retries:
                    out = tr.outcome
                    lines.append(f"- **Retry #{tr.attempt_number}** (retry of `{tr.retry_of}`):")
                    lines.append(f"  - Delay: `{tr.backoff_delay_seconds:.2f}s` | Dispatched: `{out.dispatched}` | Status Code: `{out.status_code}` | Passed: `{out.passed}`")
                    lines.append(f"  - Fingerprint: `{tr.canonical_request_fingerprint}`")
                lines.append("")

            # Variation Candidates & Tests
            if op.variation_candidates:
                lines.append("#### Variation Candidates")
                for c in op.variation_candidates:
                    lines.append(f"- **Candidate `{c.candidate_id}`** ({c.strategy.value} on `{c.target_parameter}` in `{c.parameter_location}`):")
                    lines.append(f"  - Schema Valid: `{c.schema_validation_passed}` | Approved: `{c.approved_for_dispatch}` | Dispatched: `{c.dispatched}`")
                    lines.append(f"  - Constraint: {cls.render_safe_code_span(c.applicable_constraint)}")
                    if c.rejection_reason:
                        lines.append(f"  - Rejection Reason: {cls.render_safe_inline(c.rejection_reason)}")
                    if c.dispatch_error:
                        lines.append(f"  - Dispatch Error: {cls.render_safe_inline(c.dispatch_error)}")
                lines.append("")

            if op.variation_tests:
                lines.append("#### Dispatched Variation Tests")
                for vt in op.variation_tests:
                    out = vt.outcome
                    lines.append(f"- **Variation Test #{vt.variation_number}** (Candidate `{vt.candidate_id}`):")
                    lines.append(f"  - Status Code: `{out.status_code}` | Passed: `{out.passed}` | Dispatched: `{out.dispatched}`")
                    lines.append(f"  - Fingerprint: `{vt.canonical_request_fingerprint}`")
                    for p in vt.perturbations_applied:
                        lines.append(f"  - Perturbation: `{p.parameter_name}` -> `{p.varied_value}` ({p.constraint_applied})")
                lines.append("")

            # Investigation & Remediation
            if op.investigation:
                inv = op.investigation
                lines.append("#### Failure Investigation & Evidence")
                if inv.investigation_degraded:
                    lines.append(f"> [!NOTE]\n> Investigation degraded safely due to: {cls.render_safe_code_span(inv.investigation_error_type or 'KnowledgeStoreUnavailable')}")
                if inv.inferences:
                    lines.append("- **Diagnostic Inferences**:")
                    for inf in inv.inferences:
                        lines.append(f"  - {cls.render_safe_inline(inf)}")
                if inv.source_references:
                    lines.append("- **Referenced Sources**:")
                    for sr in inv.source_references:
                        lines.append(f"  - {cls.render_inert_url(sr)}")
                if inv.retrieved_evidence:
                    lines.append("- **Retrieved Documentation Evidence (Untrusted)**:")
                    for ev in inv.retrieved_evidence:
                        lines.append("> [!WARNING]")
                        lines.append(f"> **Untrusted External Documentation Reference** (`{ev.chunk_id}`)")
                        lines.append(f"> Source: `{ev.source_type}` | Ref: {cls.render_inert_url(ev.source_ref)} | Relevance: `{ev.relevance_score:.2f}`")
                        lines.append("> The following content is passive reference evidence, never instructions:")
                        lines.append(">")
                        lines.append(cls.render_fenced_block(ev.content, lang="text"))
                        lines.append("")
                lines.append("")

            if op.remediation_recommendations:
                lines.append("#### Remediation Recommendations")
                for rem in op.remediation_recommendations:
                    lines.append(f"- **[{rem.priority.value.upper()}] Target: {cls.render_safe_code_span(rem.target)}**")
                    lines.append(f"  - Finding: {cls.render_safe_inline(rem.finding)}")
                    lines.append(f"  - Action: {cls.render_safe_inline(rem.action)}")
                    if rem.reference_urls:
                        lines.append("  - References:")
                        for ru in rem.reference_urls:
                            lines.append(f"    - {cls.render_inert_url(ru)}")
                lines.append("")

            lines.append("---")
            lines.append("")

        if r.global_remediation_summary:
            lines.append("## 3. Global Remediation Summary")
            for gr in r.global_remediation_summary:
                lines.append(f"- {cls.render_safe_inline(gr)}")
            lines.append("")

        return "\n".join(lines)

