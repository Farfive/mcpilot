/** Sanitized error types: raw transport or provider messages never enter an LLM context. */

export class MCPilotError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

export class PolicyDenied extends MCPilotError {}
export class InvalidArguments extends MCPilotError {}
export class BudgetExceeded extends MCPilotError {}
export class ConnectionUnavailable extends MCPilotError {}
/** A mutation may have succeeded. Reconcile with the provider before retrying. */
export class UncertainOutcome extends MCPilotError {}
export class InvalidProposal extends MCPilotError {}

export type AuthStatus = "auth_required" | "setup_required" | "error";

/** A deliberately credential-free authentication failure. */
export class AuthFailure extends MCPilotError {
  readonly status: AuthStatus = "error";
}

export class AuthRequired extends AuthFailure {
  override readonly status: AuthStatus = "auth_required";
}

export class AuthSetupRequired extends AuthFailure {
  override readonly status: AuthStatus = "setup_required";
}

/** Sanitized connection or transport failure. */
export class RuntimeFailure extends MCPilotError {}

/** An operation timed out; a tool may already have executed, so it is never replayed. */
export class RuntimeTimeout extends RuntimeFailure {}
