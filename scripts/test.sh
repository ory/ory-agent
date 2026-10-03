#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "${tmp}"' EXIT
mkdir -p "${tmp}/bin"

cat > "${tmp}/bin/ory-agent" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == validate ]]; then
  exit 0
fi
if [[ "${1:-}" == deployment && "${2:-}" == run ]]; then
  shift 2
  result_file=""
  credential_file=""
  deployment_id=""
  while (($#)); do
    case "$1" in
      --result-file) result_file="$2"; shift 2 ;;
      --credential-file) credential_file="$2"; shift 2 ;;
      --deployment-id) deployment_id="$2"; shift 2 ;;
      --) shift; break ;;
      *) shift ;;
    esac
  done
  if [[ -z "${deployment_id}" ]]; then
    deployment_id="new-session-deployment-id"
    printf 'ORY_AGENT_API_KEY=runtime-secret\n' > "${credential_file}"
    created=true
  else
    created=false
  fi
  printf '{"created":%s,"deployment_id":"%s"}\n' "${created}" "${deployment_id}" > "${result_file}"
  "$@" < "${credential_file}"
  exit 0
fi
[[ "${1:-}" == deployment && "${2:-}" == ensure ]]
shift 2
credential_file=""
deployment_id=""
while (($#)); do
  case "$1" in
    --credential-file) credential_file="$2"; shift 2 ;;
    --deployment-id) deployment_id="$2"; shift 2 ;;
    *) shift ;;
  esac
done
deployment_id="${FAKE_ACTUAL_ID:-${deployment_id:-new-deployment-id}}"
if [[ "${FAKE_CREATED}" == true ]]; then
  printf 'ORY_AGENT_API_KEY=runtime-secret\n' > "${credential_file}"
fi
printf '{"created":%s,"deployment_id":"%s"}\n' "${FAKE_CREATED}" "${deployment_id}"
EOF
chmod +x "${tmp}/bin/ory-agent"

run_deploy() {
  PATH="${tmp}/bin:${PATH}" \
    RUNNER_TEMP="${tmp}/runner" \
    GITHUB_OUTPUT="${tmp}/output" \
    ORY_AGENT_DEPLOY_API_KEY="deployment-secret" \
    ORY_PROJECT_URL="https://project.example.test" \
    ORY_AGENT_SECURITY_URL="https://agents.example.test" \
    MANIFEST=".ory/agent.yaml" \
    REQUESTED_CREDENTIAL_FILE="${tmp}/credential.env" \
    DEPLOYMENT_ID="${1:-}" \
    FAKE_CREATED="$2" \
    FAKE_ACTUAL_ID="${3:-}" \
    "${root}/scripts/deploy.sh"
}

: > "${tmp}/output"
run_deploy "" true ""
grep -Fxq 'deployment-id=new-deployment-id' "${tmp}/output"
grep -Fxq 'created=true' "${tmp}/output"
grep -Fxq "credential-file=${tmp}/credential.env" "${tmp}/output"
if [[ "$(uname -s)" == Darwin ]]; then
  mode="$(stat -f '%Lp' "${tmp}/credential.env")"
else
  mode="$(stat -c '%a' "${tmp}/credential.env")"
fi
[[ "${mode}" == 600 ]]
if grep -Fq 'deployment-secret' "${tmp}/output"; then
  echo "deployment API key leaked into action outputs" >&2
  exit 1
fi
rm -f -- "${tmp}/credential.env"

: > "${tmp}/output"
run_deploy existing-deployment-id false ""
grep -Fxq 'deployment-id=existing-deployment-id' "${tmp}/output"
grep -Fxq 'created=false' "${tmp}/output"
[[ ! -e "${tmp}/credential.env" ]]

if run_deploy expected-deployment-id false different-deployment-id >/dev/null 2>&1; then
  echo "deploy.sh accepted a mismatched deployment ID" >&2
  exit 1
fi

: > "${tmp}/output"
PATH="${tmp}/bin:${PATH}" \
  RUNNER_TEMP="${tmp}/runner" \
  GITHUB_OUTPUT="${tmp}/output" \
  ORY_AGENT_DEPLOY_API_KEY="deployment-secret" \
  ORY_PROJECT_URL="https://project.example.test" \
  ORY_AGENT_SECURITY_URL="https://agents.example.test" \
  MANIFEST=".ory/agent.yaml" \
  RUN_COMMAND="read -r credential; [[ \"\${credential}\" == \"ORY_AGENT_API_KEY=runtime-secret\" ]]" \
  "${root}/scripts/deploy.sh"
grep -Fxq 'deployment-id=new-session-deployment-id' "${tmp}/output"
grep -Fxq 'created=true' "${tmp}/output"
grep -Fxq "credential-file=${tmp}/runner/ory-agent-action/ory-agent.env" "${tmp}/output"
[[ ! -e "${tmp}/runner/ory-agent-action/result.json" ]]

: > "${tmp}/output"
printf 'ORY_AGENT_API_KEY=runtime-secret\n' > "${tmp}/credential.env"
# The nested bash launched by deploy.sh expands the credential expression.
# shellcheck disable=SC2016
PATH="${tmp}/bin:${PATH}" \
  RUNNER_TEMP="${tmp}/runner" \
  GITHUB_OUTPUT="${tmp}/output" \
  ORY_AGENT_DEPLOY_API_KEY="deployment-secret" \
  ORY_PROJECT_URL="https://project.example.test" \
  REQUESTED_CREDENTIAL_FILE="${tmp}/credential.env" \
  RUN_COMMAND='read -r credential; [[ "${credential}" == "ORY_AGENT_API_KEY=runtime-secret" ]]' \
  DEPLOYMENT_ID="existing-deployment-id" \
  "${root}/scripts/deploy.sh"
grep -Fxq 'deployment-id=existing-deployment-id' "${tmp}/output"
grep -Fxq 'created=false' "${tmp}/output"
