"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
import re
import secrets
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory
from src.graph.graph import AppierCampaignAgent, load_runtime_config

app = FastAPI(title="Agent")

# The runtime parameters reach the graph through its constructor: the framework
# reads max_retry from the graph config, and a config the graph was never given
# is a declaration nothing consumes.
agent = AppierCampaignAgent(config=load_runtime_config())
agent.compile()
# namespace/agent_name match the manifest's `namespace` / `name` values.
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="AppierCampaignAgent"))

# Adapter-level size cap on the caller-supplied context (bytes of its JSON
# serialization). Field-by-field validation happens in the pre_process node;
# this cap only stops oversized envelopes at the door.
_INPUT_CONTEXT_MAX_BYTES = 256 * 1024

# A caller field name is only repeated back when it is a plain token.
_PLAIN_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,32}$")


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Optional caller-supplied context: the campaign target
    # (campaign_id / campaign_hint / campaign_code), the `approved` flag, and
    # the structured `campaign` change block. Validated field-by-field by the
    # pre_process node — malformed values are refused without being echoed.
    input_context: "dict[str, Any]" = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> "dict[str, Any]":
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    if len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8")) > _INPUT_CONTEXT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="input_context too large.")

    # Standalone-deployment caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (the standalone equivalent
    # of the platform auth middleware) — a deployment-level caller credential,
    # not an agent secret, so ctx.secrets does not apply (no InvocationContext
    # exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    _refuse_credential_shaped_context(req.input_context)

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return cast(
            "dict[str, Any]",
            agent.invoke(req.input, ctx=ctx, input_context=req.input_context),
        )


def _refuse_credential_shaped_context(input_context: "dict[str, Any]") -> None:
    """Refuse a caller context carrying a credential-shaped value.

    The first backbone node copies the caller context into its own result, and
    the framework's output scan runs over every value of every node result — so
    a credential-shaped string anywhere in the context makes the FIRST node
    fail, with a stack trace, before any of this template's code runs. The
    request cannot succeed either way; refusing here turns an opaque internal
    failure into a 400 the caller can act on.

    The check calls the same detector the framework's gate calls, so the set
    refused here is exactly the set that would be blocked downstream — a
    repo-local approximation would drift from it. Only the field NAME is
    reported; the value and the detector's matched text never leave.
    """
    for index, (field, value) in enumerate(input_context.items()):
        if detect_credentials_in_value(value):
            # A field NAME is caller-controlled too: name it back only when it
            # is a plain token, otherwise report it by position.
            label = field if _PLAIN_FIELD_NAME.match(str(field)) else f"#{index + 1}"
            raise HTTPException(
                status_code=400,
                detail=(f"input_context field '{label}' looks like a credential and " "cannot be accepted."),
            )


@app.get("/health")
def health() -> "dict[str, str]":
    return {"status": "ok", "agent": "AppierCampaignAgent"}
