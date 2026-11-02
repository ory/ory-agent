# OpenAI Agents hello world on E2B

This example protects one OpenAI Agents function tool, `Greet`, with [Ory Agent Security](https://agents.console.ory.com/) and runs the Agent in an [E2B](https://e2b.dev/) sandbox.

## Build and Deploy

Ory Agent Security separates application integration from runtime identity:

- **Build** shows how to add Agent Security to application code. In this example, [`agent.py`](agent.py) creates one `OryAgentClient`, adds the Ory guardrail to `Greet`, and adds Ory run hooks to the Agent run.
- **Deploy** creates the noninteractive identity and credential used by the running Agent. The generated `.ory/agent.yaml` selects the `openai-agents` Agent type, the `e2b` runtime, the `e2b-sandbox` recipe, and a Talos API key.
- **E2B** builds and runs the application. Agent Security does not build the E2B template or include application source in its deployment package.

The first authenticated Agent request changes the Agent Security deployment from `ready` to `active`.

## Security boundary

- The E2B template contains the Agent and its dependencies, but no credentials.
- The trusted controller holds `E2B_API_KEY`, `OPENAI_API_KEY`, and the Deploy-issued credential. `E2B_API_KEY` never enters the sandbox.
- The model and Agent Security credentials are injected only into the Agent command and are never written to the sandbox filesystem.
- Egress is denied by default and allows only OpenAI, Agent Security, and the selected Ory project.
- The sandbox runs with a bounded lifetime and is killed when it expires. Do not pause, snapshot, or fork a sandbox that has held runtime credentials.

## Prerequisites

- Python 3
- An [OpenAI API key](https://platform.openai.com/api-keys)
- An [E2B API key](https://e2b.dev/docs/getting-started/api-key) and the [E2B CLI](https://e2b.dev/docs/cli)
- An Ory Network project enabled for [Agent Security](https://agents.console.ory.com/)
- An Identity in that project to own the Agent
- [Ory Agent CLI](https://agents.console.ory.com/guides/ory-agent-cli) 1.8.0 or later

## 1. Prepare E2B

Install the controller dependencies. Set `E2B_API_KEY` only on the machine or CI runner that builds templates and creates sandboxes:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --requirement e2b/requirements-controller.txt

export E2B_API_KEY=...
```

## 2. Build the template

The template contains the Agent and its dependencies, but no credentials:

```sh
python e2b/deploy.py build \
  --tag ory-openai-hello-world-release \
  --output .ory/e2b-template.json
```

`.ory/e2b-template.json` records the template name and immutable E2B build ID. Neither the E2B key nor runtime credentials are available while the template is built.

## 3. Authorize deployment creation

In the [Agent Security web app](https://agents.console.ory.com/deploy), select the target Ory Network project, open [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys), and create a Deployment API Key for the Identity that should own this Agent. Copy the key when it is shown; it is displayed only once and is distinct from the runtime key created below.

On the machine where you run the deployment commands, provide the exact Ory Network project origin and the key. This example targets the hosted Agent Security service at `https://agents.console.ory.com`, so you do not need to configure an Agent Security origin:

```sh
export ORY_PROJECT_URL=https://example.projects.oryapis.com
export ORY_AGENT_DEPLOY_API_KEY=...

ory-agent init
```

Select these values in the interactive wizard:

- **Deployment name:** `openai-agents-hello-world-e2b`
- **Agent SDK:** **OpenAI Agents SDK (python)**
- **Runtime:** **E2B**

The runtime selection supplies the `e2b-sandbox` package recipe and its current version. `init` writes `.ory/agent.yaml`, `.ory/.gitignore`, and `.github/workflows/ory-agent-deploy.yml`, and refuses to overwrite existing files. Adapt the generated workflow from [`e2b/github-actions.yml`](e2b/github-actions.yml), then commit all three files.

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
  -- python e2b/deploy.py probe --template-file .ory/e2b-template.json
```

The controller keeps the received credential in memory and injects it only into the Agent command. After the Agent command exits successfully, it runs `e2b sandbox info` with the same `E2B_API_KEY` to prove that the sandbox is running in that E2B project. A successful probe prints `status: "succeeded"`, `e2b_cli_validated: true`, the E2B `sandbox_id`, and the immutable `template_ref`. For demonstration, the sandbox stays visible in the E2B console until E2B kills it 300 seconds after creation.

The CLI then waits for **Active** and retains the deployment for later sessions. [Deploy](https://agents.console.ory.com/deploy) shows the registered deployment and its first and last contact, while [Observe](https://agents.console.ory.com/observe) retains each session and its `Greet` events. The CLI refuses to overwrite an existing `--result-file`, so move or remove the previous result before each rerun.

The runtime receives these values from the credential stream:

```text
ORY_PROJECT_URL
ORY_AGENT_SECURITY_URL
ORY_AGENT_TYPE=openai-agents
ORY_AGENT_CREDENTIAL_METHOD=talos_api_key
ORY_AGENT_API_KEY
```

Only `ORY_AGENT_API_KEY` is secret. The SDK uses it to resolve the server-owned Agent subject and root delegation at startup. It never asks a human to sign in.

## 5. Observe and enforce

`Greet` is an application-defined tool in the `AgentTool` namespace.

1. Run the probe once with no block and confirm the tool succeeds in [Observe](https://agents.console.ory.com/observe).
2. Add a case-sensitive `Greet` tool block in [Enforce](https://agents.console.ory.com/manage) while the applicable posture is **Observe**. Run the probe again; the denial is recorded, but the tool still runs.
3. Change the applicable posture to **Enforce**. Run the probe again; the guardrail blocks `Greet` before its Python function runs.

Each probe generates a new session ID, so its lifecycle and tool events appear together as one run.

## Automate with GitHub Actions

[`e2b/github-actions.yml`](e2b/github-actions.yml) builds a new immutable template, restores the durable Agent Security credential, and passes the probe command to the pinned [`ory/ory-agent`](https://github.com/ory/ory-agent) action through `with.run`. The action installs a verified, matching `ory-agent` release, validates the deployment, delivers the credential over the controller's anonymous stdin, and verifies activation. It never exposes the credential on stdout, in an output, or in an artifact.

Create the deployment once as described above, then configure a protected GitHub Environment with:

| Kind     | Name                       | Purpose                                                                                                                  |
| -------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Variable | `ORY_PROJECT_URL`          | Exact Ory Network project origin                                                                                         |
| Variable | `ORY_AGENT_DEPLOYMENT_ID`  | Non-secret ID of the deployment created once                                                                             |
| Secret   | `ORY_AGENT_DEPLOY_API_KEY` | Deployment API Key created under [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys) for the owner      |
| Secret   | `ORY_AGENT_CREDENTIALS`    | Complete protected `credentials.env` content from that deployment                                                        |
| Secret   | `E2B_API_KEY`              | E2B controller credential                                                                                                |
| Secret   | `OPENAI_API_KEY`           | Model credential injected only into the Agent command                                                                    |

Require environment reviewers, prevent self-review, and do not expose this environment to pull requests or untrusted branches.

## Retire the deployment

- Rotate the Agent Security credential by revoking the deployment and creating a new one, then update `ORY_AGENT_DEPLOYMENT_ID` and `ORY_AGENT_CREDENTIALS`.
- When retiring the Agent, run `ory-agent deployment revoke` and delete the E2B template. Ordinary session completion never revokes the deployment.

## Troubleshooting

- An invalid, expired, or revoked runtime key exits with a deployment credential error. Revoke the old deployment and create a new one.
- E2B rejects new work when the project has no remaining credits or exceeds its limits.
