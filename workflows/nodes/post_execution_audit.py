"""Post-execution token and cost audit node for Google Gemini LLM API."""
from __future__ import annotations

import logging
from typing import Any

from agents import pipeline_client
from workflows.pricing_matrix import calculate_cost

logger = logging.getLogger(__name__)


class PostExecutionAuditNode:
    """Graph node that audits actual Google LLM API token consumption and persists reconciled cost proof."""

    def audit_and_reconcile(
        self,
        *,
        approval_id: int,
        model_name: str,
        execution_result: dict[str, Any],
        estimation_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Calculates actual cost from Google LLM response usage_metadata and updates approval request in DB."""
        actual_tokens = execution_result.get("actual_tokens") or {}
        prompt_tokens = actual_tokens.get("prompt_token_count", 0)
        candidates_tokens = actual_tokens.get("candidates_token_count", 0)
        total_tokens = actual_tokens.get("total_token_count", prompt_tokens + candidates_tokens)

        # Fallback if usage_metadata tokens are 0
        if prompt_tokens == 0:
            prompt_tokens = estimation_result.get("input_tokens", 0)
        if candidates_tokens == 0:
            candidates_tokens = len(execution_result.get("output_text", "")) // 4

        actual_cost_proof = calculate_cost(
            model_name=model_name,
            input_tokens=prompt_tokens,
            output_tokens=candidates_tokens,
        )

        actual_usd = actual_cost_proof["total_cost_usd"]
        actual_inr = actual_cost_proof["total_cost_inr"]

        # Persist to Django DB via pipeline_client
        if approval_id:
            try:
                pipeline_client.update_approval_actuals(
                    approval_id,
                    actual_prompt_tokens=prompt_tokens,
                    actual_completion_tokens=candidates_tokens,
                    actual_total_tokens=total_tokens,
                    actual_cost_usd=actual_usd,
                    actual_cost_inr=actual_inr,
                )
            except Exception as exc:
                logger.warning(f"Error persisting post-call actual cost audit for approval [{approval_id}]: {exc}")

        est_usd = estimation_result.get("estimated_cost_usd", 0.0)
        cost_variance_usd = actual_usd - est_usd

        return {
            "approval_id": approval_id,
            "model_name": model_name,
            "actual_tokens": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": candidates_tokens,
                "total_tokens": total_tokens,
            },
            "actual_cost_usd": actual_usd,
            "actual_cost_inr": actual_inr,
            "estimated_cost_usd": est_usd,
            "cost_variance_usd": round(cost_variance_usd, 6),
            "reconciliation_summary": (
                f"Actual call completed with {total_tokens} tokens (${actual_usd:.6f}). "
                f"Variance vs estimate: ${cost_variance_usd:+.6f}"
            ),
        }
