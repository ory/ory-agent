# OpenAI Agents hello world on exe.dev

This example runs the [OpenAI Agents hello world](../README.md) Agent, which protects one function tool, `Greet`, with [Ory Agent Security](https://agents.console.ory.com/), in a short-lived [exe.dev](https://exe.dev/) standalone VM.

## Build and Deploy

Ory Agent Security separates application integration from runtime identity:

- **Build** shows how to add Agent Security to application code. The shared [`agent.py`](../agent.py) creates one `OryAgentClient`, adds the Ory guardrail to `Greet`, and adds Ory run hooks to the Agent run.
- **Deploy** creates the noninteractive identity and credential used by the running Agent. The generated `.ory/agent.yaml` selects the `openai-agents` Agent type, the `exe-dev` runtime, the `exe-dev-sandbox` recipe, and a Talos API key.
- **exe.dev** runs the application. exe.dev VMs are full Linux VMs that you drive over SSH. Agent Security does not create the VM or include application source in its deployment package.

The first authenticated Agent request changes the Agent Security deployment from `ready` to `active`.

## Security boundary

- The Agent bundle contains `agent.py` and its pinned dependencies, but no credentials. The controller verifies its SHA-256 digest before uploading it.
- The trusted controller holds the exe.dev SSH key, `OPENAI_API_KEY`, and the Deploy-issued credential. The SSH key never enters the VM.
- exe.dev has no per-VM secret store. The controller installs the Agent first, then streams the model and Agent Security values as shell-quoted assignments over the SSH session's standard input into the Agent process. They are never written to the VM disk, passed as command arguments, put in a setup script, or sent through the exe.dev HTTPS API.
- The controller pins the documented exe.dev host key fingerprint, `SHA256:JJOP/lwiBGOMilfONPWZCXUrfK154cnJFXcqlsi6lPo`. exe.dev does not publish VM host keys, so each VM key is trusted on first use in a per-run known-hosts file that is discarded with the VM.
- Each run creates a uniquely named standalone VM and deletes it with `ssh exe.dev rm` after success or failure. exe.dev disks are persistent, so never copy, back up, or reuse a VM that has held runtime credentials.
- exe.dev egress is either `all` or `integrations`. `integrations` blocks the public internet, including OpenAI and Agent Security, so this VM keeps the default `all`. The Agent exposes no HTTP service, and the VM's HTTPS proxy stays private, which is the exe.dev default.

## Prerequisites

- Python 3 and the OpenSSH client (`ssh`, `ssh-keyscan`, `ssh-keygen`)
- An [exe.dev](https://exe.dev/) account with a plan that allows [standalone VMs](https://exe.dev/docs/standalone-vms)
- An [OpenAI API key](https://platform.openai.com/api-keys)
- An Ory Network project enabled for [Agent Security](https://agents.console.ory.com/)
- An Identity in that project to own the Agent
- [Ory Agent CLI](https://agents.console.ory.com/guides/ory-agent-cli) 1.8.0 or later

Run every command below from this `exe-dev` directory.

## 1. Prepare exe.dev

If this is a new account, run `ssh exe.dev` once interactively and follow the registration prompts. Then create a dedicated SSH key for the controller and register its public key with your exe.dev account; `ssh exe.dev help ssh-key` lists the key-management commands.

```sh
ssh-keygen -t ed25519 -N '' -C ory-agent-exe-dev -f ~/.ssh/ory-agent-exe-dev
export EXE_DEV_SSH_IDENTITY_FILE=~/.ssh/ory-agent-exe-dev
```

The controller needs no Python packages beyond the standard library.

## 2. Build the Agent bundle

The bundle contains the Agent and its pinned dependency list, but no credentials:

```sh
python3 deploy.py bundle --output .ory/exe-dev-bundle.tar.gz
```

The archive is reproducible: the same sources always produce the same digest. `.ory/exe-dev-bundle.json` records its path and SHA-256 digest.

## 3. Authorize deployment creation

In the [Agent Security web app](https://agents.console.ory.com/deploy), select the target Ory Network project, open [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys), and create a Deployment API Key for the Identity that should own this Agent. Copy the key when it is shown; it is displayed only once and is distinct from the runtime key created below.

On the machine where you run the deployment commands, provide the exact Ory Network project origin and the key. This example targets the hosted Agent Security service at `https://agents.console.ory.com`, so you do not need to configure an Agent Security origin:

```sh
export ORY_PROJECT_URL=https://example.projects.oryapis.com
export ORY_AGENT_DEPLOY_API_KEY=...

ory-agent init
```

Select these values in the interactive wizard:

- **Deployment name:** `openai-agents-hello-world-exe-dev`
- **Agent SDK:** **OpenAI Agents SDK (python)**
- **Runtime:** **exe.dev**

The runtime selection supplies the `exe-dev-sandbox` package recipe and its current version. `init` writes `.ory/agent.yaml`, `.ory/.gitignore`, and `.github/workflows/ory-agent-deploy.yml`, and refuses to overwrite existing files. Adapt the generated workflow from [`github-actions.yml`](github-actions.yml), then commit all three files.

Verify the Deployment API Key and the generated manifest without creating a runtime credential:

```sh
ory-agent auth status
ory-agent deployment validate --manifest .ory/agent.yaml
```

## 4. Run the activation probe

`deployment run` creates the deployment once and reuses it for later sessions. It stores the credential in protected `.ory/credentials.env` and deployment state in `.ory/deployment.json`, launches the controller, and sends the runtime configuration only through the controller's anonymous stdin:

```sh
export OPENAI_API_KEY=...
ory-agent deployment run \
  --manifest .ory/agent.yaml \
  --result-file .ory/deployment-result.json \
  -- python3 deploy.py probe --bundle-file .ory/exe-dev-bundle.json
```

The controller then:

1. verifies the exe.dev host key and the bundle digest
2. creates a standalone VM with `ssh exe.dev new --sandbox --name=ory-openai-hello-<random>` and confirms that `ssh exe.dev ls --json` lists it
3. uploads the bundle and installs its dependencies in a virtual environment before any credential is sent
4. starts `agent.py` with the runtime values streamed over the session's stdin
5. deletes the VM with `ssh exe.dev rm`

A successful probe prints `status: "succeeded"`, `exe_dev_validated: true`, `vm_deleted: true`, the `vm_name`, and the `bundle_sha256`.

The CLI then waits for **Active** and retains the deployment for later sessions. [Deploy](https://agents.console.ory.com/deploy) shows the registered deployment and its first and last contact, while [Observe](https://agents.console.ory.com/observe) retains each session and its `Greet` events. The CLI refuses to overwrite an existing `--result-file`, so move or remove the previous result before each rerun.

The Agent process receives these values from the credential stream, plus `OPENAI_API_KEY`:

```text
ORY_PROJECT_URL
ORY_AGENT_SECURITY_URL
ORY_AGENT_TYPE=openai-agents
ORY_AGENT_CREDENTIAL_METHOD=talos_api_key
ORY_AGENT_API_KEY
```

Only `ORY_AGENT_API_KEY` and `OPENAI_API_KEY` are secret. The SDK uses `ORY_AGENT_API_KEY` to resolve the server-owned Agent subject and root delegation at startup. It never asks a human to sign in.

## 5. Observe and enforce

`Greet` is an application-defined tool in the `AgentTool` namespace.

1. Run the probe once with no block and confirm the tool succeeds in [Observe](https://agents.console.ory.com/observe).
2. Add a case-sensitive `Greet` tool block in [Enforce](https://agents.console.ory.com/manage) while the applicable posture is **Observe**. Run the probe again; the denial is recorded, but the tool still runs.
3. Change the applicable posture to **Enforce**. Run the probe again; the guardrail blocks `Greet` before its Python function runs.

Each probe creates a new VM and session ID, so its lifecycle and tool events appear together as one run.

## Automate with GitHub Actions

[`github-actions.yml`](github-actions.yml) builds the Agent bundle, restores the durable Agent Security credential and the exe.dev SSH key, and passes the probe command to the pinned [`ory/ory-agent`](https://github.com/ory/ory-agent) action through `with.run`. The action installs a verified, matching `ory-agent` release, validates the deployment, delivers the credential over the controller's anonymous stdin, and verifies activation. It never exposes the credential on stdout, in an output, or in an artifact.

Create the deployment once as described above, then configure a protected GitHub Environment with:

| Kind     | Name                       | Purpose                                                                                                             |
| -------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Variable | `ORY_PROJECT_URL`          | Exact Ory Network project origin                                                                                    |
| Variable | `ORY_AGENT_DEPLOYMENT_ID`  | Non-secret ID of the deployment created once                                                                        |
| Secret   | `ORY_AGENT_DEPLOY_API_KEY` | Deployment API Key created under [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys) for the owner |
| Secret   | `ORY_AGENT_CREDENTIALS`    | Complete protected `credentials.env` content from that deployment                                                   |
| Secret   | `EXE_DEV_SSH_PRIVATE_KEY`  | Private key of a dedicated SSH key registered with the exe.dev account                                              |
| Secret   | `OPENAI_API_KEY`           | Model credential streamed only into the Agent process                                                               |

Use a dedicated exe.dev SSH key for CI so you can remove it from the account without affecting people. Do not expose this environment to pull requests or untrusted branches.

## Retire the deployment

- Rotate the Agent Security credential by revoking the deployment and creating a new one, then update `ORY_AGENT_DEPLOYMENT_ID` and `ORY_AGENT_CREDENTIALS`.
- When retiring the Agent, run `ory-agent deployment revoke`, remove the controller's SSH key from exe.dev, and confirm that `ssh exe.dev ls` lists no `ory-openai-hello-` VMs. Ordinary session completion never revokes the deployment.

## Troubleshooting

- `exe.dev host key fingerprint ... does not match`: the connection did not reach exe.dev, or exe.dev rotated its key. Compare against the [published fingerprint](https://exe.dev/docs/faq/host-key) before changing `EXE_DEV_HOST_KEY_FINGERPRINT`.
- `exe.dev new failed`: the account has no plan, standalone VMs are disabled for the team (`team settings standalone`), or the account has reached its VM limit.
- `installing the Agent dependencies failed`: the VM image could not create a Python virtual environment or reach PyPI. The error includes the end of the install output, which contains no credentials.
- If the controller is interrupted before cleanup, it prints the VM name to delete. Remove leftovers with `ssh exe.dev rm <vm-name>`; exe.dev holds a deleted name for 24 hours.
- An invalid, expired, or revoked runtime key exits with a deployment credential error. Revoke the old deployment and create a new one.
