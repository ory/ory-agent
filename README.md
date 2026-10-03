# Ory Agent

Public release artifacts and the deployment action for the Ory Agent CLI.

## Deployment action

The action installs the CLI matching its exact release tag and verifies the signed release checksum manifest. With `run`, it creates an ephemeral Agent Security deployment, passes the runtime credential to a trusted command over stdin, verifies activation, and revokes automatically:

```yaml
- id: ory-agent
  uses: ory/ory-agent@v1.4.5
  with:
    api-key: ${{ secrets.ORY_AGENT_DEPLOY_API_KEY }}
    project-url: ${{ vars.ORY_PROJECT_URL }}
    agent-security-url: ${{ vars.ORY_AGENT_SECURITY_URL }}
    run: python e2b/deploy.py probe --template-file "${RUNNER_TEMP}/e2b-template.json"
```

The command runs under `bash -euo pipefail -c` and inherits the workflow environment, with deployment credentials removed by the CLI. Its stdin contains only the generated runtime configuration. Do not use `run` for an untrusted command or one that needs interactive stdin.

Omit `run` to create or reuse a durable deployment and receive its one-time credential file:

```yaml
- id: ory-agent
  uses: ory/ory-agent@v1.4.5
  with:
    api-key: ${{ secrets.ORY_AGENT_DEPLOY_API_KEY }}
    project-url: ${{ vars.ORY_PROJECT_URL }}
    agent-security-url: ${{ vars.ORY_AGENT_SECURITY_URL }}
    deployment-id: ${{ vars.ORY_AGENT_DEPLOYMENT_ID }}
```

Omit `deployment-id` only when creating the deployment for the first time. Persist the resulting non-secret `deployment-id` output before the next run. A reused deployment does not reissue its one-time runtime credential, so the deployment command must reuse the credential already stored by the target platform when `created` is `false`.

The deployment API key is sent only to the configured Agent Security origin. The action never outputs secret contents. Ephemeral `run` mode always revokes its new deployment; durable mode never implicitly replaces or revokes one.
