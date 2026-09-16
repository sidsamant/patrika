"""Human-in-the-Loop user approval gate node for ADK Graph Workflows."""
from __future__ import annotations

import logging
import time
from typing import Any

from agents import pipeline_client

logger = logging.getLogger(__name__)

# Steps / agents that require mandatory user approval
HIGH_COST_STEPS = {"newsletter_generator", "sectionizer", "batch_sectionizer", "editorial_rewrite"}
HIGH_COST_MODELS = {"gemini-2.5-pro", "gemini-1.5-pro"}


class UserApprovalGateNode:
    """Graph node that enforces user approval gating before making high-cost Google LLM API calls."""

    def is_approval_required(
        self,
        *,
        step_name: str,
        model_name: str,
        estimated_cost_usd: float,
        cost_threshold_usd: float = 0.005,
    ) -> bool:
        """Determines if the LLM call requires pre-execution human approval."""
        normalized_step = (step_name or "").strip().lower()
        normalized_model = (model_name or "").strip().lower()

        if normalized_step in HIGH_COST_STEPS:
            return True
        if normalized_model in HIGH_COST_MODELS:
            return True
        if estimated_cost_usd >= cost_threshold_usd:
            return True
        return False

    def request_approval(
        self,
        *,
        workflow_id: str,
        step_name: str,
        target_model: str,
        estimation_result: dict[str, Any],
        prompt_summary: str,
    ) -> dict[str, Any]:
        """Creates approval request in Django DB and returns registered approval metadata."""
        cost_proof = estimation_result.get("cost_proof") or {}
        return pipeline_client.create_approval_request(
            workflow_id=workflow_id,
            step_name=step_name,
            target_model=target_model,
            input_tokens=estimation_result.get("input_tokens", 0),
            estimated_output_tokens=estimation_result.get("estimated_output_tokens", 0),
            estimated_cost_usd=cost_proof.get("total_cost_usd", 0.0),
            estimated_cost_inr=cost_proof.get("total_cost_inr", 0.0),
            prompt_summary=prompt_summary,
            token_breakdown=estimation_result.get("token_breakdown") or {},
        )

    def poll_approval_decision(
        self,
        workflow_id: str,
        *,
        poll_interval_seconds: float = 2.0,
        max_wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Polls Django API indefinitely (or up to max_wait) until user approves or rejects."""
        start_time = time.time()
        logger.info(f"Workflow [{workflow_id}] entering WAITING_FOR_USER_APPROVAL state.")

        while True:
            try:
                res = pipeline_client.get_approval_status(workflow_id)
                status = (res.get("status") or "PENDING").upper()
                if status in ("APPROVED", "REJECTED", "CANCELLED"):
                    logger.info(f"Workflow [{workflow_id}] received approval decision: {status}")
                    return res
            except Exception as exc:
                logger.warning(f"Error checking approval status for [{workflow_id}]: {exc}")

            if max_wait_seconds and (time.time() - start_time) > max_wait_seconds:
                return {"status": "TIMEOUT", "workflow_id": workflow_id}

            time.sleep(poll_interval_seconds)
