# Ory Agent

Public release artifacts and the deployment action for the Ory Agent CLI.

## Examples

The [`examples/`](examples/) directory contains runnable Agents that deploy with this action on E2B, exe.dev, NVIDIA OpenShell, and Microsoft 365.

## Deployment action

The action installs the CLI matching its exact release tag and verifies the signed release checksum manifest. With `run`, it creates an Agent Security deployment once, passes its reusable runtime credential to a trusted command over stdin, and verifies activation:

Replace `vX.Y.Z` with the immutable tag for the release you have approved.

```yaml
- id: ory-agent
  uses: ory/ory-agent@vX.Y.Z
  with:
    api-key: ${{ secrets.ORY_AGENT_DEPLOY_API_KEY }}
    project-url: ${{ vars.ORY_PROJECT_URL }}
    agent-security-url: ${{ vars.ORY_AGENT_SECURITY_URL }}
    run: python e2b/deploy.py probe --template-file "${RUNNER_TEMP}/e2b-template.json"
```

The command runs under `bash -euo pipefail -c` and inherits the workflow environment, with deployment-administration credentials removed by the CLI. Its stdin contains only the deployment runtime configuration. Do not use `run` for an untrusted command or one that needs interactive stdin. Persist the protected credential file in an external secret store and restore it together with the non-secret deployment ID before later runs.

Omit `run` to create or reuse a durable deployment and receive its one-time credential file:

```yaml
- id: ory-agent
  uses: ory/ory-agent@vX.Y.Z
  with:
    api-key: ${{ secrets.ORY_AGENT_DEPLOY_API_KEY }}
    project-url: ${{ vars.ORY_PROJECT_URL }}
    agent-security-url: ${{ vars.ORY_AGENT_SECURITY_URL }}
    deployment-id: ${{ vars.ORY_AGENT_DEPLOYMENT_ID }}
```

Omit `deployment-id` only when creating the deployment for the first time. Persist the resulting non-secret `deployment-id` output before the next run. A reused deployment does not reissue its one-time runtime credential, so the deployment command must reuse the credential already stored by the target platform when `created` is `false`.

The deployment API key is sent only to the configured Agent Security origin. The action never outputs secret contents, never creates a replacement deployment for a run, and never implicitly revokes a deployment. Use `ory-agent deployment revoke` only when retiring the registered agent and its package.
