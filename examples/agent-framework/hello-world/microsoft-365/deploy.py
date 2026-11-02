from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent
TEMPLATE_FILE = HERE / "main.bicep"
MANIFEST_FILE = HERE / "manifest.json"

REQUIRED_ORY_ENV = {
    "ORY_PROJECT_URL",
    "ORY_AGENT_SECURITY_URL",
    "ORY_AGENT_TYPE",
    "ORY_AGENT_CREDENTIAL_METHOD",
    "ORY_AGENT_API_KEY",
}
COMPATIBILITY_ORY_ENV = {"ORY_AGENT_SUBJECT_ID", "ORY_AGENT_ROOT_DELEGATION"}
MAX_CREDENTIAL_BYTES = 1024 * 1024
IMAGE_REPOSITORY = "ory-agent-framework-hello-world"
DEPLOYMENT_NAME = "ory-agent-framework-hello-world"
DIRECT_LINE_URL = "https://directline.botframework.com/v3/directline"
PROBE_PROMPT = "Use the Greet tool, then tell me what the tool returned."
EXPECTED_REPLY = "Hello, world!"
PROBE_USER_ID = "ory-activation-probe"
# Stable namespace so the Microsoft 365 app ID follows the bot registration.
APP_ID_NAMESPACE = uuid.UUID("5b2bd0ae-6f0c-4b0b-9a43-6f7d7a1f3c55")


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
    if values["ORY_AGENT_TYPE"] != "agent-framework":
        raise ValueError("ORY_AGENT_TYPE must be agent-framework")
    if values["ORY_AGENT_CREDENTIAL_METHOD"] != "talos_api_key":
        raise ValueError("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key")
    for name in ("ORY_PROJECT_URL", "ORY_AGENT_SECURITY_URL"):
        require_https_origin(values[name], name)
    return {name: values[name] for name in REQUIRED_ORY_ENV}


def require_https_origin(origin: str, name: str) -> str:
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


