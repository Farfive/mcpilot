"""MCPilot: importing never installs packages, opens sockets or launches processes."""

from .models import AuthSpec, Integration, Limits, PackageSpec, TaskRequest, ToolRule

__all__ = ["AuthSpec", "Integration", "Limits", "MCPilot", "PackageSpec", "Policy", "TaskRequest", "ToolRule"]
__version__ = "0.1.0"


def __getattr__(name: str):
    if name == "MCPilot":
        from .sdk import MCPilot

        return MCPilot
    if name == "Policy":
        from .policy import Policy

        return Policy
    raise AttributeError(name)
