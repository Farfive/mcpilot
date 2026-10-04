"""Small deterministic bilingual baseline plus a constrained optional reranker."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable

from .errors import InvalidProposal, PolicyDenied
from .models import Integration, Plan, Requirement, Selection, TaskRequest, Unavailable
from .policy import Policy

Reranker = Callable[[Requirement, tuple[dict, ...]], Awaitable[str]]

SERVICE_ALIASES = {
    "github": ("github",),
    "notion": ("notion",),
    "google-drive": ("google drive", "gdrive", "dysk google"),
    "slack": ("slack",),
    "filesystem": ("filesystem", "local file", "lokaln", "plik lokal", "readme"),
}
DEFAULT_CAPABILITIES = {
    "github": "issues.read",
    "notion": "documents.search",
    "google-drive": "documents.search",
    "slack": "messages.search",
    "filesystem": "files.read",
}


def requirements(request: TaskRequest) -> tuple[Requirement, ...]:
    text = request.task.lower()
    services = tuple(dict.fromkeys((*request.services, *(
        service for service, aliases in SERVICE_ALIASES.items() if any(a in text for a in aliases)
    ))))
    # A structured account binding must not be dropped when the text omits a service name.
    services = tuple(dict.fromkeys((*services, *request.accounts)))
    if services:
        found = []
        for service in services:
            caps = request.capabilities or (DEFAULT_CAPABILITIES.get(service, "unknown"),)
            if service == "slack" and not request.capabilities:
                if re.search(r"\b(send|post|wyślij|wyslij|opublikuj)\b", text):
                    caps = ("messages.send",)
                elif re.search(r"\b(draft|prepare|przygotuj|szkic)\b", text):
                    caps = ("messages.draft",)
            found.extend(Requirement(capability=c, service=service,
                                     account=request.accounts.get(service, "default")) for c in caps)
        return tuple(found)
    caps = request.capabilities
    if not caps:
        if any(s in text for s in ("issue", "bug", "zgłosze")):
            caps = ("issues.read",)
        elif any(s in text for s in ("file", "plik")):
            caps = ("files.read",)
        elif any(s in text for s in ("message", "wiadomo", "chat")):
            caps = ("messages.search",)
        elif any(s in text for s in ("document", "dokument", "search", "znajd")):
            caps = ("documents.search",)
        else:
            caps = ("unknown",)
    return tuple(Requirement(capability=c) for c in caps)


def _unavailable(integration: Integration, requirement: Requirement, policy: Policy) -> str | None:
    if integration.support not in {"supported", "experimental"}:
        return "setup_required" if integration.auth.setup_required else "unsupported"
    try:
        policy.check(integration, requirement.capability, requirement.account)
    except PolicyDenied:
        return "policy_denied"
    if not any(r.capability == requirement.capability and r.effect in policy.effects
               for r in integration.tools.values()):
        return "no_tool_mapping"
    return None


def _expand(requirement: Requirement, integrations: tuple[Integration, ...], policy: Policy) -> tuple[Requirement, ...]:
    """A named service without a recognized capability gets its read-only, policy-allowed ones."""
    if requirement.capability != "unknown" or not requirement.service:
        return (requirement,)
    capabilities = sorted({
        rule.capability for integration in integrations if integration.service == requirement.service
        for rule in integration.tools.values()
        if rule.effect == "read" and rule.capability in policy.capabilities
    })
    return tuple(requirement.model_copy(update={"capability": c}) for c in capabilities) or (requirement,)


async def route(
    request: TaskRequest,
    integrations: Iterable[Integration],
    policy: Policy,
    *,
    connected: set[tuple[str, str]] | None = None,
    reranker: Reranker | None = None,
) -> Plan:
    available = tuple(integrations)
    selected, missing, unavailable = [], [], []
    expanded = (e for r in requirements(request) for e in _expand(r, available, policy))
    for requirement in dict.fromkeys(expanded):
        candidates, rejected = [], []
        for integration in available:
            if requirement.service and requirement.service != integration.service:
                continue
            if not requirement.service and requirement.capability not in integration.capabilities:
                continue
            reason = _unavailable(integration, requirement, policy)
            if reason is not None:
                rejected.append(Unavailable(integration_id=integration.id,
                                            capability=requirement.capability, reason=reason))
                continue
            reused = (integration.id, requirement.account) in (connected or set())
            score = 100 + 20 * reused + 10 * integration.quality
            score -= min(integration.cost_per_call * 10, 20) + min(integration.latency_ms / 1000, 10)
            candidates.append((score, integration))
        candidates.sort(key=lambda pair: (-pair[0], pair[1].id))
        if not candidates:
            missing.append(requirement)
            unavailable.extend(sorted(rejected, key=lambda u: u.integration_id)[:5])
            continue
        score, chosen = candidates[0]
        if reranker:
            proposal = await reranker(requirement, tuple({
                "id": i.id, "service": i.service, "capabilities": i.capabilities,
                "score": s,
            } for s, i in candidates[:10]))
            matches = [(s, i) for s, i in candidates[:10] if i.id == proposal]
            if not matches:
                raise InvalidProposal("Reranker proposed an integration outside eligible candidates")
            score, chosen = matches[0]
        selected.append(Selection(integration_id=chosen.id, capability=requirement.capability,
                                  account=requirement.account, score=score,
                                  reason="Matches capability, service, account and host policy"))
    return Plan(task=request.task, selections=tuple(selected), missing=tuple(missing),
                unavailable=tuple(unavailable))
