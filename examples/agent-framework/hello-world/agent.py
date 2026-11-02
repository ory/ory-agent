from __future__ import annotations

import argparse
import asyncio
import os
from uuid import uuid4

from agent_framework import Agent, function_middleware, tool
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import DefaultAzureCredential
from ory_agent_framework import ory_function_middleware
from ory_argus import OryAgentClient, with_hook_context

DEFAULT_PROMPT = "Use the Greet tool, then tell me what the tool returned."

# OryAgentClient.from_env() is intentionally permissive for interactive development: an
# incomplete configuration can produce a disconnected, pass-through client or fall back to
# interactive authentication. A headless deployment must fail before either behavior is possible.
REQUIRED_DEPLOYMENT_ENV = (
    "ORY_PROJECT_URL",
    "ORY_AGENT_SECURITY_URL",
    "ORY_AGENT_TYPE",
    "ORY_AGENT_CREDENTIAL_METHOD",
    "ORY_AGENT_API_KEY",
)


def require_deployment_environment() -> None:
    """Temporarily enforce the complete headless deployment-package contract."""
    missing = [name for name in REQUIRED_DEPLOYMENT_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "missing Agent Security deployment environment: " + ", ".join(missing)
        )
    if os.environ["ORY_AGENT_TYPE"] != "agent-framework":
        raise RuntimeError("ORY_AGENT_TYPE must be agent-framework")
    if os.environ["ORY_AGENT_CREDENTIAL_METHOD"] != "talos_api_key":
        raise RuntimeError("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key")


require_deployment_environment()
ory_client = OryAgentClient.from_env("agent-framework")
_ory_middleware = ory_function_middleware(client=ory_client)


@function_middleware
async def ory_guard(context, call_next) -> None:
    """Gate every Agent Framework function call through Ory Agent Security."""

    # ory-agent-framework 1.4.8 calls call_next(context), while Agent Framework 1.x passes a
    # zero-argument continuation. Register ory_function_middleware() directly once the
    # published adapter supports the stable contract.
    async def legacy_next(_context):
        await call_next()
        return context.result

    await _ory_middleware(context, legacy_next)


@tool(name="Greet")
def greet(name: str) -> str:
    """Return a greeting for the supplied name."""
    return f"Hello, {name}!"


# Keyless model access: FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL select the deployment, and
# the Container App pins DefaultAzureCredential to its managed identity with
# AZURE_TOKEN_CREDENTIALS. Locally, the same code uses the developer's Azure CLI sign-in.
agent = Agent(
    client=FoundryChatClient(credential=DefaultAzureCredential()),
    name="Ory Agent Security Example",
    instructions=(
        "Call Greet exactly once with name='world'. After the tool returns, "
        "give the user a concise final response containing its result."
    ),
    tools=[greet],
    middleware=[ory_guard],
)


async def run(prompt: str = DEFAULT_PROMPT, *, session_id: str | None = None) -> str:
    """Run one complete guarded Agent turn and return its final output."""
    run_session_id = session_id or uuid4().hex
    activity_offset = len(ory_client.logger.activities())
    with with_hook_context(ory_client, session_id=run_session_id):
        status = "error"
        try:
            result = await agent.run(prompt)
            status = "ok"
        finally:
            ory_client.logger.activity(
                "session.end",
                status,
                attributes={"harness": "agent-framework"},
            )
            flushed = await asyncio.to_thread(ory_client.flush_events, timeout=3)

    if not flushed:
        errors = set()
        for activity in ory_client.logger.activities()[activity_offset:]:
            if activity.status != "error" or not activity.attributes:
                continue
            errors.update(
                code
                for code in (
                    activity.attributes.get("error"),
                    activity.attributes.get("reasonCode"),
                )
                if code
            )
        if "session_inactive" in errors:
            raise RuntimeError(
                "Agent Security rejected the deployment credential; it may be invalid, "
                "expired, or revoked. Create and inject a new deployment package."
            )
        if "network_error" in errors:
            raise RuntimeError(
                "Agent Security could not be reached. Verify ORY_AGENT_SECURITY_URL and "
                "the deployment network path."
            )
        raise RuntimeError("Agent Security activity flush did not complete")
    return result.text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one Ory Agent Security Microsoft Agent Framework turn locally.",
    )
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--session-id",
        help="Optional stable session ID; defaults to a new ID for each run.",
    )
    args = parser.parse_args()
    print(asyncio.run(run(args.prompt, session_id=args.session_id)))


if __name__ == "__main__":
    main()
