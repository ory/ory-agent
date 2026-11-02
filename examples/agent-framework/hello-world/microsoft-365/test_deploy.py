import io
import json
import struct
import sys
import types
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import deploy
from deploy import (
    PROBE_USER_ID,
    agent_reply,
    build_app_package,
    load_credentials,
    load_image,
    render_manifest,
)

BOT_APP_ID = "11111111-2222-3333-4444-555555555555"
ENDPOINT = "https://ory-agent.example.eastus.azurecontainerapps.io"
DIGEST = "sha256:" + "a" * 64


def credentials(**overrides: str) -> str:
    values = {
        "ORY_PROJECT_URL": "https://project.projects.oryapis.com",
        "ORY_AGENT_SECURITY_URL": "https://agents.console.ory.com",
        "ORY_AGENT_TYPE": "agent-framework",
        "ORY_AGENT_CREDENTIAL_METHOD": "talos_api_key",
        "ORY_AGENT_API_KEY": "secret",
    }
    values.update(overrides)
    return "\n".join(f"{name}={json.dumps(value)}" for name, value in values.items())


class CredentialTest(unittest.TestCase):
    def test_accepts_deploy_issued_agent_framework_credentials(self) -> None:
        parsed = load_credentials(credentials())

        self.assertEqual(parsed["ORY_AGENT_TYPE"], "agent-framework")
        self.assertEqual(parsed["ORY_AGENT_API_KEY"], "secret")

    def test_drops_compatibility_values(self) -> None:
        parsed = load_credentials(
            credentials() + '\nORY_AGENT_ROOT_DELEGATION="delegation"'
        )

        self.assertNotIn("ORY_AGENT_ROOT_DELEGATION", parsed)

    def test_rejects_a_credential_for_another_agent_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be agent-framework"):
            load_credentials(credentials(ORY_AGENT_TYPE="openai-agents"))

    def test_rejects_unexpected_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected credential key"):
            load_credentials(credentials() + '\nOPENAI_API_KEY="sk"')

    def test_rejects_non_origin_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an HTTPS origin"):
            load_credentials(
                credentials(ORY_AGENT_SECURITY_URL="https://agents.console.ory.com/x")
            )


class ImageTest(unittest.TestCase):
    def load(self, image: str) -> str:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "image.json"
            path.write_text(json.dumps({"image": image}), encoding="utf-8")
            return load_image(path)

    def test_accepts_a_digest_pinned_acr_image(self) -> None:
        image = f"oryexample.azurecr.io/ory-agent-framework-hello-world@{DIGEST}"

        self.assertEqual(self.load(image), image)

    def test_rejects_mutable_tags(self) -> None:
        with self.assertRaisesRegex(ValueError, "digest-pinned ACR image"):
            self.load("oryexample.azurecr.io/ory-agent-framework-hello-world:latest")


class TemplateDeploymentTest(unittest.TestCase):
    def test_sends_runtime_secrets_only_as_secure_template_parameters(self) -> None:
        requests = []

        class Poller:
            def result(self):
                outputs = {"endpoint": {"type": "String", "value": ENDPOINT}}
                return types.SimpleNamespace(
                    properties=types.SimpleNamespace(outputs=outputs)
                )

        class Client:
            def __init__(self, credential, subscription_id):
                self.deployments = self
                requests.append(("client", subscription_id))

            def begin_create_or_update(self, resource_group, name, body):
                requests.append((resource_group, name, body))
                return Poller()

        modules = {
            "azure": types.ModuleType("azure"),
            "azure.identity": types.SimpleNamespace(AzureCliCredential=object),
            "azure.mgmt": types.ModuleType("azure.mgmt"),
            "azure.mgmt.resource": types.ModuleType("azure.mgmt.resource"),
            "azure.mgmt.resource.deployments": types.SimpleNamespace(
                DeploymentsMgmtClient=Client
            ),
        }
        template = {"parameters": {"runtimeSecrets": {"type": "secureObject"}}}
        with (
            mock.patch.dict(sys.modules, modules),
            mock.patch.object(deploy, "az", return_value=json.dumps(template)),
        ):
            outputs = deploy.deploy_template(
                subscription_id="subscription",
                resource_group="group",
                parameters={"runtimeSecrets": {"ORY_AGENT_API_KEY": "secret"}},
            )

        self.assertEqual(outputs, {"endpoint": ENDPOINT})
        self.assertEqual(requests[0], ("client", "subscription"))
        group, name, body = requests[1]
        self.assertEqual((group, name), ("group", deploy.DEPLOYMENT_NAME))
        self.assertEqual(body["properties"]["template"], template)
        self.assertEqual(
            body["properties"]["parameters"],
            {"runtimeSecrets": {"value": {"ORY_AGENT_API_KEY": "secret"}}},
        )


