"""
Least-privilege gate for destructive infrastructure actions.

Mirrors the purple-team authorization pattern (sirius.purpleteam): before an agent
may run a destructive op (terraform apply/destroy, k8s mutation) against a
resource, two conditions must hold:

  1. Per-agent scope — the calling agent's config allow-list
     (infra.agent_projects) must include the resource (or "*").
  2. Human authorization — if infra.require_authorization is true, a human must
     have authorized the exact resource via POST /api/infra/authorize.

The resource is a terraform project name, or a k8s cluster name / the literal
"k8s". The LLM cannot self-authorize: `_AUTHORIZED_INFRA` is written only by the
HTTP endpoint. Refusals are returned as a dict (never raised) so the model sees a
normal tool result and reports back how to authorize.
"""

from __future__ import annotations

from typing import Optional

from sirius.config import load_config
from sirius.events import current_agent, current_session, emit

# Resources a human has authorized for destructive infra ops (in-memory,
# process-scoped — lost on restart, same as purple-team). The raw grant is
# ONE-SHOT: guard() consumes it. Apply then becomes sticky for the rest of the
# session (see _STICKY_APPLY); destroy always needs a fresh confirm.
_AUTHORIZED_INFRA: set[str] = set()

# Per-session pending confirmation: set when guard() refuses a scoped op only for
# lack of human auth, popped by the chat route so the UI can prompt in-chat.
_PENDING: dict[str, dict] = {}

# Apply authorization is sticky per (session, resource): once a human confirms an
# apply, later applies of the SAME project in the SAME session skip the prompt, so
# the agent can fix a failed apply and retry autonomously. Destroy is never sticky
# (each destroy re-confirms) and clears any sticky apply grant for the resource so
# a later rebuild is confirmed again. In-memory / process-scoped, like the rest.
_STICKY_APPLY: set[tuple[str, str]] = set()


def _normalize(resource: str) -> str:
    return (resource or "").strip().lower()


def authorize_infra(resource: str) -> None:
    """Record human authorization for a resource (called only by the HTTP endpoint)."""
    _AUTHORIZED_INFRA.add(_normalize(resource))


def is_infra_authorized(resource: str) -> bool:
    return _normalize(resource) in _AUTHORIZED_INFRA


def take_pending(session: str) -> Optional[dict]:
    """Pop the pending in-chat confirmation for a session, if any."""
    return _PENDING.pop(session or "", None)


def _allowed_projects(agent: str, cfg) -> list[str]:
    return cfg.infra.agent_projects.get(agent, ["*"])


def guard(resource: str, op: str, confirm: bool = True, sticky: bool = False) -> Optional[dict]:
    """Enforce scope (+ optional human confirmation) for an infra op.

    Returns a refusal dict (and emits a `blocked` event) if the current agent may
    not perform `op` on `resource`; returns None to allow.

    `confirm` controls the human-confirmation step: True for ops that change real
    infrastructure (terraform apply/destroy, k8s mutations) — these require the
    user to confirm in-chat by typing the resource name. False for local-only ops
    (authoring/removing project files) — scope is still enforced, but no typed
    confirmation is needed, so writing files and planning stay frictionless.

    `sticky` makes the confirmation last for the rest of the session, like a
    terraform apply: once the human confirms `resource`, later sticky ops on the
    SAME resource in the SAME session run without re-prompting. Used by the general
    `aws_cli` runner so a single "aws" confirm covers a batch of mutations (e.g.
    cleaning up several orphaned resources) instead of prompting on every call.
    """
    cfg = load_config()
    agent = current_agent.get() or "unknown"

    # 1. Per-agent project scope (least privilege) — always enforced.
    allowed = _allowed_projects(agent, cfg)
    if "*" not in allowed and _normalize(resource) not in [_normalize(a) for a in allowed]:
        reason = (f"Agent {agent!r} is not permitted to {op} {resource!r}. "
                  f"Allowed: {', '.join(allowed) or '(none)'}.")
        emit("blocked", f"{op} refused — {resource} out of scope for {agent}",
             severity="danger",
             payload={"resource": resource, "op": op, "agent": agent})
        return {"ok": False, "refused": True, "reason": reason}

    # Local-only op (file authoring/cleanup): scope passed, no confirmation needed.
    if not confirm:
        return None

    session = current_session.get() or ""
    op_n = (op or "").strip().lower()
    res_n = _normalize(resource)

    # 2. Human authorization for destructive ops — confirmed in-chat by typing the
    #    resource name.
    #
    # Apply is confirmed ONCE PER SESSION: after the first confirmation, later
    # applies of the same project in the same session are sticky (no re-prompt), so
    # the agent can fix a failed apply and retry on its own. Destroy always needs a
    # fresh confirm.
    if cfg.infra.require_authorization:
        is_sticky_op = op_n == "apply" or sticky
        already_sticky = is_sticky_op and (session, res_n) in _STICKY_APPLY
        if not already_sticky and not is_infra_authorized(resource):
            reason = (f"{op} of {resource!r} needs human confirmation. Summarize for the "
                      f"user, in plain language, exactly what this {op} will change, then "
                      f"ask them to type the exact resource name ({resource!r}) to confirm. "
                      f"Do not retry until they have confirmed.")
            _PENDING[session] = {"resource": resource, "op": op}
            emit("confirm_required", f"{op} of {resource} needs confirmation",
                 severity="warn",
                 payload={"resource": resource, "op": op, "agent": agent})
            return {"ok": False, "refused": True, "confirm_required": True, "reason": reason}
        if already_sticky:
            emit("skill_invoked",
                 f"Authorized {op}: {resource} (confirmed earlier this session)",
                 severity="warn",
                 payload={"resource": resource, "op": op, "agent": agent, "sticky": True})
            return None

    # Authorized (or authorization not required) — consume any one-shot grant.
    _AUTHORIZED_INFRA.discard(res_n)
    if cfg.infra.require_authorization:
        if op_n == "apply" or sticky:
            _STICKY_APPLY.add((session, res_n))      # sticky for the rest of the session
        elif op_n == "destroy":
            _STICKY_APPLY.discard((session, res_n))  # a later rebuild re-confirms
    emit("skill_invoked", f"Authorized {op}: {resource}", severity="warn",
         payload={"resource": resource, "op": op, "agent": agent})
    return None
