import os

if os.getenv("SCREENER_BACKEND", "hosted").strip().lower() == "hosted":
    from .agent_hosted import file_metadata_screening_agent as root_agent
else:
    from .agent import file_metadata_screening_agent as root_agent

__all__ = ["root_agent"]
