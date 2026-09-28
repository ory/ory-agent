#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ -n "${ORY_AGENT_DEPLOY_API_KEY:-}" && "${ORY_AGENT_DEPLOY_API_KEY}" != *$'\n'* && "${ORY_AGENT_DEPLOY_API_KEY}" != *$'\r'* ]] || {
  echo "::error::api-key must be a nonempty single-line value."
  exit 1
}
echo "::add-mask::${ORY_AGENT_DEPLOY_API_KEY}"

manifest="${MANIFEST:-.ory/agent.yaml}"
credential_file="${REQUESTED_CREDENTIAL_FILE:-${RUNNER_TEMP:?RUNNER_TEMP is required}/ory-agent-action/ory-agent.env}"
[[ "${credential_file}" != *$'\n'* && "${credential_file}" != *$'\r'* ]] || {
  echo "::error::credential-file must be a single-line path."
  exit 1
}
state_file="${RUNNER_TEMP}/ory-agent-action/deployment.json"
result_file="${RUNNER_TEMP}/ory-agent-action/result.json"
mkdir -p "$(dirname "${credential_file}")" "$(dirname "${state_file}")"
[[ ! -e "${credential_file}" ]] || {
  echo "::error::credential-file already exists; refusing to overwrite it."
  exit 1
}
rm -f -- "${result_file}"

ory-agent validate --manifest "${manifest}"
args=(deployment ensure --manifest "${manifest}" --state-file "${state_file}" --credential-file "${credential_file}")
if [[ -n "${DEPLOYMENT_ID:-}" ]]; then
  args+=(--deployment-id "${DEPLOYMENT_ID}")
fi
ory-agent "${args[@]}" > "${result_file}"

actual_id="$(jq -er '.deployment_id' "${result_file}")"
created="$(jq -r '.created' "${result_file}")"
if [[ -n "${DEPLOYMENT_ID:-}" && "${actual_id}" != "${DEPLOYMENT_ID}" ]]; then
  echo "::error::Agent Security returned a different deployment ID."
  exit 1
fi
if [[ "${created}" == "true" ]]; then
  [[ -s "${credential_file}" ]] || {
    echo "::error::A new deployment did not produce its runtime credential file."
    exit 1
  }
  chmod 0600 "${credential_file}"
else
  [[ "${created}" == "false" ]] || {
    echo "::error::Ory Agent CLI returned an invalid created value."
    exit 1
  }
  [[ ! -e "${credential_file}" ]] || {
    echo "::error::A reused deployment unexpectedly produced credential material."
    exit 1
  }
fi

printf 'deployment-id=%s\n' "${actual_id}" >> "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
printf 'created=%s\n' "${created}" >> "${GITHUB_OUTPUT}"
printf 'credential-file=%s\n' "${credential_file}" >> "${GITHUB_OUTPUT}"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf "### Ory Agent deployment\n\n- Deployment ID: \`%s\`\n- Created: \`%s\`\n" "${actual_id}" "${created}" >> "${GITHUB_STEP_SUMMARY}"
fi
rm -f -- "${result_file}"
