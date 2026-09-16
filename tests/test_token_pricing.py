"""Unit tests for pre-call token counting and pricing matrix in newsletter_adk."""
import unittest
from workflows.pricing_matrix import calculate_cost, MODEL_PRICING
from workflows.nodes.token_cost_estimator import PreCallTokenCostEstimatorNode


class TestTokenPricing(unittest.TestCase):
    def test_pricing_matrix_formulas(self):
        # 1,000,000 input tokens and 1,000,000 output tokens for gemini-2.5-pro
        result = calculate_cost("gemini-2.5-pro", input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertEqual(result["input_cost_usd"], 1.25)
        self.assertEqual(result["output_cost_usd"], 5.00)
        self.assertEqual(result["total_cost_usd"], 6.25)

        # Flash model pricing
        flash_res = calculate_cost("gemini-2.5-flash", input_tokens=100_000, output_tokens=1_000)
        self.assertAlmostEqual(flash_res["input_cost_usd"], 0.0075, places=5)
        self.assertTrue(flash_res["total_cost_inr"] > 0)

    def test_token_cost_estimator_node(self):
        estimator = PreCallTokenCostEstimatorNode()
        res = estimator.estimate_prompt_cost(
            model_name="gemini-2.5-flash",
            prompt_text="Write a concise newsletter summary.",
            system_instruction="You are an expert editorial writer.",
            context_text="Sample document context text goes here.",
            max_output_tokens=1024,
        )

        self.assertIn("input_tokens", res)
        self.assertIn("estimated_cost_usd", res)
        self.assertIn("token_breakdown", res)
        self.assertEqual(res["estimated_output_tokens"], 1024)
        self.assertTrue(res["estimated_cost_usd"] > 0)


if __name__ == "__main__":
    unittest.main()
