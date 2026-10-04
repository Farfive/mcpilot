"""Sanitized error types: raw transport exceptions must not enter an LLM context."""


class MCPilotError(Exception):
    pass


class PolicyDenied(MCPilotError):
    pass


class InvalidArguments(MCPilotError):
    pass


class BudgetExceeded(MCPilotError):
    pass


class ConnectionUnavailable(MCPilotError):
    pass


class UncertainOutcome(MCPilotError):
    """A mutation may have succeeded. Reconcile with the provider before retrying."""


class InvalidProposal(MCPilotError):
    pass
