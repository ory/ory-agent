#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ -n "${ORY_AGENT_DEPLOY_API_KEY:-}" && "${ORY_AGENT_DEPLOY_API_KEY}" != *$'\n'* && "${ORY_AGENT_DEPLOY_API_KEY}" != *$'\r'* ]] || {
  echo "::error::api-key must be a nonempty single-line value."
  exit 1
}
echo "::add-mask::${ORY_AGENT_DEPLOY_API_KEY}"

manifest="${MANIFEST:-.ory/agent.yaml}"
action_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/ory-agent-action"
result_file="${action_dir}/result.json"
mkdir -p "${action_dir}"
rm -f -- "${result_file}"
trap 'rm -f -- "${result_file}"' EXIT

if [[ -n "${RUN_COMMAND:-}" ]]; then
  [[ -z "${DEPLOYMENT_ID:-}" ]] || {
    echo "::error::deployment-id cannot be combined with run."
    exit 1
  }
  [[ -z "${REQUESTED_CREDENTIAL_FILE:-}" ]] || {
    echo "::error::credential-file cannot be combined with run."
    exit 1
  }
  ory-agent validate --manifest "${manifest}"
  ory-agent deployment run \
    --manifest "${manifest}" \
    --result-file "${result_file}" \
    -- bash -euo pipefail -c "${RUN_COMMAND}"

  actual_id="$(jq -er '.deployment_id' "${result_file}")"
  printf 'deployment-id=%s\n' "${actual_id}" >> "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
  printf 'created=true\n' >> "${GITHUB_OUTPUT}"
  printf 'credential-file=\n' >> "${GITHUB_OUTPUT}"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf "### Ory Agent deployment\n\n- Deployment ID: \`%s\`\n- Ephemeral: \`true\`\n- Revoked: \`true\`\n" "${actual_id}" >> "${GITHUB_STEP_SUMMARY}"
  fi
  exit 0
fi

credential_file="${REQUESTED_CREDENTIAL_FILE:-${action_dir}/ory-agent.env}"
[[ "${credential_file}" != *$'\n'* && "${credential_file}" != *$'\r'* ]] || {
  echo "::error::credential-file must be a single-line path."
  exit 1
}
state_file="${action_dir}/deployment.json"
mkdir -p "$(dirname "${credential_file}")" "$(dirname "${state_file}")"
[[ ! -e "${credential_file}" ]] || {
  echo "::error::credential-file already exists; refusing to overwrite it."
  exit 1
}
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
