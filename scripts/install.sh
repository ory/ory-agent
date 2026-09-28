#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
install_dir="${2:-}"
[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "version must be an exact semantic version" >&2
  exit 1
}
[[ -n "${install_dir}" ]] || {
  echo "install directory is required" >&2
  exit 1
}
command -v cosign >/dev/null || {
  echo "cosign is required to verify the Ory Agent CLI release" >&2
  exit 1
}

case "${RUNNER_OS:-}" in
  Linux) os=linux ;;
  macOS) os=darwin ;;
  *)
    echo "unsupported GitHub Actions runner OS: ${RUNNER_OS:-unknown}" >&2
    exit 1
    ;;
esac
case "${RUNNER_ARCH:-}" in
  X64) arch=amd64 ;;
  ARM64) arch=arm64 ;;
  *)
    echo "unsupported GitHub Actions runner architecture: ${RUNNER_ARCH:-unknown}" >&2
    exit 1
    ;;
esac

work_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/ory-agent-action/download"
archive="ory-agent_${version}_${os}_${arch}.tar.gz"
release="https://github.com/ory/ory-agent/releases/download/v${version}"
rm -rf -- "${work_dir}"
mkdir -p "${work_dir}" "${install_dir}"

curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
  --output "${work_dir}/${archive}" "${release}/${archive}"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
  --output "${work_dir}/checksums.txt" "${release}/checksums.txt"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
  --output "${work_dir}/checksums.txt.bundle" "${release}/checksums.txt.bundle"

cosign verify-blob \
  --bundle "${work_dir}/checksums.txt.bundle" \
  --certificate-identity \
    https://github.com/ory-corp/ory-agent-plugins/.github/workflows/release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "${work_dir}/checksums.txt"

if [[ "${os}" == linux ]]; then
  grep "  ${archive}$" "${work_dir}/checksums.txt" | (cd "${work_dir}" && sha256sum --check --strict -)
else
  grep "  ${archive}$" "${work_dir}/checksums.txt" | (cd "${work_dir}" && shasum -a 256 --check -)
fi

tar -xzf "${work_dir}/${archive}" -C "${work_dir}" ory-agent
install -m 0755 "${work_dir}/ory-agent" "${install_dir}/ory-agent"
"${install_dir}/ory-agent" version