class RunDeployTest(unittest.TestCase):
    def run_deploy(self, **overrides) -> dict[str, object]:
        sent: dict[str, object] = {}

        def deploy_template(**kwargs):
            sent.update(kwargs["parameters"])
            return {
                "endpoint": ENDPOINT,
                "containerAppName": "app",
                "botName": "bot",
                "botAppId": BOT_APP_ID,
            }

        values = {
            "resource_group": "group",
            "registry": "registry",
            "name_prefix": "oryhello",
            "image_file": Path("image.json"),
            "subscription": "subscription",
            "model": "gpt-5.4-mini",
            "model_version": "2026-03-17",
            "model_sku": "DataZoneStandard",
            "location": "eastus2",
        }
        values.update(overrides)
        with (
            mock.patch.object(deploy, "load_image", return_value="image"),
            mock.patch.object(deploy, "deploy_template", deploy_template),
            mock.patch.object(deploy, "wait_for_revision"),
            mock.patch.object(
                deploy, "probe_direct_line", return_value="Hello, world!"
            ),
        ):
            result = deploy.run_deploy(types.SimpleNamespace(**values), credentials())

        self.assertTrue(result["reply_verified"])
        return sent

    def test_sends_only_agent_security_values_as_secrets(self) -> None:
        sent = self.run_deploy()

        self.assertEqual(sent["runtimeSecrets"], load_credentials(credentials()))
        self.assertEqual(
            (sent["modelName"], sent["modelVersion"], sent["modelSku"]),
            ("gpt-5.4-mini", "2026-03-17", "DataZoneStandard"),
        )
        self.assertEqual(sent["location"], "eastus2")

    def test_omits_location_to_use_the_resource_group_region(self) -> None:
        sent = self.run_deploy(location=None)

        self.assertNotIn("location", sent)


class RevisionTest(unittest.TestCase):
    def test_waits_until_the_released_revision_serves_traffic(self) -> None:
        responses = iter(
            [
                {"status": "ok", "revision": "app--previous"},
                {"status": "ok", "revision": "app--r1"},
            ]
        )
        with (
            mock.patch.object(deploy, "request_json", lambda *a, **k: next(responses)),
            mock.patch.object(deploy.time, "sleep") as sleep,
        ):
            deploy.wait_for_revision(ENDPOINT, "app--r1")

        sleep.assert_called_once()


class DirectLineTest(unittest.TestCase):
    def test_ignores_the_probe_message_and_returns_the_agent_reply(self) -> None:
        activities = [
            {"type": "message", "from": {"id": PROBE_USER_ID}, "text": "Use Greet"},
            {"type": "typing", "from": {"id": "bot"}},
            {"type": "message", "from": {"id": "bot"}, "text": "Hello, world!"},
        ]

        self.assertEqual(agent_reply(activities), "Hello, world!")

    def test_waits_while_only_the_probe_message_is_visible(self) -> None:
        activities = [{"type": "message", "from": {"id": PROBE_USER_ID}, "text": "hi"}]

        self.assertIsNone(agent_reply(activities))


class AppPackageTest(unittest.TestCase):
    def test_manifest_binds_the_custom_engine_agent_to_the_bot(self) -> None:
        manifest = render_manifest(BOT_APP_ID, ENDPOINT)

        self.assertGreaterEqual(
            tuple(map(int, manifest["manifestVersion"].split("."))), (1, 21)
        )
        self.assertEqual(manifest["bots"][0]["botId"], BOT_APP_ID)
        self.assertIn("personal", manifest["bots"][0]["scopes"])
        self.assertEqual(
            manifest["copilotAgents"]["customEngineAgents"],
            [{"type": "bot", "id": BOT_APP_ID}],
        )
        self.assertEqual(
            manifest["validDomains"], ["ory-agent.example.eastus.azurecontainerapps.io"]
        )
        self.assertNotEqual(manifest["id"], BOT_APP_ID)
        self.assertEqual(manifest["id"], render_manifest(BOT_APP_ID, ENDPOINT)["id"])

    def test_package_contains_only_the_manifest_and_sized_icons(self) -> None:
        with zipfile.ZipFile(
            io.BytesIO(build_app_package(BOT_APP_ID, ENDPOINT))
        ) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["color.png", "manifest.json", "outline.png"],
            )
            sizes = {
                name: struct.unpack(">II", archive.read(name)[16:24])
                for name in ("color.png", "outline.png")
            }
            manifest = archive.read("manifest.json").decode("utf-8")

        self.assertEqual(sizes, {"color.png": (192, 192), "outline.png": (32, 32)})
        self.assertNotIn("ORY_", manifest)

    def test_rejects_an_invalid_bot_id(self) -> None:
        with self.assertRaises(ValueError):
            render_manifest("not-a-guid", ENDPOINT)


if __name__ == "__main__":
    unittest.main()
