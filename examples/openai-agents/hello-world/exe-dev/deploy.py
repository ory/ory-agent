"""Trusted controller for the OpenAI Agents hello world on exe.dev.

The controller packages the credential-free Agent, creates a short-lived exe.dev standalone
VM over SSH, installs the Agent, and starts it with the Deploy-issued runtime values streamed
over the SSH session's standard input. The values never reach the VM disk, command arguments,
or the exe.dev control plane. The VM is always deleted afterwards.

It uses only the Python standard library and the OpenSSH client.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import secrets
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FILES = {
    "agent.py": EXAMPLE_ROOT / "agent.py",
    "requirements.txt": EXAMPLE_ROOT / "exe-dev" / "requirements-runtime.txt",
}
REQUIRED_ORY_ENV = {
    "ORY_PROJECT_URL",
    "ORY_AGENT_SECURITY_URL",
    "ORY_AGENT_TYPE",
    "ORY_AGENT_CREDENTIAL_METHOD",
    "ORY_AGENT_API_KEY",
}
COMPATIBILITY_ORY_ENV = {"ORY_AGENT_SUBJECT_ID", "ORY_AGENT_ROOT_DELEGATION"}
MAX_CREDENTIAL_BYTES = 1024 * 1024

# Documented exe.dev lobby host key fingerprint: https://exe.dev/docs/faq/host-key
EXE_DEV_LOBBY = "exe.dev"
EXE_DEV_HOST_KEY_FINGERPRINT = "SHA256:JJOP/lwiBGOMilfONPWZCXUrfK154cnJFXcqlsi6lPo"
VM_DOMAIN = "exe.xyz"
VM_NAME_PREFIX = "ory-openai-hello"

INSTALL_COMMAND = (
    "set -eu; cd ~/agent; "
    "if ! python3 -m venv .venv >/dev/null 2>&1; then "
    "sudo apt-get update -qq && "
    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null && "
    "python3 -m venv .venv; fi; "
    ".venv/bin/python -m pip install --quiet --disable-pip-version-check "
    "--requirement requirements.txt"
)
# The runtime values arrive on stdin as shell-quoted assignments. `set -a` exports them to the
# Agent process only; nothing is written to disk and nothing appears in the process arguments.
AGENT_COMMAND = (
    "set -a && . /dev/stdin && set +a && cd ~/agent && exec .venv/bin/python agent.py"
)
AGENT_TIMEOUT_SECONDS = 300


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
    if values["ORY_AGENT_TYPE"] != "openai-agents":
        raise ValueError("ORY_AGENT_TYPE must be openai-agents")
    if values["ORY_AGENT_CREDENTIAL_METHOD"] != "talos_api_key":
        raise ValueError("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key")
    for name in ("ORY_PROJECT_URL", "ORY_AGENT_SECURITY_URL"):
        require_https_origin(values[name], name)
    return {name: values[name] for name in REQUIRED_ORY_ENV}


def require_https_origin(origin: str, name: str) -> None:
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


def shell_assignments(environment: Mapping[str, str]) -> str:
    """Encode values as POSIX shell assignments that `. /dev/stdin` reads verbatim."""
    lines = []
    for name in sorted(environment):
        if not name.isidentifier() or not name.isascii():
            raise ValueError(f"invalid environment name: {name}")
        lines.append(f"{name}={shlex.quote(environment[name])}")
    return "\n".join(lines) + "\n"


def read_credentials_stdin() -> str:
    if sys.stdin.isatty():
        raise ValueError("probe requires deployment credentials on stdin")
    contents = sys.stdin.read(MAX_CREDENTIAL_BYTES + 1)
    if len(contents.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise ValueError("deployment credentials exceed 1 MiB")
    return contents


def build_bundle() -> bytes:
    """Create a reproducible, credential-free archive of the Agent and its pinned dependencies."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, source in sorted(BUNDLE_FILES.items()):
            data = source.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o500 if name.endswith(".py") else 0o400
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    # A zero gzip timestamp keeps the digest dependent only on file contents.
    return gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0)


def write_bundle(output: Path) -> dict[str, str]:
    bundle = build_bundle()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bundle)
    artifact = {"path": str(output), "sha256": hashlib.sha256(bundle).hexdigest()}
    artifact_path = output.with_suffix("").with_suffix(".json")
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {**artifact, "artifact": str(artifact_path)}


