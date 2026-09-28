# Ory Agent

Public release artifacts and the deployment action for the Ory Agent CLI.

## Deployment action

The action installs the CLI matching its exact release tag, verifies the signed release checksum manifest, validates `.ory/agent.yaml`, and creates or reuses an Agent Security deployment.

```yaml
- id: ory-agent
  uses: ory/ory-agent@v1.3.0
  with:
    api-key: ${{ secrets.ORY_AGENT_DEPLOY_API_KEY }}
    project-url: ${{ vars.ORY_PROJECT_URL }}
    agent-security-url: ${{ vars.ORY_AGENT_SECURITY_URL }}
    deployment-id: ${{ vars.ORY_AGENT_DEPLOYMENT_ID }}

- name: Deploy runtime
  env:
    CREATED: ${{ steps.ory-agent.outputs.created }}
    CREDENTIAL_FILE: ${{ steps.ory-agent.outputs.credential-file }}
  run: |
    if [[ "${CREATED}" == "true" ]]; then
      ORY_AGENT_CREDENTIAL_FILE="${CREDENTIAL_FILE}" ./scripts/deploy-agent.sh
    else
      ./scripts/deploy-agent.sh
    fi
```

Omit `deployment-id` only when creating the deployment for the first time. Persist the resulting non-secret `deployment-id` output before the next run. A reused deployment does not reissue its one-time runtime credential, so the deployment command must reuse the credential already stored by the target platform when `created` is `false`.

The deployment API key is sent only to the configured Agent Security origin. The action never outputs secret contents or implicitly replaces or revokes a deployment.