def read_credentials_stdin() -> str:
    if sys.stdin.isatty():
        raise ValueError("deploy requires deployment credentials on stdin")
    contents = sys.stdin.read(MAX_CREDENTIAL_BYTES + 1)
    if len(contents.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise ValueError("deployment credentials exceed 1 MiB")
    return contents


def az(*args: str, stream: bool = False) -> str:
    """Run Azure CLI. Captured output keeps returned values off public stdout."""
    try:
        result = subprocess.run(
            ["az", *args, "--only-show-errors"],
            stdout=sys.stderr if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Azure CLI is required") from error
    if result.returncode != 0:
        command = " ".join(args[:2])
        detail = "see output above" if stream else result.stderr.strip()
        raise RuntimeError(f"az {command} failed: {detail}")
    return "" if stream else result.stdout.strip()


def load_image(path: Path) -> str:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid image artifact: {path}") from error
    image = artifact.get("image") if isinstance(artifact, dict) else None
    if not isinstance(image, str) or not re.fullmatch(
        r"[a-z0-9.-]+\.azurecr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", image
    ):
        raise ValueError(
            f"image artifact must contain a digest-pinned ACR image: {path}"
        )
    return image


def build_image(resource_group: str, registry: str, tag: str) -> dict[str, str]:
    login_server = az(
        "acr",
        "show",
        "--resource-group",
        resource_group,
        "--name",
        registry,
        "--query",
        "loginServer",
        "--output",
        "tsv",
    )
    reference = f"{IMAGE_REPOSITORY}:{tag}"
    # ACR Tasks builds in Azure, so the controller needs no local Docker daemon. Build logs
    # contain no credentials and stream to stderr.
    az(
        "acr",
        "build",
        "--resource-group",
        resource_group,
        "--registry",
        registry,
        "--image",
        reference,
        "--platform",
        "linux/amd64",
        str(APP_DIR),
        stream=True,
    )
    digest = az(
        "acr",
        "repository",
        "show",
        "--name",
        registry,
        "--image",
        reference,
        "--query",
        "digest",
        "--output",
        "tsv",
    )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError(f"ACR returned an invalid digest for {reference}")
    return {"image": f"{login_server}/{IMAGE_REPOSITORY}@{digest}", "tag": tag}


def deploy_template(
    *,
    subscription_id: str,
    resource_group: str,
    parameters: dict[str, object],
) -> dict[str, str]:
    from azure.identity import AzureCliCredential
    from azure.mgmt.resource.deployments import DeploymentsMgmtClient

    template = json.loads(
        az("bicep", "build", "--file", str(TEMPLATE_FILE), "--stdout")
    )
    client = DeploymentsMgmtClient(AzureCliCredential(), subscription_id)
    # Secure parameters travel to Azure Resource Manager in this request body only. ARM does
    # not return them in deployment history, operations, or outputs.
    poller = client.deployments.begin_create_or_update(
        resource_group,
        DEPLOYMENT_NAME,
        {
            "properties": {
                "mode": "Incremental",
                "template": template,
                "parameters": {
                    name: {"value": value} for name, value in parameters.items()
                },
            }
        },
    )
    outputs = poller.result().properties.outputs or {}
    return {name: output["value"] for name, output in outputs.items()}


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def wait_for_revision(endpoint: str, revision: str, timeout: float = 300) -> None:
    """Wait until the released revision, not its predecessor, serves traffic."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            health = request_json("GET", f"{endpoint}/healthz")
            if health.get("status") == "ok" and health.get("revision") == revision:
                return
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"revision {revision} did not become ready")
        time.sleep(5)


def agent_reply(activities: list[dict[str, object]]) -> str | None:
    for activity in activities:
        sender = activity.get("from")
        if (
            activity.get("type") == "message"
            and isinstance(sender, dict)
            and sender.get("id") != PROBE_USER_ID
            and isinstance(activity.get("text"), str)
        ):
            return activity["text"]
    return None


def probe_direct_line(resource_group: str, bot_name: str, timeout: float = 120) -> str:
    """Send one turn through the Azure Bot and return the Agent's reply."""
    secret = az(
        "bot",
        "directline",
        "show",
        "--resource-group",
        resource_group,
        "--name",
        bot_name,
        "--with-secrets",
        "true",
        "--query",
        "properties.properties.sites[0].key",
        "--output",
        "tsv",
    )
    if not secret:
        raise RuntimeError("Direct Line site key is unavailable")
    conversation = request_json(
        "POST", f"{DIRECT_LINE_URL}/conversations", token=secret
    )
    conversation_id = conversation["conversationId"]
    # Use the conversation-scoped token for the rest of the probe.
    token = str(conversation["token"])
    activities_url = f"{DIRECT_LINE_URL}/conversations/{conversation_id}/activities"
    request_json(
        "POST",
        activities_url,
        token=token,
        body={"type": "message", "from": {"id": PROBE_USER_ID}, "text": PROBE_PROMPT},
    )

    watermark = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        query = f"?watermark={watermark}" if watermark else ""
        page = request_json("GET", activities_url + query, token=token)
        watermark = str(page.get("watermark") or watermark)
        reply = agent_reply(page.get("activities") or [])
        if reply is not None:
            return reply
        time.sleep(2)
    raise RuntimeError("the Agent did not reply through Direct Line")


def run_deploy(args: argparse.Namespace, credentials: str) -> dict[str, object]:
    ory_env = load_credentials(credentials)
    image = load_image(args.image_file)
    subscription_id = args.subscription or az(
        "account", "show", "--query", "id", "--output", "tsv"
    )

    # Container Apps keeps running revisions on their old secret values.
    revision_suffix = f"r{time.time_ns() // 1_000_000}"
    parameters: dict[str, object] = {
        "namePrefix": args.name_prefix,
        "registryName": args.registry,
        "image": image,
        "revisionSuffix": revision_suffix,
        "modelName": args.model,
        "modelVersion": args.model_version,
        "modelSku": args.model_sku,
        "runtimeSecrets": ory_env,
    }
    # Regional resources default to the resource group's region; override it when that
    # region lacks Container Apps capacity or model quota.
    if args.location:
        parameters["location"] = args.location
    outputs = deploy_template(
        subscription_id=subscription_id,
        resource_group=args.resource_group,
        parameters=parameters,
    )
    endpoint = outputs["endpoint"]
    revision = f"{outputs['containerAppName']}--{revision_suffix}"
    wait_for_revision(endpoint, revision)
    reply = probe_direct_line(args.resource_group, outputs["botName"])
    if EXPECTED_REPLY not in reply:
        raise RuntimeError(f"unexpected Agent reply: {reply!r}")
    return {
        "status": "succeeded",
        "image": image,
        "endpoint": endpoint,
        "container_app": outputs["containerAppName"],
        "revision": revision,
        "bot_name": outputs["botName"],
        "bot_app_id": outputs["botAppId"],
        "reply_verified": True,
    }


def png(size: int, pixel) -> bytes:
    """Encode a small RGBA PNG without third-party imaging dependencies."""
    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y, size)) for x in range(size))
        for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def color_icon_pixel(_x: int, _y: int, _size: int) -> tuple[int, int, int, int]:
    return (79, 70, 229, 255)


