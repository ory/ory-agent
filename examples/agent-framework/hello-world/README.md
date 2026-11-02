# Microsoft Agent Framework hello world on Microsoft 365

This example protects one Microsoft Agent Framework function tool, `Greet`, with [Ory Agent Security](https://agents.console.ory.com/) and publishes the Agent to Microsoft 365 Copilot and Teams as a [custom engine agent](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/create-deploy-agents-sdk). Its model is served by a [Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/) project, and every Microsoft credential is a managed identity, so the only secret the Agent receives is its Agent Security runtime key.

## Build and Deploy

Ory Agent Security separates application integration from runtime identity:

- **Build** shows how to add Agent Security to application code. In this example, [`agent.py`](agent.py) creates one `OryAgentClient` and registers Ory function middleware on the Agent, so every function call is gated before it runs. [`app.py`](app.py) uses the Microsoft 365 Agents SDK only as the channel layer: it receives Microsoft 365 activities and returns the Agent's response.
- **Deploy** creates the noninteractive identity and credential used by the running Agent. The generated `.ory/agent.yaml` selects the `agent-framework` Agent type, the `microsoft-365-custom-engine` runtime and recipe, and a Talos API key.
- **Azure and Microsoft 365** build, host, and publish the application, and Microsoft Foundry serves its model. Agent Security does not create the image, Azure resources, Foundry project, bot registration, Entra identity, or Microsoft 365 app package.

The first authenticated Agent request changes the Agent Security deployment from `ready` to `active`.

```text
Microsoft 365 Copilot / Teams ──► Azure Bot ──► Container App: app.py ──► agent.py ──► Ory middleware ──► Greet
                                  (managed       (Agents SDK channel)     (Agent        (gate, observe)
                                   identity)                               Framework)
                                                                              │
                                                                              ▼  managed identity
                                                                         Microsoft Foundry project
                                                                         (model deployment)
```

[`microsoft-365/main.bicep`](microsoft-365/main.bicep) creates:

- a user-assigned managed identity that is both the bot's Microsoft identity and the Container App's ACR pull identity
- an Azure Container Apps environment, Log Analytics workspace, and one always-on Container App
- an Azure Bot with the Microsoft Teams channel and a Direct Line site used only for the activation probe
- a Microsoft Foundry resource with key authentication disabled, one project, and one pinned model deployment
- a **Foundry User** (formerly Azure AI User) role assignment that lets the managed identity call models in that project only

## Security boundary

- The image contains the Agent and its dependencies, but no credentials.
- The bot and the model client both authenticate as the managed identity, so there is no Entra client secret or model API key to create, store, or rotate. In the container, `AZURE_TOKEN_CREDENTIALS=ManagedIdentityCredential` restricts `DefaultAzureCredential` to that identity.
- The Agent Security values are sent to Azure only as a `@secure()` template parameter from memory and become Container Apps secrets. They never appear on a command line, in a parameters file, in deployment history, in the image, or in the Microsoft 365 app package.
- Only function and tool calls that pass through the Agent Framework middleware in `agent.py` are observed or gated. Microsoft Graph calls, Copilot connectors, Foundry-hosted and other provider-hosted tools, and other Microsoft-native actions that do not run in this process are outside Agent Security. Add Ory middleware to every Agent that invokes protected tools, including child agents and workflows.

## Prerequisites

- Python 3.12 and the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), signed in with `az login`
- An Azure subscription with the `Microsoft.App`, `Microsoft.BotService`, `Microsoft.CognitiveServices`, `Microsoft.ContainerRegistry`, `Microsoft.ManagedIdentity`, and `Microsoft.OperationalInsights` resource providers registered
- [Model quota](https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits) in the resource group's region for `gpt-5.4-mini` version `2026-03-17`, or another chat model you select with `--model` and `--model-version`. The deployment uses Global Standard by default; pass `--model-sku DataZoneStandard` to use Data Zone Standard quota instead. Check both with `az cognitiveservices usage list --location <region>`
- Permission to create resources and role assignments in the target resource group, such as Owner, or Contributor plus Role Based Access Control Administrator
- A Microsoft 365 tenant where you can upload custom apps, with a [Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites) license to test in Copilot
- An Ory Network project enabled for [Agent Security](https://agents.console.ory.com/)
- An Identity in that project to own the Agent
- [Ory Agent CLI](https://agents.console.ory.com/guides/ory-agent-cli) 1.8.0 or later

## 1. Prepare Azure

Create one resource group and container registry for the example. Every later command deploys into this resource group:

```sh
export AZURE_RESOURCE_GROUP=ory-agent-framework-hello-world
export AZURE_CONTAINER_REGISTRY=oryagentframeworkhello  # globally unique, alphanumeric
export AZURE_NAME_PREFIX=oryhello                        # 3-16 lowercase characters

az group create --name "${AZURE_RESOURCE_GROUP}" --location eastus
az acr create --resource-group "${AZURE_RESOURCE_GROUP}" \
  --name "${AZURE_CONTAINER_REGISTRY}" --sku Basic
```

Install the controller dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --requirement microsoft-365/requirements-controller.txt
```

## 2. Build the image

ACR Tasks builds the [`Dockerfile`](Dockerfile) in Azure, so no local Docker daemon is needed. The image contains the Agent and its dependencies, but no credentials:

```sh
python microsoft-365/deploy.py build \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --registry "${AZURE_CONTAINER_REGISTRY}" \
  --tag release-1 \
  --output .ory/image.json
```

`.ory/image.json` records the immutable `@sha256:` image reference. The deployment refuses mutable tags.

### Test locally

[`test_app.py`](test_app.py) drives a real Microsoft 365 Agents SDK request through `app.py`, the Agent Framework tool loop, and the Ory middleware, using a scripted model and stubbed Ory decisions. It runs without network access or credentials:

```sh
python -m pip install --requirement requirements.txt
python -m pip install --no-deps --requirement requirements-adapter.txt
python -m unittest test_app
python -m unittest discover -s microsoft-365 -p 'test_*.py'
```

The test enables anonymous channel requests only in its own process. Deployed Agents always require an Azure Bot Service token.

## 3. Authorize deployment creation

In the [Agent Security web app](https://agents.console.ory.com/deploy), select the target Ory Network project, open [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys), and create a Deployment API Key for the Identity that should own this Agent. Copy the key when it is shown; it is displayed only once and is distinct from the runtime key created below.

On the machine where you run the deployment commands, provide the exact Ory Network project origin and the key. This example targets the hosted Agent Security service at `https://agents.console.ory.com`, so you do not need to configure an Agent Security origin:

```sh
export ORY_PROJECT_URL=https://example.projects.oryapis.com
export ORY_AGENT_DEPLOY_API_KEY=...

ory-agent init
```

Select these values in the interactive wizard:

- **Deployment name:** `agent-framework-hello-world-m365`
- **Agent SDK:** **Microsoft Agent Framework (python)**
- **Runtime:** **Microsoft 365 custom engine agent**

The runtime selection supplies the `microsoft-365-custom-engine` package recipe and its current version. `init` writes `.ory/agent.yaml`, `.ory/.gitignore`, and `.github/workflows/ory-agent-deploy.yml`, and refuses to overwrite existing files. Adapt the generated workflow from [`microsoft-365/github-actions.yml`](microsoft-365/github-actions.yml), then commit all three files.

Verify the Deployment API Key and the generated manifest without creating a runtime credential:

```sh
ory-agent auth status
ory-agent deployment validate --manifest .ory/agent.yaml
```

## 4. Run the activation probe

`deployment run` creates the deployment once and reuses it for later releases. It stores the credential in protected `.ory/credentials.env` and deployment state in `.ory/deployment.json`, launches the controller, and sends the runtime configuration only through the controller's anonymous stdin:

```sh
ory-agent deployment run \
  --manifest .ory/agent.yaml \
  --result-file .ory/deployment-result.json \
  -- python microsoft-365/deploy.py deploy \
    --resource-group "${AZURE_RESOURCE_GROUP}" \
    --registry "${AZURE_CONTAINER_REGISTRY}" \
    --name-prefix "${AZURE_NAME_PREFIX}" \
    --image-file .ory/image.json
```

The controller:

1. Validates that the received values are a complete `agent-framework` Talos deployment package.
2. Deploys `main.bicep` through Azure Resource Manager, including the Foundry project and model deployment, and stores the Agent Security values as Container Apps secrets.
3. Waits until `/healthz` reports the new revision, so the probe cannot reach its predecessor.
4. Sends one message through the bot's Direct Line channel. The message travels through Azure Bot Service and the Microsoft 365 Agents SDK token validation exactly like a Teams message, runs the Agent, and calls `Greet` through Ory.
5. Prints `status: "succeeded"` and `reply_verified: true` only if the Agent's reply contains `Hello, world!`.

Regional resources default to the resource group's region. Add `--location <region>` to place them elsewhere, for example when the resource group's region lacks Container Apps capacity or model quota. The registry can stay in its original region.

The CLI then waits for **Active** and retains the deployment for later releases. [Deploy](https://agents.console.ory.com/deploy) shows the registered deployment and its first and last contact, while [Observe](https://agents.console.ory.com/observe) retains each turn and its `Greet` events. The CLI refuses to overwrite an existing `--result-file`, so move or remove the previous result before each rerun.

The runtime receives these values from the credential stream:

```text
ORY_PROJECT_URL
ORY_AGENT_SECURITY_URL
ORY_AGENT_TYPE=agent-framework
ORY_AGENT_CREDENTIAL_METHOD=talos_api_key
ORY_AGENT_API_KEY
```

Only `ORY_AGENT_API_KEY` is secret. The SDK uses it to resolve the server-owned Agent subject and root delegation at startup. It never asks a human to sign in.

### Publish to Microsoft 365

Build the [Microsoft 365 app package](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/build-and-test/apps-package) from the deployment outputs:

```sh
outputs="$(az deployment group show \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --name ory-agent-framework-hello-world \
  --query properties.outputs --output json)"

python microsoft-365/deploy.py package \
  --bot-app-id "$(jq -r .botAppId.value <<<"${outputs}")" \
  --endpoint "$(jq -r .endpoint.value <<<"${outputs}")"
```

`microsoft-365/build/appPackage.zip` contains a manifest version 1.30 [`manifest.json`](microsoft-365/manifest.json) that declares the bot as a `customEngineAgents` entry, plus generated icons. It contains no Agent Security, OpenAI, or Microsoft credential.

Publish to a development catalog first:

1. In Teams, open **Apps > Manage your apps > Upload an app > Upload a custom app** and select `appPackage.zip`, or ask a Teams administrator to upload it to the organization catalog.
2. Open the app in Teams, or select it from the agents list in Microsoft 365 Copilot, and send `Use the Greet tool`.
3. Confirm the response and the matching `Greet` event in [Observe](https://agents.console.ory.com/observe).

Promote through your organization's app catalog only after security and administrator review. Publishing, consent, and rollback of the app package remain customer-operated.

## 5. Observe and enforce

`Greet` is an application-defined tool in the `AgentTool` namespace.

1. Run the probe, or send a message, once with no block and confirm the tool succeeds in [Observe](https://agents.console.ory.com/observe).
2. Add a case-sensitive `Greet` tool block in [Enforce](https://agents.console.ory.com/manage) while the applicable posture is **Observe**. Send another message; the denial is recorded, but the tool still runs.
3. Change the applicable posture to **Enforce**. Send another message; the middleware returns the denial to the model and `Greet` never runs.

Each message is one Agent Security session, so its lifecycle and tool events appear together as one run.

## Automate with GitHub Actions

[`microsoft-365/github-actions.yml`](microsoft-365/github-actions.yml) signs in to Azure with GitHub OIDC, builds the image with ACR Tasks, restores the durable Agent Security credential, and passes the deploy command to the pinned [`ory/ory-agent`](https://github.com/ory/ory-agent) action through `with.run`. The action installs a verified, matching `ory-agent` release, validates the deployment, delivers the credential over the controller's anonymous stdin, and verifies activation. It never exposes the credential on stdout, in an output, or in an artifact. The final step uploads the Microsoft 365 app package as a workflow artifact.

Create an Entra workload identity with a [federated credential](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust) for the protected GitHub environment and grant it the Azure permissions from [Prerequisites](#prerequisites) on the resource group. Create the deployment once as described above, then configure a protected GitHub Environment with:

| Kind     | Name                       | Purpose                                                                                                             |
| -------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Variable | `ORY_PROJECT_URL`          | Exact Ory Network project origin                                                                                    |
| Variable | `ORY_AGENT_DEPLOYMENT_ID`  | Non-secret ID of the deployment created once                                                                        |
| Variable | `AZURE_CLIENT_ID`          | Client ID of the Entra workload identity federated to this GitHub environment                                       |
| Variable | `AZURE_TENANT_ID`          | Entra tenant ID                                                                                                     |
| Variable | `AZURE_SUBSCRIPTION_ID`    | Target Azure subscription                                                                                           |
| Variable | `AZURE_RESOURCE_GROUP`     | Resource group from step 1                                                                                          |
| Variable | `AZURE_CONTAINER_REGISTRY` | Container registry name from step 1                                                                                 |
| Variable | `AZURE_NAME_PREFIX`        | Resource name prefix from step 1                                                                                    |
| Variable | `AZURE_LOCATION`           | Optional region for regional resources; defaults to the resource group's region                                     |
| Variable | `FOUNDRY_MODEL_SKU`        | Optional `GlobalStandard` (default) or `DataZoneStandard`                                                           |
| Secret   | `ORY_AGENT_DEPLOY_API_KEY` | Deployment API Key created under [**Deploy > Keys**](https://agents.console.ory.com/deploy?view=keys) for the owner |
| Secret   | `ORY_AGENT_CREDENTIALS`    | Complete protected `credentials.env` content from that deployment                                                   |

No Azure, Microsoft, or model secret is stored in GitHub. Require environment reviewers, prevent self-review, and do not expose this environment to pull requests or untrusted branches.

## Retire the deployment

- Roll back the Agent by rerunning `deploy` with an earlier `.ory/image.json`. Roll back the app package through the Teams admin center. Neither revokes the Agent Security runtime identity.
- Rotate the Agent Security credential by revoking the deployment, creating a new one, and rerunning `deploy`. Every `deploy` creates a new Container Apps revision, so the Agent always starts with the secrets it was given.
- When retiring the Agent, run `ory-agent deployment revoke`, remove the app from the Microsoft 365 catalog, and delete the resource group. Revoking one system does not revoke the others.

## Troubleshooting

- An invalid, expired, or revoked runtime key makes the Agent reply with a generic error, and `deploy` fails its reply check. The Container App logs in Log Analytics name the cause. Revoke the old deployment, create a new one, and rerun `deploy`.
- If the first deployment fails to pull the image, the new ACR role assignment may not have propagated yet. Rerun `deploy`.
- If the first activation probe fails and the Container App logs show `401` or `403` from Foundry, the new Foundry User role assignment may not have propagated yet. Rerun `deploy`.
- If the model deployment fails with a quota or availability error, request quota for the model in that region, rerun `deploy` with `--model-sku DataZoneStandard` if the subscription has Data Zone Standard quota, or select an available model with `--model` and `--model-version`.
- If the Container Apps environment fails with `ManagedEnvironmentCapacityHeavyUsageError`, or stays in `Updating` for much longer than 15 minutes, the region lacks capacity. Cancel the deployment with `az deployment group cancel`, then rerun `deploy` with `--location` set to another region and a new `--name-prefix`; existing regional resources cannot move. Delete the abandoned resources afterwards.
- Foundry resources are soft-deleted when the resource group is deleted, and their name stays reserved. Before recreating the example with the same resource group and prefix, purge it with `az cognitiveservices account purge`.
- `ory-agent-framework` 1.4.8 predates the stable Agent Framework 1.x middleware contract. `agent.py` adapts it in `ory_guard`, and the image installs the adapter without its `agent-framework` meta-package dependency, which would add every Agent Framework connector. Register `ory_function_middleware()` directly once a release that supports the stable contract is published.
