"""Offline channel-to-tool test using the real Agents SDK, Agent Framework, and Ory adapter."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

TEST_ENV = {
    "ORY_PROJECT_URL": "https://project.projects.oryapis.com",
    "ORY_AGENT_SECURITY_URL": "https://agents.console.ory.com",
    "ORY_AGENT_TYPE": "agent-framework",
    "ORY_AGENT_CREDENTIAL_METHOD": "talos_api_key",
    "ORY_AGENT_API_KEY": "test-runtime-key",
    "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.services.ai.azure.com/api/projects/agent",
    "FOUNDRY_MODEL": "test-model",
    # Anonymous channel requests are enabled only for this in-process test.
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "true",
    "XDG_CONFIG_HOME": tempfile.mkdtemp(prefix="ory-agent-test-"),
}

with mock.patch.dict(os.environ, TEST_ENV):
    from agent_framework import (
        BaseChatClient,
        ChatResponse,
        Content,
        FunctionInvocationLayer,
        Message,
    )
    from agent_framework.foundry import FoundryChatClient
    from azure.identity.aio import DefaultAzureCredential
    from ory_agent_framework import middleware as ory_middleware
    from ory_argus.adapters import GateResult
    from ory_argus.permissions import PermissionDecision

    # The middleware binds its session starter on import; keep it offline.
    with mock.patch.object(
        ory_middleware, "create_session_starter", lambda *a, **k: lambda: None
    ):
        import agent
        import app


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """Requests Greet once, then answers with the tool result the framework returned."""

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        results = [
            content
            for message in messages
            for content in message.contents
            if content.type == "function_result"
        ]
        if not results:
            call = Content.from_function_call(
                "call-1", "Greet", arguments={"name": "world"}
            )
            return ChatResponse(messages=[Message(role="assistant", contents=[call])])
        reply = f"The tool returned: {results[-1].result}"
        return ChatResponse(messages=[Message(role="assistant", contents=[reply])])


def decision(*, blocked: bool) -> GateResult:
    return GateResult(
        proceed=not blocked,
        blocked=blocked,
        decision=PermissionDecision(kind="deny" if blocked else "allow"),
        subject="Agent:test",
        namespace="AgentTool",
        denial_message="Ory: permission denied" if blocked else None,
    )


def message_activity(text: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": "activity-1",
        "channelId": "test",
        "serviceUrl": "https://channel.invalid",
        "deliveryMode": "expectReplies",
        "conversation": {"id": "conversation-1"},
        "from": {"id": "user-1"},
        "recipient": {"id": "agent-1"},
        "text": text,
    }


class ChannelToToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls: list[str] = []
        self.tool_names: list[str] = []
        original_greet = agent.greet.func

        def recording_greet(name: str) -> str:
            self.calls.append(name)
            return original_greet(name)

        def recording_gate(_client, **kwargs) -> GateResult:
            self.tool_names.append(kwargs["tool_name"])
            return decision(blocked=self.blocked)

        self.blocked = False
        patches = [
            mock.patch.object(agent.agent, "client", ScriptedChatClient()),
            mock.patch.object(agent.greet, "func", recording_greet),
            mock.patch.object(ory_middleware, "gate", recording_gate),
            mock.patch.object(agent.ory_client, "flush_events", lambda **_: True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.client = TestClient(TestServer(app.create_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def send(self, text: str) -> list[str]:
        response = await self.client.post("/api/messages", json=message_activity(text))
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        return [activity.get("text", "") for activity in body["activities"]]

    async def test_allowed_tool_runs_and_its_result_reaches_the_channel(self) -> None:
        replies = await self.send(agent.DEFAULT_PROMPT)

        self.assertEqual(self.tool_names, ["Greet"])
        self.assertEqual(self.calls, ["world"])
        self.assertEqual(replies, ["The tool returned: Hello, world!"])

    async def test_denied_tool_never_runs(self) -> None:
        self.blocked = True

        replies = await self.send(agent.DEFAULT_PROMPT)

        self.assertEqual(self.tool_names, ["Greet"])
        self.assertEqual(self.calls, [])
        self.assertEqual(replies, ["The tool returned: Ory: permission denied"])

    async def test_channel_rejects_unauthenticated_requests_by_default(self) -> None:
        config = app.connection_manager.get_default_connection_configuration()
        with mock.patch.object(config, "ANONYMOUS_ALLOWED", False):
            response = await self.client.post(
                "/api/messages", json=message_activity("hello")
            )
        self.assertEqual(response.status, 401)

    async def test_health_endpoint_is_unauthenticated(self) -> None:
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)


class ModelClientTest(unittest.TestCase):
    def test_uses_the_configured_foundry_deployment_without_a_key(self) -> None:
        client = agent.agent.client

        self.assertIsInstance(client, FoundryChatClient)
        self.assertEqual(client.model, TEST_ENV["FOUNDRY_MODEL"])
        self.assertEqual(
            str(client.client.base_url),
            TEST_ENV["FOUNDRY_PROJECT_ENDPOINT"] + "/openai/v1/",
        )
        self.assertIsInstance(
            client.project_client._config.credential, DefaultAzureCredential
        )
        self.assertFalse(client.client.api_key)


class DeploymentEnvironmentTest(unittest.TestCase):
    def test_rejects_a_credential_for_another_agent_type(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {**TEST_ENV, "ORY_AGENT_TYPE": "openai-agents"}
            ),
            self.assertRaisesRegex(RuntimeError, "must be agent-framework"),
        ):
            agent.require_deployment_environment()

    def test_rejects_an_incomplete_deployment_package(self) -> None:
        with mock.patch.dict(os.environ, TEST_ENV):
            del os.environ["ORY_AGENT_API_KEY"]
            with self.assertRaisesRegex(RuntimeError, "ORY_AGENT_API_KEY"):
                agent.require_deployment_environment()


if __name__ == "__main__":
    unittest.main()
