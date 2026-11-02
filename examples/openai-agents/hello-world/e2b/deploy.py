from __future__ import annotations

import argparse
import json
import os
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
AGENT_COMMAND = "python /workspace/agent.py"
MAX_CREDENTIAL_BYTES = 1024 * 1024


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
        if not isinstance(value, str):
            raise ValueError(f"invalid credential value on line {line_number}: {name}")
        if not value or name in values:
            raise ValueError(f"invalid credential value on line {line_number}: {name}")
        values[name] = value

    missing = sorted(REQUIRED_ORY_ENV - values.keys())
    if missing:
        raise ValueError("deployment credentials are missing: " + ", ".join(missing))
    if values["ORY_AGENT_TYPE"] != "openai-agents":
        raise ValueError("ORY_AGENT_TYPE must be openai-agents")
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
        cpu_count=1,
        memory_mb=1024,
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


def run_probe(template_ref: str, credentials: str) -> dict[str, object]:
    ory_env = load_credentials(credentials)
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is required")

    allow_out = [
        "api.openai.com",
        hostname(ory_env["ORY_AGENT_SECURITY_URL"], "ORY_AGENT_SECURITY_URL"),
        hostname(ory_env["ORY_PROJECT_URL"], "ORY_PROJECT_URL"),
    ]
    sandbox = Sandbox.create(
        template=template_ref,
        timeout=300,
        metadata={"workload": "ory-openai-agents-hello-world"},
        network={
            "deny_out": lambda context: [context.all_traffic],
            "allow_out": allow_out,
        },
        lifecycle={"on_timeout": "kill"},
    )
    result = sandbox.commands.run(
        AGENT_COMMAND,
        cwd="/workspace",
        envs={**ory_env, "OPENAI_API_KEY": openai_api_key},
        timeout=300,
    )
    if result.exit_code != 0:
        raise RuntimeError("E2B Agent probe failed")
    validate_running_sandbox(sandbox.sandbox_id)
    return {
        "e2b_cli_validated": True,
        "sandbox_id": sandbox.sandbox_id,
        "template_ref": template_ref,
        "status": "succeeded",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and verify the OpenAI Agents hello-world E2B deployment."
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
        template_ref = load_template_ref(args.template_file)
        output = run_probe(template_ref, read_credentials_stdin())
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
