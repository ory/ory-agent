# OpenRouter Agent hello world on OpenShell

This example protects one OpenRouter Agent SDK tool, `Greet`, with [Ory Agent Security](https://agents.console.ory.com/) and runs the Agent through a hosted [NVIDIA OpenShell](https://docs.nvidia.com/openshell/) gateway.

## Build and Deploy

Ory Agent Security separates application integration from runtime identity:

- **Build** shows how to add Agent Security to application code. In this example, [`agent.ts`](agent.ts) creates one `OryAgentClient` and attaches Ory hooks to the OpenRouter model call that can invoke `Greet`.
- **Deploy** creates the noninteractive identity and credential used by the running Agent. The generated `.ory/agent.yaml` selects the `openrouter-agent` Agent type, the `openshell` runtime, the `openshell-workload` recipe, and a Talos API key.
- **OpenShell** pulls and runs the application image. Agent Security does not build the image or include application source in its deployment package.

The first authenticated Agent request changes the Agent Security deployment from `ready` to `active`.

## Security boundary

- The image contains the Agent and its dependencies, but no OpenRouter, OpenShell, or Agent Security credentials.
- The trusted controller holds `OPENSHELL_OIDC_CLIENT_SECRET`, `OPENROUTER_API_KEY`, and the Deploy-issued credential. `OPENSHELL_OIDC_CLIENT_SECRET` never enters a sandbox.
- The model and Agent Security keys are stored in per-run OpenShell providers. The sandbox receives only placeholders: `OPENROUTER_API_KEY` resolves only for Node requests to `openrouter.ai:443/api/**`, and `ORY_AGENT_API_KEY` resolves only for requests to the deployment's Agent Security host.
- The sandbox policy denies every other network destination.
- The registry pull credential belongs to the hosted runtime, not the Agent image or GitHub workflow environment.
- Cleanup is best effort. If the gateway fails during cleanup, inspect and delete leftover sandboxes and providers before rerunning.

## Prerequisites

- Node.js
- An [OpenRouter API key](https://openrouter.ai/settings/keys)
- The [OpenShell CLI](https://github.com/NVIDIA/OpenShell/releases/latest), a hosted OpenShell gateway and workspace, Docker Engine, and an OCI registry the OpenShell runtime can pull from
- An Ory Network project enabled for [Agent Security](https://agents.console.ory.com/)
- An Identity in that project to own the Agent
- [Ory Agent CLI](https://agents.console.ory.com/guides/ory-agent-cli) 1.8.0 or later

This example publishes a Linux amd64 image. The hosted OpenShell workspace must schedule it on amd64 compute nodes.

## 1. Prepare OpenShell

OpenShell does not currently provide a managed SaaS account. A hosted target is an OpenShell gateway operated by your organization or platform provider, normally on Kubernetes. Ask the OpenShell operator to provide:

| Value                       | Purpose                                                  |
| --------------------------- | -------------------------------------------------------- |
| Gateway endpoint            | Public HTTPS URL for the OpenShell gateway               |
| OIDC issuer                 | Identity provider used by the gateway                    |
| OIDC client ID and audience | Confidential client configured for unattended deployment |
| OIDC client secret          | Secret used by the OAuth client-credentials flow         |
| Workspace                   | Resource boundary used by this deployment                |

The OIDC identity needs the gateway's configured user role and **Workspace Admin** membership because this example imports provider profiles and creates short-lived providers. The operator must also configure the workspace runtime to pull the published Agent image. For a private GHCR package, use a durable, read-only registry credential in the OpenShell runtime; do not use a workflow's short-lived `GITHUB_TOKEN` as a cluster pull credential. See the OpenShell guides for [Kubernetes setup](https://docs.nvidia.com/openshell/kubernetes/setup), [HTTPS ingress](https://docs.nvidia.com/openshell/kubernetes/ingress), [gateway authentication](https://docs.nvidia.com/openshell/how-it-works/gateways/authentication), and [workspaces](https://docs.nvidia.com/openshell/how-it-works/workspaces).

Install the locked Node dependencies and confirm the OpenShell CLI is available on the Linux amd64 machine where you will run the deployment commands:

```sh
npm ci
openshell --version
```

Register the hosted gateway. When `OPENSHELL_OIDC_CLIENT_SECRET` is set, OpenShell uses the noninteractive OAuth client-credentials flow instead of opening a browser:

```sh
export OPENSHELL_GATEWAY=hosted
export OPENSHELL_GATEWAY_ENDPOINT=https://gateway.example.com
export OPENSHELL_OIDC_ISSUER=https://idp.example.com/realms/openshell
export OPENSHELL_OIDC_CLIENT_ID=openshell-deploy
export OPENSHELL_OIDC_AUDIENCE=openshell-cli
export OPENSHELL_OIDC_CLIENT_SECRET=...
export OPENSHELL_WORKSPACE=agent-security-examples

openshell gateway add "$OPENSHELL_GATEWAY_ENDPOINT" \
  --name "$OPENSHELL_GATEWAY" \
  --oidc-issuer "$OPENSHELL_OIDC_ISSUER" \
  --oidc-client-id "$OPENSHELL_OIDC_CLIENT_ID" \
  --oidc-audience "$OPENSHELL_OIDC_AUDIENCE"
openshell status --output json
openshell whoami --output json
```

Import the example's OpenRouter and Agent Security provider profiles into that workspace:

```sh
node openshell/deploy.ts setup
```

## 2. Build the image

Log in to an OCI registry, then build and push the credential-free image:

```sh
docker login ghcr.io
node openshell/deploy.ts build \
  --tag "ghcr.io/example/agent-repository:openshell-$(git rev-parse HEAD)" \
  --push \
  --output .ory/openshell-image.json
```

`.ory/openshell-image.json` records the immutable registry digest used by OpenShell. Verify that the hosted runtime can pull that digest before continuing.

## 3. Authorize deployment creation

In the [Agent Security web app](https://agents.console.ory.com/deploy), select the target Ory Network project, open [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys), and create a Deployment API Key for the Identity that should own this Agent. Copy the key when it is shown; it is displayed only once and is distinct from the runtime key created below.

On the machine where you run the deployment commands, provide the exact Ory Network project origin and the key. This example targets the hosted Agent Security service at `https://agents.console.ory.com`, so you do not need to configure an Agent Security origin:

```sh
export ORY_PROJECT_URL=https://example.projects.oryapis.com
export ORY_AGENT_DEPLOY_API_KEY=...

ory-agent init
```

Select these values in the interactive wizard:

- **Deployment name:** `openrouter-agent-hello-world-openshell`
- **Agent SDK:** **OpenRouter Agent SDK (typescript)**
- **Runtime:** **OpenShell**

The runtime selection supplies the `openshell-workload` package recipe and its current version. `init` writes `.ory/agent.yaml`, `.ory/.gitignore`, and `.github/workflows/ory-agent-deploy.yml`, and refuses to overwrite existing files. Adapt the generated workflow from [`openshell/github-actions.yml`](openshell/github-actions.yml), then commit all three files.

Verify the Deployment API Key and the generated manifest without creating a runtime credential:

```sh
ory-agent auth status
ory-agent deployment validate --manifest .ory/agent.yaml
```

## 4. Run the activation probe

`deployment run` creates the deployment once and reuses it for later sessions. It stores the credential in protected `.ory/credentials.env` and deployment state in `.ory/deployment.json`, launches the controller, and sends the runtime configuration only through the controller's anonymous stdin:

```sh
export OPENROUTER_API_KEY=...
ory-agent deployment run \
  --manifest .ory/agent.yaml \
  --result-file .ory/deployment-result.json \
  -- node openshell/deploy.ts probe --image-file .ory/openshell-image.json
```

The controller creates one OpenRouter provider and one Agent Security provider in the hosted workspace, runs the image in a sandbox restricted by the example policy, and deletes the sandbox and providers when the probe finishes. A successful probe prints `status: "succeeded"`.

The CLI then waits for **Active** and retains the deployment for later sessions. [Deploy](https://agents.console.ory.com/deploy) shows the registered deployment and its first and last contact, while [Observe](https://agents.console.ory.com/observe) retains each session and its `Greet` events. The CLI refuses to overwrite an existing `--result-file`, so move or remove the previous result before each rerun.

The runtime receives these values from the credential stream:

```text
ORY_PROJECT_URL
ORY_AGENT_SECURITY_URL
ORY_AGENT_TYPE=openrouter-agent
ORY_AGENT_CREDENTIAL_METHOD=talos_api_key
ORY_AGENT_API_KEY
```

Only `ORY_AGENT_API_KEY` is secret. The SDK uses it to resolve the server-owned Agent subject and root delegation at startup. It never asks a human to sign in.

## 5. Observe and enforce

`Greet` is an application-defined tool in the `AgentTool` namespace.

1. Run the probe once with no block and confirm the tool succeeds in [Observe](https://agents.console.ory.com/observe).
2. Add a case-sensitive `Greet` tool block in [Enforce](https://agents.console.ory.com/manage) while the applicable posture is **Observe**. Run the probe again; the denial is recorded, but the tool still runs.
3. Change the applicable posture to **Enforce**. Run the probe again; the Ory `PreToolUse` hook blocks `Greet` before `execute` runs.

Each probe generates a new session ID, so its lifecycle and tool events appear together as one run.

## Automate with GitHub Actions

[`openshell/github-actions.yml`](openshell/github-actions.yml) connects to OpenShell, builds and publishes a new image, restores the durable Agent Security credential, and passes the probe command to the pinned [`ory/ory-agent`](https://github.com/ory/ory-agent) action through `with.run`. The action installs a verified, matching `ory-agent` release, validates the deployment, delivers the credential over the controller's anonymous stdin, and verifies activation. It never exposes the credential on stdout, in an output, or in an artifact.

Create the deployment once as described above, then configure a protected GitHub Environment with:

| Kind     | Name                           | Purpose                                                                                                             |
| -------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Variable | `ORY_PROJECT_URL`              | Exact Ory Network project origin                                                                                    |
| Variable | `ORY_AGENT_DEPLOYMENT_ID`      | Non-secret ID of the deployment created once                                                                        |
| Variable | `OPENSHELL_GATEWAY_ENDPOINT`   | Public HTTPS gateway URL                                                                                            |
| Variable | `OPENSHELL_OIDC_ISSUER`        | Gateway's OIDC issuer                                                                                               |
| Variable | `OPENSHELL_OIDC_CLIENT_ID`     | Confidential CI client ID                                                                                           |
| Variable | `OPENSHELL_OIDC_AUDIENCE`      | Gateway token audience, normally `openshell-cli`                                                                    |
| Variable | `OPENSHELL_WORKSPACE`          | Hosted workspace used by the deployment                                                                             |
| Secret   | `ORY_AGENT_DEPLOY_API_KEY`     | Deployment API Key created under [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys) for the owner |
| Secret   | `ORY_AGENT_CREDENTIALS`        | Complete protected `credentials.env` content from that deployment                                                   |
| Secret   | `OPENSHELL_OIDC_CLIENT_SECRET` | Confidential CI client secret                                                                                       |
| Secret   | `OPENROUTER_API_KEY`           | Model credential stored only in a per-run OpenShell provider                                                        |

The workflow also needs `packages: write` so its standard `GITHUB_TOKEN` can push the repository-scoped Agent image to `ghcr.io/<owner>/<repository>`. Configure the hosted OpenShell runtime with separate, durable pull access or make the package public.

Require environment reviewers, prevent self-review, and do not expose this environment to pull requests or untrusted branches.

## Retire the deployment

- Rotate the Agent Security credential by revoking the deployment and creating a new one, then update `ORY_AGENT_DEPLOYMENT_ID` and `ORY_AGENT_CREDENTIALS`.
- When retiring the Agent, run `ory-agent deployment revoke`, delete any leftover OpenShell providers, and remove the published image. Ordinary session completion never revokes the deployment.

## Troubleshooting

- An invalid, expired, or revoked runtime key exits with a deployment credential error. Revoke the old deployment and create a new one.
- If `openshell status` connects but authentication fails, verify the OIDC issuer, audience, client secret, token role, and workspace membership.
- If the sandbox cannot pull the digest in `.ory/openshell-image.json`, configure registry access in the hosted runtime's sandbox namespace.
- If an imported provider profile must change, update it with `openshell profile update`; profile import is create-only.
