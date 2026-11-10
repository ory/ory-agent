# Agent examples

Runnable, headless Agents that use the Ory Agent CLI and the [deployment action](../README.md#deployment-action) in this repository to deploy with [Ory Agent Security](https://agents.console.ory.com/). Each example builds a credential-free artifact, creates its deployment with `ory-agent deployment run`, runs an activation probe that exercises one guarded tool call, and includes a GitHub Actions workflow.

The workflows reference their files under `examples/`. Copy an example into your own repository at the same path, or adjust the paths in its `github-actions.yml`, and replace the pinned `ory/ory-agent@vX.Y.Z` tag with the release you have approved.

SDK examples are organized by Agent SDK catalog ID and Agent name. Harness examples use the harness catalog ID and deployment target:

```text
examples/<agent-sdk>/<agent-name>/
examples/<harness>/<runtime>/
```

An SDK example that supports more than one runtime keeps the extra runtime in its own `<runtime>/` subdirectory with its own README.

Each example covers two Ory Agent Security features:

- **Build** integrates an Ory Agent SDK adapter or harness plugin with the tool boundary and lifecycle.
- **Deploy** creates the noninteractive runtime identity and credential used by that application.

The runtime target still builds and runs the application. Agent Security does not package customer application source.

Every example is a headless Agent. It must:

- use a complete Deploy-issued runtime configuration without interactive login or a human OAuth token
- complete an LLM turn in which the model requests a guarded tool, receives its handled result, and produces the final response
- attach the SDK integration or harness plugin to the tool boundary and activity lifecycle

Runtime credentials stay in process environment variables so local deployment commands or CI workflows can inject them without changing Agent source.

## Layout

Every example uses the same structure:

```text
<example>/
  README.md                  # shared section order, below
  <Agent source>             # agent.py, agent.ts, or the harness tool
  <runtime>/deploy.*         # trusted controller: build, then probe or deploy
  <runtime>/github-actions.yml  # workflow that calls ory/ory-agent@vX.Y.Z with run
```

Each README uses these sections in this order: **Build and Deploy**, **Security boundary**, **Prerequisites**, **1. Prepare** the runtime, **2. Build** the template or image, **3. Authorize deployment creation**, **4. Run the activation probe**, **5. Observe and enforce**, **Automate with GitHub Actions**, **Retire the deployment**, and **Troubleshooting**. Runtime-specific material belongs in a subsection of the matching step. Every example uses `ory-agent init`, `ory-agent deployment validate`, and `ory-agent deployment run`, and every workflow pins the same `ory/ory-agent` action release.

## Examples

| Agent SDK                                                 | Agent         | Runtime                           | Demonstrates                                                                                                                                                                                             |
| --------------------------------------------------------- | ------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [OpenAI Agents SDK](openai-agents/hello-world/)           | `hello-world` | E2B                               | Guarded `Greet` tool, credential-free template, runtime identity, activation probe, and GitHub Actions                                                                                                   |
| [OpenAI Agents SDK](openai-agents/hello-world/exe-dev/)   | `hello-world` | exe.dev                           | Guarded `Greet` tool, digest-verified bundle, short-lived standalone VM, credentials streamed over SSH stdin, and GitHub Actions                                                                         |
| [OpenRouter Agent SDK](openrouter-agent/hello-world/)     | `hello-world` | Hosted NVIDIA OpenShell           | Guarded `Greet` tool, GHCR image, OIDC-authenticated gateway, provider-bound credentials, activation probe, and GitHub Actions                                                                           |
| [Claude Code](claude-code/e2b/)                           | `claude-code` | E2B                               | Headless Claude Code, preinstalled Ory plugin, Deploy-issued runtime identity, guarded Bash call, and GitHub Actions                                                                                     |
| [Microsoft Agent Framework](agent-framework/hello-world/) | `hello-world` | Microsoft 365 custom engine agent | Guarded `Greet` tool, Microsoft 365 Agents SDK channel, Azure Container Apps, managed-identity Azure Bot, keyless Microsoft Foundry model, Direct Line activation probe, app package, and GitHub Actions |
