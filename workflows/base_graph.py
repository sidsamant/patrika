"""ADK Graph Workflow implementation combining token estimation, approval gating, and execution."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from workflows.nodes.approval_gate import UserApprovalGateNode
from workflows.nodes.llm_execution import LLMExecutionNode
from workflows.nodes.post_execution_audit import PostExecutionAuditNode
from workflows.nodes.token_cost_estimator import PreCallTokenCostEstimatorNode

logger = logging.getLogger(__name__)


class ADKGraphWorkflow:
    """Google ADK Graph Workflow state graph executor for Google Gemini LLM pipeline operations."""

    def __init__(
        self,
        *,
        estimator_node: PreCallTokenCostEstimatorNode | None = None,
        approval_node: UserApprovalGateNode | None = None,
        execution_node: LLMExecutionNode | None = None,
        audit_node: PostExecutionAuditNode | None = None,
    ):
        self.estimator_node = estimator_node or PreCallTokenCostEstimatorNode()
        self.approval_node = approval_node or UserApprovalGateNode()
        self.execution_node = execution_node or LLMExecutionNode()
        self.audit_node = audit_node or PostExecutionAuditNode()

    def run_workflow(
        self,
        *,
        step_name: str,
        prompt_text: str,
        system_instruction: str | None = None,
        context_text: str | None = None,
        target_model: str = "gemini-2.5-flash",
        max_output_tokens: int = 2048,
        workflow_id: str | None = None,
        force_approval: bool = False,
    ) -> dict[str, Any]:
        """Runs graph workflow DAG: Estimate -> Approval Gate (if needed) -> LLM Call -> Post Audit."""
        w_id = workflow_id or f"wf-{uuid.uuid4().hex[:12]}"
        logger.info(f"Starting ADK Graph Workflow [{w_id}] for step [{step_name}] with model [{target_model}]...")

        # Step 1: Pre-Execution Token Counting & Cost Estimation
        estimation_res = self.estimator_node.estimate_prompt_cost(
            model_name=target_model,
            prompt_text=prompt_text,
            system_instruction=system_instruction,
            context_text=context_text,
            max_output_tokens=max_output_tokens,
        )

        prompt_summary = (
            f"Step: {step_name} | Model: {target_model} | "
            f"Prompt: {prompt_text[:120]}..."
        )

        # Step 2: Human-in-the-Loop Approval Gate Node
        approval_required = force_approval or self.approval_node.is_approval_required(
            step_name=step_name,
            model_name=target_model,
            estimated_cost_usd=estimation_res["estimated_cost_usd"],
        )

        approval_record: dict[str, Any] = {}
        approval_id: int | None = None

        if approval_required:
            logger.info(f"Workflow [{w_id}] requires pre-call human approval.")
            approval_record = self.approval_node.request_approval(
                workflow_id=w_id,
                step_name=step_name,
                target_model=target_model,
                estimation_result=estimation_res,
                prompt_summary=prompt_summary,
            )
            approval_id = approval_record.get("approval_id")

            # Poll/Wait for user approval
            decision = self.approval_node.poll_approval_decision(w_id)
            if decision.get("status") != "APPROVED":
                logger.warning(f"Workflow [{w_id}] cancelled or rejected by user.")
                return {
                    "workflow_id": w_id,
                    "status": decision.get("status", "REJECTED"),
                    "estimation": estimation_res,
                    "approval_record": approval_record,
                    "output_text": "",
                    "message": "Workflow halted: LLM call was rejected by user.",
                }

            # Check if user overrode model tier during approval
            selected_model = decision.get("model_override") or target_model
        else:
            selected_model = target_model

        # Step 3: LLM Execution Node (Google Gemini API Call)
        exec_res = self.execution_node.execute_call(
            model_name=selected_model,
            prompt_text=prompt_text,
            system_instruction=system_instruction,
            context_text=context_text,
            max_output_tokens=max_output_tokens,
        )

        # Step 4: Post Execution Token & Cost Audit Node
        audit_res = self.audit_node.audit_and_reconcile(
            approval_id=approval_id,
            model_name=selected_model,
            execution_result=exec_res,
            estimation_result=estimation_res,
        )

        return {
            "workflow_id": w_id,
            "status": "COMPLETED",
            "estimation": estimation_res,
            "approval_record": approval_record,
            "execution": exec_res,
            "audit": audit_res,
            "output_text": exec_res.get("output_text", ""),
        }
