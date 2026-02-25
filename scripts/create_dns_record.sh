#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <fqdn> <ip>"
  exit 2
fi

FQDN="$1"
IP="$2"

echo "[dns] request: ${FQDN} -> ${IP}"

# Option 1: DigitalOcean Domains API
# Required env:
#   DNS_PROVIDER=digitalocean
#   DIGITALOCEAN_TOKEN=...
if [[ "${DNS_PROVIDER:-}" == "digitalocean" ]]; then
  if [[ -z "${DIGITALOCEAN_TOKEN:-}" ]]; then
    echo "[dns] DIGITALOCEAN_TOKEN is required for digitalocean provider."
    exit 1
  fi

  ROOT_DOMAIN="${DNS_ROOT_DOMAIN:-}"
  if [[ -z "${ROOT_DOMAIN}" ]]; then
    echo "[dns] set DNS_ROOT_DOMAIN (example.com) for digitalocean provider."
    exit 1
  fi

  if [[ "${FQDN}" == "${ROOT_DOMAIN}" ]]; then
    RECORD_NAME="@"
  else
    RECORD_NAME="${FQDN%.${ROOT_DOMAIN}}"
  fi

  curl -sS -X POST "https://api.digitalocean.com/v2/domains/${ROOT_DOMAIN}/records" \
    -H "Authorization: Bearer ${DIGITALOCEAN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"A\",\"name\":\"${RECORD_NAME}\",\"data\":\"${IP}\",\"ttl\":300}" >/dev/null

  echo "[dns] DigitalOcean record created."
  exit 0
fi

# Option 2: AWS Route53
# Required env:
#   DNS_PROVIDER=route53
#   AWS_ROUTE53_ZONE_ID=...
if [[ "${DNS_PROVIDER:-}" == "route53" ]]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "[dns] aws cli not installed."
    exit 1
  fi
  if [[ -z "${AWS_ROUTE53_ZONE_ID:-}" ]]; then
    echo "[dns] AWS_ROUTE53_ZONE_ID is required for route53 provider."
    exit 1
  fi

  CHANGE_BATCH="$(mktemp)"
  cat > "${CHANGE_BATCH}" <<EOF
{
  "Comment": "DafeApp DNS create/update",
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "${FQDN}",
      "Type": "A",
      "TTL": 300,
      "ResourceRecords": [{"Value": "${IP}"}]
    }
  }]
}
EOF

  aws route53 change-resource-record-sets \
    --hosted-zone-id "${AWS_ROUTE53_ZONE_ID}" \
    --change-batch "file://${CHANGE_BATCH}" >/dev/null
  rm -f "${CHANGE_BATCH}"
  echo "[dns] Route53 record upserted."
  exit 0
fi

echo "[dns] DNS_PROVIDER not set. Skipping DNS creation (non-fatal)."
exit 0