def outline_icon_pixel(x: int, y: int, size: int) -> tuple[int, int, int, int]:
    # Microsoft 365 outline icons are white on a transparent background.
    border = x in (2, 3, size - 4, size - 3) or y in (2, 3, size - 4, size - 3)
    inside = 2 <= x <= size - 3 and 2 <= y <= size - 3
    return (255, 255, 255, 255) if border and inside else (0, 0, 0, 0)


def render_manifest(bot_app_id: str, endpoint: str) -> dict[str, object]:
    bot_id = str(uuid.UUID(bot_app_id))
    domain = require_https_origin(endpoint, "endpoint")
    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    manifest["id"] = str(uuid.uuid5(APP_ID_NAMESPACE, bot_id))
    manifest["bots"][0]["botId"] = bot_id
    manifest["copilotAgents"]["customEngineAgents"][0]["id"] = bot_id
    manifest["validDomains"] = [domain]
    return manifest


def build_app_package(bot_app_id: str, endpoint: str) -> bytes:
    manifest = render_manifest(bot_app_id, endpoint)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr("color.png", png(192, color_icon_pixel))
        archive.writestr("outline.png", png(32, outline_icon_pixel))
    return buffer.getvalue()


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build, deploy, verify, and package the Microsoft 365 custom engine agent.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="build the image with ACR Tasks")
    build.add_argument("--resource-group", required=True)
    build.add_argument("--registry", required=True, help="existing ACR name")
    build.add_argument("--tag", required=True)
    build.add_argument("--output", type=Path, default=Path(".ory/image.json"))

    deploy = subcommands.add_parser(
        "deploy",
        help="deploy the image with stdin credentials and verify one Direct Line turn",
    )
    deploy.add_argument("--resource-group", required=True)
    deploy.add_argument("--registry", required=True, help="existing ACR name")
    deploy.add_argument("--name-prefix", required=True)
    deploy.add_argument("--image-file", type=Path, default=Path(".ory/image.json"))
    deploy.add_argument("--subscription", default=os.getenv("AZURE_SUBSCRIPTION_ID"))
    deploy.add_argument(
        "--location",
        default=os.getenv("AZURE_LOCATION") or None,
        help="region for regional resources (default: the resource group's region)",
    )
    deploy.add_argument("--model", default=os.getenv("FOUNDRY_MODEL", "gpt-5.4-mini"))
    deploy.add_argument(
        "--model-version", default=os.getenv("FOUNDRY_MODEL_VERSION", "2026-03-17")
    )
    deploy.add_argument(
        "--model-sku",
        choices=("GlobalStandard", "DataZoneStandard"),
        default=os.getenv("FOUNDRY_MODEL_SKU") or "GlobalStandard",
    )

    package = subcommands.add_parser(
        "package", help="write the Microsoft 365 app package"
    )
    package.add_argument("--bot-app-id", required=True)
    package.add_argument("--endpoint", required=True, help="Agent HTTPS origin")
    package.add_argument(
        "--output", type=Path, default=HERE / "build" / "appPackage.zip"
    )

    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build_image(args.resource_group, args.registry, args.tag)
            write_json(args.output, result)
        elif args.command == "deploy":
            result = run_deploy(args, read_credentials_stdin())
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(build_app_package(args.bot_app_id, args.endpoint))
            result = {"status": "succeeded", "app_package": str(args.output)}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
