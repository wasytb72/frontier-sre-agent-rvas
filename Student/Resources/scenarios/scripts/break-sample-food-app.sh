#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 [break|restore] --yes"
  echo "Lab only: temporarily route API ingress to port 1, or restore its saved target port."
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

action="${1:-}"
if [[ $# -ne 2 || "${2:-}" != "--yes" || ( "${action}" != "break" && "${action}" != "restore" ) ]]; then
  usage >&2
  exit 1
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
require_command curl

resource_group="$(rvas_resource_group_name)"
api_app_name="$(tf_output sample_food_api_container_app_name)"
app="$(az containerapp show --name "${api_app_name}" --resource-group "${resource_group}" --output json)"
resource_id="$(jq -er '.id' <<< "${app}")"
original_port_tag="rvas-food-original-target-port"
saved_port="$(jq -r --arg tag "${original_port_tag}" '.tags[$tag] // empty' <<< "${app}")"
current_port="$(jq -er '.properties.configuration.ingress.targetPort' <<< "${app}")"
if ! fqdn="$(jq -er '.properties.configuration.ingress | select(.external == true and .transport != "tcp") | .fqdn | select(length > 0)' <<< "${app}")"; then
  echo "The lab API must have external HTTP ingress with a valid FQDN." >&2
  exit 1
fi
api_url="https://${fqdn}"
fault_port=1

valid_port() {
  [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && (( $1 <= 65535 && $1 != fault_port ))
}

http_code() {
  local code
  if code="$(curl --max-time 10 --silent --show-error --output /dev/null \
    --write-out "%{http_code}" "${api_url}$1")"; then
    printf '%s' "${code}"
  else
    echo "HTTP probe failed for ${api_url}$1; transport failure is not outage evidence." >&2
    printf '000'
  fi
}

verify_http() {
  local expected="$1" attempts="$2" consecutive=0 attempt weather food
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    weather="$(http_code /WeatherForecast)"
    food="$(http_code /api/FoodItems)"
    echo "Probe ${attempt}/${attempts}: WeatherForecast HTTP ${weather}; FoodItems HTTP ${food}"
    if [[ "${weather}" =~ ${expected} && "${food}" =~ ${expected} ]]; then
      consecutive=$((consecutive + 1))
      if (( consecutive >= 3 )); then
        return 0
      fi
    else
      consecutive=0
    fi
    if (( attempt < attempts )); then
      sleep 5
    fi
  done
  echo "API did not reach the required HTTP state on both routes for three consecutive probes." >&2
  return 1
}

echo "Lab API: ${api_app_name} in ${resource_group} (${api_url})"
echo "Recovery: bash ${BASH_SOURCE[0]} restore --yes"

if [[ "${action}" == "break" ]]; then
  if [[ -n "${saved_port}" ]]; then
    echo "A saved fault state already exists. Run restore --yes before injecting another fault." >&2
    exit 1
  fi
  if ! valid_port "${current_port}"; then
    echo "Refusing to inject a fault: invalid or already faulted target port ${current_port}." >&2
    exit 1
  fi
  verify_http '^2[0-9]{2}$' 3
  az tag update --resource-id "${resource_id}" --operation Merge \
    --tags "${original_port_tag}=${current_port}" --output none
  target_port="${fault_port}"
else
  if ! valid_port "${saved_port}"; then
    echo "No valid saved target port found in tag ${original_port_tag}; refusing to guess." >&2
    exit 1
  fi
  if [[ "${current_port}" != "${fault_port}" && "${current_port}" != "${saved_port}" ]]; then
    echo "Target port changed outside this scenario (${current_port}); refusing to overwrite it." >&2
    exit 1
  fi
  target_port="${saved_port}"
fi

trap 'echo "Scenario failed; saved recovery state is retained. Run restore --yes after resolving the error." >&2' ERR
az containerapp ingress update --name "${api_app_name}" --resource-group "${resource_group}" \
  --target-port "${target_port}" --output none

if [[ "${action}" == "break" ]]; then
  verify_http '^5[0-9]{2}$' 24
  echo "Verified ingress outage. The API image and application code are unchanged."
else
  verify_http '^2[0-9]{2}$' 24
  az tag update --resource-id "${resource_id}" --operation Delete \
    --tags "${original_port_tag}=${saved_port}" --output none
  echo "Verified recovery; original ingress target port ${saved_port} restored."
fi