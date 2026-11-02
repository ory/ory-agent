from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from e2b import Sandbox, Template, default_build_logger

from template import template

REQUIRED_ORY_ENV = {
    "ORY_PROJECT_URL",
    "ORY_AGENT_SECURITY_URL",
    "ORY_AGENT_TYPE",
    "ORY_AGENT_CREDENTIAL_METHOD",
    "ORY_AGENT_API_KEY",
}
COMPATIBILITY_ORY_ENV = {"ORY_AGENT_SUBJECT_ID", "ORY_AGENT_ROOT_DELEGATION"}
MAX_CREDENTIAL_BYTES = 1024 * 1024
PROMPT = (
    "Use the Bash tool exactly once to run `node /workspace/greet.mjs Ory`. "
    "After the command succeeds, answer with its exact output and do not use another tool."
)


def load_credentials(contents: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(contents.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid credential entry on line {line_number}")
        name, encoded_value = line.split("=", 1)
        if name not in REQUIRED_ORY_ENV | COMPATIBILITY_ORY_ENV:
            raise ValueError(f"unexpected credential key on line {line_number}: {name}")
        try:
            value = json.loads(encoded_value)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"invalid credential value on line {line_number}: {name}"
            ) from error
        if not isinstance(value, str) or not value or name in values:
            raise ValueError(f"invalid credential value on line {line_number}: {name}")
        values[name] = value

    missing = sorted(REQUIRED_ORY_ENV - values.keys())
    if missing:
        raise ValueError("deployment credentials are missing: " + ", ".join(missing))
    if values["ORY_AGENT_TYPE"] != "claude-code":
        raise ValueError("ORY_AGENT_TYPE must be claude-code")
    if values["ORY_AGENT_CREDENTIAL_METHOD"] != "talos_api_key":
        raise ValueError("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key")
    return {name: values[name] for name in REQUIRED_ORY_ENV}


def hostname(origin: str, name: str) -> str:
    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS origin")
    return parsed.hostname


def build_template(tag: str) -> dict[str, str]:
    build = Template.build(
        template,
        tag,
        cpu_count=2,
        memory_mb=2048,
        on_build_logs=default_build_logger(),
    )
    return {
        "template_name": build.name,
        "build_id": build.build_id,
        "template_ref": f"{build.name}:{build.build_id}",
    }


def load_template_ref(path: Path) -> str:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid E2B template artifact: {path}") from error
    if not isinstance(artifact, dict) or not isinstance(
        artifact.get("template_ref"), str
    ):
        raise ValueError(f"invalid E2B template artifact: {path}")
    template_ref = artifact["template_ref"].strip()
    if not template_ref:
        raise ValueError(f"invalid E2B template artifact: {path}")
    return template_ref


def read_credentials_stdin() -> str:
    if sys.stdin.isatty():
        raise ValueError("probe requires deployment credentials on stdin")
    contents = sys.stdin.read(MAX_CREDENTIAL_BYTES + 1)
    if len(contents.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise ValueError("deployment credentials exceed 1 MiB")
    return contents


def validate_running_sandbox(sandbox_id: str) -> None:
    try:
        result = subprocess.run(
            ["e2b", "sandbox", "info", "--format", "json", sandbox_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("E2B CLI is required to validate the sandbox") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("E2B CLI sandbox validation timed out") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "sandbox inspection failed"
        raise RuntimeError(f"E2B CLI sandbox validation failed: {detail}")


def validate_plugin_activity(sandbox: Sandbox) -> None:
    try:
        contents = sandbox.files.read(
            "/home/user/.config/ory-agent-plugins/claude-code/ory-agent-debug.log"
        )
        events = [json.loads(line) for line in contents.splitlines() if line.strip()]
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Ory Claude Code activity log is invalid") from error

    guarded = any(
        event.get("event") == "tool.invoke"
        and (event.get("attributes") or {}).get("toolName") == "Bash"
        and "node" in (event.get("attributes") or {}).get("shellWords", [])
        for event in events
    )
    if not guarded:
        raise RuntimeError("Ory did not record the guarded Bash and node tool checks")


def run_probe(template_ref: str, credentials: str) -> dict[str, object]:
    ory_env = load_credentials(credentials)
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required")

    sandbox = Sandbox.create(
        template=template_ref,
        timeout=300,
        metadata={"workload": "ory-claude-code-e2b"},
        network={
            "deny_out": lambda context: [context.all_traffic],
            "allow_out": [
                "api.anthropic.com",
                hostname(ory_env["ORY_AGENT_SECURITY_URL"], "ORY_AGENT_SECURITY_URL"),
                hostname(ory_env["ORY_PROJECT_URL"], "ORY_PROJECT_URL"),
            ],
        },
        lifecycle={"on_timeout": "kill"},
    )
    try:
        command = (
            "claude --dangerously-skip-permissions --output-format json -p "
            + shlex.quote(PROMPT)
        )
        result = sandbox.commands.run(
            command,
            cwd="/workspace",
            envs={**ory_env, "ANTHROPIC_API_KEY": anthropic_api_key},
            timeout=300,
        )
        if result.exit_code != 0:
            raise RuntimeError("E2B Claude Code probe failed")
        validate_plugin_activity(sandbox)
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Claude Code returned invalid JSON") from error
        proof = sandbox.commands.run(
            "cat /tmp/ory-claude-code-probe", cwd="/workspace", timeout=30
        )
        marker = proof.stdout.strip()
        if proof.exit_code != 0 or not re.fullmatch(r"[0-9a-f]{32}", marker):
            raise RuntimeError("Claude Code did not execute the guarded tool")
        if f"Hello, Ory! proof={marker}" not in str(response.get("result", "")):
            raise RuntimeError("Claude Code did not return the guarded tool result")
        validate_running_sandbox(sandbox.sandbox_id)
        return {
            "e2b_cli_validated": True,
            "sandbox_id": sandbox.sandbox_id,
            "template_ref": template_ref,
            "status": "succeeded",
        }
    finally:
        sandbox.kill()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and verify the Claude Code E2B deployment."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    build_parser = subcommands.add_parser("build")
    build_parser.add_argument("--tag", required=True)
    build_parser.add_argument("--output", type=Path)
    probe_parser = subcommands.add_parser("probe")
    probe_parser.add_argument("--template-file", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "build":
        output = build_template(args.tag)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(output, sort_keys=True) + "\n", encoding="utf-8"
            )
            return
    else:
        output = run_probe(
            load_template_ref(args.template_file), read_credentials_stdin()
        )
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
