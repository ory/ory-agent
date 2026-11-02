import json
import unittest

from deploy import hostname, load_credentials, validate_plugin_activity
from template import ORY_CLAUDE_VERSION


def credentials(**overrides: str) -> str:
    values = {
        "ORY_PROJECT_URL": "https://project.projects.oryapis.com",
        "ORY_AGENT_SECURITY_URL": "https://agents.console.ory.com",
        "ORY_AGENT_TYPE": "claude-code",
        "ORY_AGENT_CREDENTIAL_METHOD": "talos_api_key",
        "ORY_AGENT_API_KEY": "secret",
    }
    values.update(overrides)
    return "\n".join(f"{name}={json.dumps(value)}" for name, value in values.items())


class DeploymentControllerTest(unittest.TestCase):
    def test_accepts_deploy_issued_claude_code_credentials(self) -> None:
        parsed = load_credentials(credentials())

        self.assertEqual(parsed["ORY_AGENT_TYPE"], "claude-code")
        self.assertEqual(parsed["ORY_AGENT_CREDENTIAL_METHOD"], "talos_api_key")

    def test_rejects_an_sdk_deployment_credential(self) -> None:
        with self.assertRaisesRegex(ValueError, "ORY_AGENT_TYPE must be claude-code"):
            load_credentials(credentials(ORY_AGENT_TYPE="claude-agent-sdk"))

    def test_rejects_non_origin_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an HTTPS origin"):
            hostname("https://agents.console.ory.com/path", "ORY_AGENT_SECURITY_URL")

    def test_pins_plugin_with_deployment_owner_support(self) -> None:
        self.assertEqual(ORY_CLAUDE_VERSION, "1.4.7")

    def test_requires_guarded_bash_and_shell_activity(self) -> None:
        event = {
            "event": "tool.invoke",
            "attributes": {"toolName": "Bash", "shellWords": ["node"]},
        }
        sandbox = type(
            "Sandbox",
            (),
            {
                "files": type(
                    "Files", (), {"read": lambda self, path: json.dumps(event)}
                )()
            },
        )()

        validate_plugin_activity(sandbox)


if __name__ == "__main__":
    unittest.main()
