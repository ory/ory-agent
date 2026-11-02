from __future__ import annotations

import argparse
import os
from uuid import uuid4

from agents import (
    Agent,
    ModelSettings,
    Runner,
    function_tool,
)
from ory_argus import OryAgentClient, with_hook_context
from ory_openai_agents import ory_run_hooks, ory_tool_input_guardrail

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
    if os.environ["ORY_AGENT_TYPE"] != "openai-agents":
        raise RuntimeError("ORY_AGENT_TYPE must be openai-agents")
    if os.environ["ORY_AGENT_CREDENTIAL_METHOD"] != "talos_api_key":
        raise RuntimeError("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key")


require_deployment_environment()
ory_client = OryAgentClient.from_env("openai-agents")


@function_tool(
    name_override="Greet",
    tool_input_guardrails=[ory_tool_input_guardrail(client=ory_client)],
)
def greet(name: str) -> str:
    """Return a greeting for the supplied name."""
    return f"Hello, {name}!"


agent = Agent(
    name="Ory Agent Security Example",
    instructions=(
        "Call Greet exactly once with name='world'. After the tool returns, "
        "give the user a concise final response containing its result."
    ),
    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    model_settings=ModelSettings(
        tool_choice="Greet",
        parallel_tool_calls=False,
    ),
    tools=[greet],
)


def run(prompt: str = DEFAULT_PROMPT, *, session_id: str | None = None) -> str:
    """Run one complete guarded Agent turn and return its final output."""
    run_session_id = session_id or os.getenv("ORY_AGENT_SESSION_ID") or uuid4().hex
    activity_offset = len(ory_client.logger.activities())
    with with_hook_context(ory_client, session_id=run_session_id):
        status = "error"
        try:
            result = Runner.run_sync(
                agent,
                prompt,
                hooks=ory_run_hooks(client=ory_client),
            )
            status = "ok"
        finally:
            ory_client.logger.activity(
                "session.end",
                status,
                attributes={"harness": "openai-agents"},
            )
            flushed = ory_client.flush_events(timeout=3)

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
        if "tls_error" in errors:
            raise RuntimeError(
                "Agent Security TLS verification failed. For Local Lite, set SSL_CERT_FILE "
                "to the combined public and active Caddy CA bundle described in README.md."
            )
        if "network_error" in errors:
            raise RuntimeError(
                "Agent Security could not be reached. Verify ORY_AGENT_SECURITY_URL and "
                "the deployment network path."
            )
        raise RuntimeError("Agent Security activity flush did not complete")
    return str(result.final_output)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Ory Agent Security OpenAI Agents hello-world example.",
    )
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--session-id",
        default=os.getenv("ORY_AGENT_SESSION_ID"),
        help="Optional stable session ID; defaults to a new ID for each run.",
    )
    args = parser.parse_args()
    print(run(args.prompt, session_id=args.session_id))


if __name__ == "__main__":
    main()
