import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import deploy


def credentials(**overrides: str) -> str:
    values = {
        "ORY_PROJECT_URL": "https://project.projects.oryapis.com",
        "ORY_AGENT_SECURITY_URL": "https://agents.console.ory.com",
        "ORY_AGENT_TYPE": "openai-agents",
        "ORY_AGENT_CREDENTIAL_METHOD": "talos_api_key",
        "ORY_AGENT_API_KEY": "talos-secret",
    }
    values.update(overrides)
    return "\n".join(f"{name}={json.dumps(value)}" for name, value in values.items())


class FakeRunner:
    """Records ssh invocations and answers like exe.dev."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[list[str], bytes | str | None]] = []
        self.fail_on = fail_on
        self.vm_name = ""

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        stdin = kwargs.get("input")
        self.calls.append((list(argv), stdin))
        remote = argv[argv.index("LogLevel=ERROR") + 1 :]
        host, command = remote[0], remote[1:]
        if host == "exe.dev" and command[0] == "new":
            self.vm_name = command[2].removeprefix("--name=")
        stdout = ""
        if host == "exe.dev" and command[:2] == ["ls", "--json"]:
            stdout = json.dumps(
                {"vms": [{"vm_name": self.vm_name, "status": "running"}]}
            )
        failed = self.fail_on is not None and self.fail_on in " ".join(command)
        if host != "exe.dev":
            return subprocess.CompletedProcess(argv, 1 if failed else 0, b"", b"boom")
        return subprocess.CompletedProcess(argv, 1 if failed else 0, stdout, "boom")

    def lobby_commands(self) -> list[list[str]]:
        result = []
        for argv, _ in self.calls:
            remote = argv[argv.index("LogLevel=ERROR") + 1 :]
            if remote[0] == "exe.dev":
                result.append(remote[1:])
        return result


def probe(runner: FakeRunner) -> dict[str, object]:
    exe = deploy.ExeDev(known_hosts=Path("/tmp/known_hosts"), run=runner)
    return deploy.run_probe(
        exe, b"bundle", "digest", credentials(), "sk-openai", sleep=lambda _: None
    )


class DeploymentControllerTest(unittest.TestCase):
    def test_accepts_deploy_issued_openai_agents_credentials(self) -> None:
        parsed = deploy.load_credentials(credentials())
        self.assertEqual(parsed["ORY_AGENT_TYPE"], "openai-agents")
        self.assertEqual(set(parsed), deploy.REQUIRED_ORY_ENV)

    def test_rejects_other_agent_types_and_non_origin_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "ORY_AGENT_TYPE must be openai-agents"):
            deploy.load_credentials(credentials(ORY_AGENT_TYPE="claude-code"))
        with self.assertRaisesRegex(ValueError, "must be an HTTPS origin"):
            deploy.load_credentials(
                credentials(ORY_AGENT_SECURITY_URL="https://agents.console.ory.com/x")
            )

    def test_shell_assignments_survive_posix_sourcing_verbatim(self) -> None:
        values = {
            "PLAIN": "abc",
            "QUOTES": 'it\'s "quoted"',
            "EXPANSION": "$HOME `id` $(id) \\n",
            "NEWLINE": "line1\nline2",
        }
        result = subprocess.run(
            ["sh", "-c", "set -a && . /dev/stdin && set +a && env -0"],
            input=deploy.shell_assignments(values).encode(),
            capture_output=True,
            check=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        environment = dict(
            entry.split("=", 1) for entry in result.stdout.decode().split("\0") if entry
        )
        for name, value in values.items():
            self.assertEqual(environment[name], value)

    def test_bundle_is_reproducible_and_credential_free(self) -> None:
        first, second = deploy.build_bundle(), deploy.build_bundle()
        self.assertEqual(
            hashlib.sha256(first).digest(), hashlib.sha256(second).digest()
        )
        with tarfile.open(fileobj=io.BytesIO(first)) as archive:
            self.assertEqual(
                sorted(archive.getnames()), ["agent.py", "requirements.txt"]
            )

    def test_bundle_digest_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = deploy.write_bundle(Path(directory) / "bundle.tar.gz")
            bundle, digest = deploy.load_bundle(Path(artifact["artifact"]))
            self.assertEqual(digest, artifact["sha256"])
            Path(artifact["path"]).write_bytes(bundle + b"tampered")
            with self.assertRaisesRegex(ValueError, "digest does not match"):
                deploy.load_bundle(Path(artifact["artifact"]))

    def test_probe_streams_credentials_only_over_stdin_and_deletes_the_vm(self) -> None:
        runner = FakeRunner()
        output = probe(runner)

        self.assertEqual(output["status"], "succeeded")
        self.assertTrue(output["vm_deleted"])
        name = str(output["vm_name"])
        self.assertTrue(name.startswith("ory-openai-hello-"))
        self.assertEqual(
            runner.lobby_commands(),
            [["new", "--sandbox", f"--name={name}"], ["ls", "--json"], ["rm", name]],
        )
        for argv, _ in runner.calls:
            joined = " ".join(argv)
            self.assertNotIn("talos-secret", joined)
            self.assertNotIn("sk-openai", joined)
            self.assertIn("BatchMode=yes", argv)
        agent_calls = [
            stdin for argv, stdin in runner.calls if argv[-1] == deploy.AGENT_COMMAND
        ]
        self.assertEqual(len(agent_calls), 1)
        self.assertIn(b"ORY_AGENT_API_KEY=talos-secret", agent_calls[0])
        self.assertIn(b"OPENAI_API_KEY=sk-openai", agent_calls[0])
        lobby_strictness = {
            argv[argv.index("LogLevel=ERROR") + 1]: argv[
                argv.index("LogLevel=ERROR") - 2
            ]
            for argv, _ in runner.calls
        }
        self.assertEqual(lobby_strictness["exe.dev"], "StrictHostKeyChecking=yes")

    def test_probe_deletes_the_vm_when_the_agent_fails(self) -> None:
        runner = FakeRunner(fail_on="agent.py")
        with self.assertRaisesRegex(RuntimeError, "Agent probe failed"):
            probe(runner)
        self.assertEqual(runner.lobby_commands()[-1][0], "rm")

    def test_rejects_an_unexpected_host_key(self) -> None:
        def runner(argv, **kwargs):
            if argv[0] == "ssh-keyscan":
                return subprocess.CompletedProcess(
                    argv, 0, "exe.dev ssh-ed25519 AAAA\n", ""
                )
            return subprocess.CompletedProcess(
                argv, 0, "256 SHA256:wrong exe.dev (ED25519)\n", ""
            )

        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known_hosts"
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                deploy.pin_lobby_host_key(known_hosts, run=runner)
            self.assertFalse(known_hosts.exists())


if __name__ == "__main__":
    unittest.main()
