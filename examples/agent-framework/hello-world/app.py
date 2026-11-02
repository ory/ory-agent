from __future__ import annotations

import logging
import os

from aiohttp import web
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_decorator,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnContext,
    TurnState,
)

import agent

logger = logging.getLogger(__name__)

# The Microsoft 365 Agents SDK is only the channel layer. Orchestration and every protected
# tool call stay in agent.py, behind the Ory function middleware.
agents_sdk_config = load_configuration_from_env(os.environ)
storage = MemoryStorage()
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)
authorization = Authorization(storage, connection_manager, **agents_sdk_config)
agent_app = AgentApplication[TurnState](
    storage=storage,
    adapter=adapter,
    authorization=authorization,
    **agents_sdk_config,
)


@agent_app.activity("message")
async def on_message(context: TurnContext, _state: TurnState) -> None:
    prompt = (context.activity.text or "").strip() or agent.DEFAULT_PROMPT
    await context.send_activity(await agent.run(prompt))


@agent_app.error
async def on_error(context: TurnContext, error: Exception) -> None:
    # Keep provider, credential, and policy details in server logs, not in the channel.
    logger.error("Agent turn failed: %s", type(error).__name__, exc_info=error)
    await context.send_activity("The agent could not complete this request.")


@jwt_authorization_decorator
async def messages(request: web.Request) -> web.Response:
    return await start_agent_process(request, agent_app, adapter)


async def healthz(_request: web.Request) -> web.Response:
    # Azure Container Apps sets CONTAINER_APP_REVISION; the deploy controller waits for it.
    revision = os.getenv("CONTAINER_APP_REVISION", "")
    return web.json_response({"status": "ok", "revision": revision})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    # The Agents SDK JWT decorator reads this exact string key.
    app["agent_configuration"] = (
        connection_manager.get_default_connection_configuration()
    )
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "3978")))
