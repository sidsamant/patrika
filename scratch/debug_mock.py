import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "agents"))

import agent
from unittest.mock import patch

print("Checking agents.screener.agent load function:", getattr(sys.modules["agents.screener.agent"], "load_hoarder_rows_for_screening"))
print("Checking agents.standardizer.agent pipeline_client:", getattr(sys.modules["agents.standardizer.agent"], "pipeline_client"))

with patch("agents.standardizer.agent.pipeline_client.load_screened_files_for_standardization", return_value=["test"]):
    import pipeline_client
    print("Inside patch, pipeline_client.load_screened_files_for_standardization():", pipeline_client.load_screened_files_for_standardization())
    print("Inside patch, agents.standardizer.agent.pipeline_client.load_screened_files_for_standardization():", sys.modules["agents.standardizer.agent"].pipeline_client.load_screened_files_for_standardization())