def load_bundle(artifact_path: Path) -> tuple[bytes, str]:
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        bundle_path = Path(artifact["path"])
        expected = artifact["sha256"]
        bundle = bundle_path.read_bytes()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid exe.dev bundle artifact: {artifact_path}") from error
    actual = hashlib.sha256(bundle).hexdigest()
    if not isinstance(expected, str) or not secrets.compare_digest(actual, expected):
        raise ValueError("exe.dev bundle digest does not match its artifact")
    return bundle, actual


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class ExeDev:
    """Runs exe.dev commands through the OpenSSH client with pinned host keys."""

    known_hosts: Path
    identity_file: str | None = None
    run: Runner = subprocess.run

    def _ssh(self, host: str, strict: str) -> list[str]:
        command = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=20",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"StrictHostKeyChecking={strict}",
            "-o", "LogLevel=ERROR",
        ]  # fmt: skip
        if self.identity_file:
            command += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        return command + [host]

    def lobby(self, *arguments: str, timeout: int = 120) -> str:
        result = self.run(
            self._ssh(EXE_DEV_LOBBY, "yes") + list(arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or "command failed"
            raise RuntimeError(f"exe.dev {arguments[0]} failed: {detail}")
        return result.stdout

    def vm(
        self,
        name: str,
        command: str,
        *,
        input_data: bytes | None = None,
        timeout: int = 600,
        capture: bool = True,
    ) -> subprocess.CompletedProcess:
        # exe.dev does not publish VM host keys, so each VM key is trusted on first use in the
        # per-run known-hosts file, which is discarded with the VM.
        return self.run(
            self._ssh(f"{name}.{VM_DOMAIN}", "accept-new") + [command],
            input=input_data if input_data is not None else b"",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
            check=False,
        )


def pin_lobby_host_key(known_hosts: Path, run: Runner = subprocess.run) -> None:
    scan = run(
        ["ssh-keyscan", "-T", "15", "-t", "ed25519", EXE_DEV_LOBBY],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    keys = [
        line for line in scan.stdout.splitlines() if line and not line.startswith("#")
    ]
    if scan.returncode != 0 or len(keys) != 1:
        raise RuntimeError("could not read the exe.dev host key")
    fingerprint = run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", "-"],
        input=keys[0] + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    fields = fingerprint.stdout.split()
    if fingerprint.returncode != 0 or len(fields) < 2:
        raise RuntimeError("could not fingerprint the exe.dev host key")
    if not secrets.compare_digest(fields[1], EXE_DEV_HOST_KEY_FINGERPRINT):
        raise RuntimeError(
            f"exe.dev host key fingerprint {fields[1]} does not match the documented "
            f"{EXE_DEV_HOST_KEY_FINGERPRINT}"
        )
    known_hosts.write_text(keys[0] + "\n", encoding="utf-8")
    known_hosts.chmod(0o600)


def wait_for_vm(
    exe: ExeDev,
    name: str,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 30,
    delay: float = 5,
) -> None:
    for attempt in range(attempts):
        if exe.vm(name, "true", timeout=60).returncode == 0:
            return
        if attempt + 1 < attempts:
            sleep(delay)
    raise RuntimeError(f"exe.dev VM {name} did not accept SSH connections")


def stderr_tail(result: subprocess.CompletedProcess, limit: int = 2000) -> str:
    """Return the end of a credential-free setup command's stderr for diagnostics."""
    stderr = result.stderr or b""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return stderr.strip()[-limit:] or f"exit status {result.returncode}"


def vm_is_listed(exe: ExeDev, name: str) -> bool:
    listing = json.loads(exe.lobby("ls", "--json"))
    vms = listing.get("vms", []) if isinstance(listing, dict) else []
    return any(isinstance(vm, dict) and vm.get("vm_name") == name for vm in vms)


def run_probe(
    exe: ExeDev,
    bundle: bytes,
    bundle_sha256: str,
    credentials: str,
    openai_api_key: str,
    *,
    vm_name: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    ory_env = load_credentials(credentials)
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is required")
    name = vm_name or f"{VM_NAME_PREFIX}-{secrets.token_hex(4)}"

    exe.lobby("new", "--sandbox", f"--name={name}", timeout=300)
    deleted = False
    try:
        validated = vm_is_listed(exe, name)
        if not validated:
            raise RuntimeError(f"exe.dev does not list the new VM {name}")
        wait_for_vm(exe, name, sleep)

        upload = exe.vm(
            name, "mkdir -p ~/agent && tar -xzf - -C ~/agent", input_data=bundle
        )
        if upload.returncode != 0:
            raise RuntimeError(
                "uploading the Agent bundle failed: " + stderr_tail(upload)
            )
        install = exe.vm(name, INSTALL_COMMAND, timeout=900)
        if install.returncode != 0:
            raise RuntimeError(
                "installing the Agent dependencies failed: " + stderr_tail(install)
            )

        runtime_env = {**ory_env, "OPENAI_API_KEY": openai_api_key}
        agent = exe.vm(
            name,
            AGENT_COMMAND,
            input_data=shell_assignments(runtime_env).encode("utf-8"),
            timeout=AGENT_TIMEOUT_SECONDS,
            capture=False,
        )
        if agent.returncode != 0:
            raise RuntimeError("exe.dev Agent probe failed")
    finally:
        try:
            exe.lobby("rm", name, timeout=300)
            deleted = True
        except Exception as error:  # report, but keep the original failure
            print(
                f"warning: delete exe.dev VM {name} manually: {error}", file=sys.stderr
            )
    if not deleted:
        raise RuntimeError(f"exe.dev VM {name} was not deleted")
    return {
        "bundle_sha256": bundle_sha256,
        "exe_dev_validated": validated,
        "status": "succeeded",
        "vm_deleted": deleted,
        "vm_name": name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Package and verify the OpenAI Agents hello-world exe.dev deployment."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    bundle_parser = subcommands.add_parser("bundle")
    bundle_parser.add_argument("--output", required=True, type=Path)
    probe_parser = subcommands.add_parser("probe")
    probe_parser.add_argument("--bundle-file", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "bundle":
        output: dict[str, object] = dict(write_bundle(args.output))
    else:
        bundle, digest = load_bundle(args.bundle_file)
        credentials = read_credentials_stdin()
        with tempfile.TemporaryDirectory(prefix="exe-dev-") as directory:
            known_hosts = Path(directory) / "known_hosts"
            pin_lobby_host_key(known_hosts)
            exe = ExeDev(
                known_hosts=known_hosts,
                identity_file=os.environ.get("EXE_DEV_SSH_IDENTITY_FILE") or None,
            )
            output = run_probe(
                exe, bundle, digest, credentials, os.environ.get("OPENAI_API_KEY", "")
            )
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
