"""Integration tests for ADK Graph Workflows and approval gate node execution."""
import unittest
from unittest.mock import MagicMock, patch

from workflows.base_graph import ADKGraphWorkflow
from workflows.nodes.approval_gate import UserApprovalGateNode


class TestGraphWorkflow(unittest.TestCase):
    def test_approval_gate_decision_gating(self):
        gate = UserApprovalGateNode()

        # High cost step requires approval
        self.assertTrue(gate.is_approval_required(step_name="newsletter_generator", model_name="gemini-2.5-flash", estimated_cost_usd=0.001))
        # High cost model requires approval
        self.assertTrue(gate.is_approval_required(step_name="screener", model_name="gemini-2.5-pro", estimated_cost_usd=0.001))
        # Low cost background task bypasses approval
        self.assertFalse(gate.is_approval_required(step_name="screener", model_name="gemini-2.5-flash", estimated_cost_usd=0.0001))

    @patch("workflows.nodes.approval_gate.UserApprovalGateNode.poll_approval_decision")
    @patch("workflows.nodes.approval_gate.UserApprovalGateNode.request_approval")
    @patch("workflows.nodes.llm_execution.LLMExecutionNode.execute_call")
    def test_full_adk_graph_workflow_approval_flow(self, mock_exec, mock_req_app, mock_poll):
        mock_req_app.return_value = {"approval_id": 42, "status": "PENDING"}
        mock_poll.return_value = {"status": "APPROVED", "model_override": "gemini-2.5-flash"}
        mock_exec.return_value = {
            "output_text": "Generated newsletter content",
            "actual_tokens": {"prompt_token_count": 500, "candidates_token_count": 200, "total_token_count": 700},
        }

        graph = ADKGraphWorkflow()
        result = graph.run_workflow(
            step_name="newsletter_generator",
            prompt_text="Generate newsletter draft.",
            target_model="gemini-2.5-flash",
            force_approval=True,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertIn("audit", result)
        self.assertEqual(result["output_text"], "Generated newsletter content")
        self.assertTrue(mock_req_app.called)
        self.assertTrue(mock_exec.called)


if __name__ == "__main__":
    unittest.main()
