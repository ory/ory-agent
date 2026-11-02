# Claude Code on E2B

This example runs [Claude Code](https://docs.anthropic.com/en/docs/claude-code) headlessly with [Ory Agent Security](https://agents.console.ory.com/) in an [E2B](https://e2b.dev/) sandbox. The probe asks Claude Code to use `Bash` to run [`greet.mjs`](greet.mjs); the Ory plugin observes or enforces both `AgentTool:Bash#use` and the decomposed `ShellTool:node#use` check before the command executes.

## Build and Deploy

Ory Agent Security separates application integration from runtime identity:

- **Build** shows how to add Agent Security to the harness. In this example, [`e2b/template.py`](e2b/template.py) extends E2B's `claude` template and preinstalls the [Ory Agent plugin for Claude Code](https://www.npmjs.com/package/@ory/claude-code), which checks every tool call in its `PreToolUse` hook and reports session activity.
- **Deploy** creates the noninteractive identity and credential used by the running harness. The generated `.ory/agent.yaml` selects the `claude-code` Agent type, the `e2b` runtime, the `e2b-sandbox` recipe, and a Talos API key.
- **E2B** builds and runs the harness. Agent Security does not build the E2B template or include application source in its deployment package.

The first authenticated plugin request changes the Agent Security deployment from `ready` to `active`.

## Security boundary

- The E2B template contains Claude Code, the pinned Ory plugin, and the example tool, but no credentials.
- The trusted controller holds `E2B_API_KEY`, `ANTHROPIC_API_KEY`, and the Deploy-issued credential. `E2B_API_KEY` never enters the sandbox.
- The model and Agent Security credentials are injected only into the Claude Code command, but Claude Code and every tool subprocess can read that command environment. Use short-lived, least-privilege credentials and trusted prompts and source; do not run untrusted repository content with this pattern.
- Egress is denied by default and allows only Anthropic, Agent Security, and the selected Ory project. This reduces destinations but does not isolate command credentials from model-controlled tools.
- The sandbox is killed after the probe. Do not pause, snapshot, or fork a sandbox that has held runtime credentials.
- `--dangerously-skip-permissions` disables Claude Code's interactive approval prompts inside the isolated sandbox; it does not bypass Ory's `PreToolUse` hook.

## Prerequisites

- Python 3.12
- An [Anthropic API key](https://console.anthropic.com/settings/keys)
- An [E2B API key](https://e2b.dev/docs/getting-started/api-key) and the [E2B CLI](https://e2b.dev/docs/cli)
- An Ory Network project enabled for [Agent Security](https://agents.console.ory.com/)
- An Identity in that project to own Claude Code
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

The template contains Claude Code, the Ory plugin, and the example tool, but no credentials:

```sh
python e2b/deploy.py build \
  --tag ory-claude-code-release \
  --output .ory/e2b-template.json
```

`.ory/e2b-template.json` records the template name and immutable E2B build ID. Neither the E2B key nor runtime credentials are available while the template is built.

## 3. Authorize deployment creation

In the [Agent Security web app](https://agents.console.ory.com/deploy), select the target Ory Network project, open [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys), and create a Deployment API Key for the Identity that should own this Claude Code deployment. Copy the key when it is shown; it is displayed only once and is distinct from the runtime key created below.

On the machine where you run the deployment commands, provide the exact Ory Network project origin and the key. This example targets the hosted Agent Security service at `https://agents.console.ory.com`, so you do not need to configure an Agent Security origin:

```sh
export ORY_PROJECT_URL=https://example.projects.oryapis.com
export ORY_AGENT_DEPLOY_API_KEY=...

ory-agent init
```

Select these values in the interactive wizard:

- **Deployment name:** `claude-code-e2b`
- **Agent SDK:** **Claude Code (typescript)**
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
export ANTHROPIC_API_KEY=...
ory-agent deployment run \
  --manifest .ory/agent.yaml \
  --result-file .ory/deployment-result.json \
  -- python e2b/deploy.py probe --template-file .ory/e2b-template.json
```

The tool writes a random proof marker that the controller compares with Claude's final response, so returning the predictable greeting without executing `Bash` cannot pass the probe. The controller also requires the privacy-safe plugin log to contain a `tool.invoke` event for `Bash` with `node` in its shell decomposition, proving the Ory hook guarded the execution. A successful probe prints `status: "succeeded"`, the sandbox ID, and the template build reference, then kills the sandbox.

The CLI then waits for **Active** and retains the deployment for later sessions. [Deploy](https://agents.console.ory.com/deploy) shows the registered deployment and its first and last contact, while [Observe](https://agents.console.ory.com/observe) retains each session and its `Bash` events. The CLI refuses to overwrite an existing `--result-file`, so move or remove the previous result before each rerun.

The runtime receives these values from the credential stream:

```text
ORY_PROJECT_URL
ORY_AGENT_SECURITY_URL
ORY_AGENT_TYPE=claude-code
ORY_AGENT_CREDENTIAL_METHOD=talos_api_key
ORY_AGENT_API_KEY
```

Only `ORY_AGENT_API_KEY` is secret. The plugin uses it to resolve the server-owned Agent subject and root delegation at startup. It never asks a human to sign in.

## 5. Observe and enforce

`Bash` is a Claude Code tool in the `AgentTool` namespace, and `node` is a command in the `ShellTool` namespace.

1. Run the probe once with no block and confirm the Claude Code session and `Bash` activity appear in [Observe](https://agents.console.ory.com/observe).
2. Add a case-sensitive `Bash` tool block, or block `node` in the shell-command catalog, in [Enforce](https://agents.console.ory.com/manage) while the applicable posture is **Observe**. Run the probe again; the denial is recorded, but the command still runs.
3. Change the applicable posture to **Enforce**. Run the probe again; the plugin blocks the command before `greet.mjs` executes, so the probe exits unsuccessfully as expected.

Each probe starts a new Claude Code session, so its lifecycle and tool events appear together as one run.

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
| Secret   | `ANTHROPIC_API_KEY`        | Model credential injected only into the Claude Code command                                                              |

Require environment reviewers, prevent self-review, and do not expose this environment to pull requests or untrusted branches.

## Retire the deployment

- Rotate the Agent Security credential by revoking the deployment and creating a new one, then update `ORY_AGENT_DEPLOYMENT_ID` and `ORY_AGENT_CREDENTIALS`.
- When retiring Claude Code, run `ory-agent deployment revoke` and delete the E2B template. Ordinary session completion never revokes the deployment.

## Troubleshooting

- An invalid, expired, or revoked runtime key makes the plugin fail its first request, and the probe exits with an error. Revoke the old deployment and create a new one.
- If the probe fails its proof-marker or `tool.invoke` check, confirm the template was built from this example and pins the expected `@ory/claude-code` version.
- E2B rejects new work when the project has no remaining credits or exceeds its limits.
