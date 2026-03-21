#!/usr/bin/env bash
# =============================================================================
# deploy_bare.sh
# Direct bare-metal Odoo installer for Ubuntu 22.04 / 24.04 servers.
#
# Works with:  DigitalOcean Droplets  (default SSH user: ubuntu)
#              AWS EC2 Ubuntu AMIs    (default SSH user: ubuntu)
#              Any SSH-accessible VPS / PYOS server in DafeApp
#
# Usage:
#   ./scripts/deploy_bare.sh [OPTIONS]
#
# Options (all can also be set via environment variables):
#   -i, --ip         IP          Server IP address          [required]
#   -u, --user       USER        SSH username               [default: ubuntu]
#   -k, --key        PATH        Path to SSH private key    [default: ~/.ssh/id_rsa]
#   -v, --version    VERSION     Odoo major version: 17|18|19  [default: 19]
#   -p, --port       PORT        Odoo HTTP port             [default: 8069]
#   -d, --domain     DOMAIN      FQDN for Nginx + SSL       [optional]
#   -e, --email      EMAIL       Admin e-mail for certbot   [optional, required with --domain]
#   --enterprise                 Install Odoo Enterprise    [default: Community]
#   --local                      Run the installer on this machine instead of SSH
#   --fresh                      Remove stale local Odoo host state before install
#   -h, --help                   Show this help message
#
# Examples:
#   # Minimal – Community, no Nginx, no SSL
#   ./scripts/deploy_bare.sh --ip 165.22.100.50 --key ~/.ssh/do_key
#
#   # Local test on this machine (no Docker)
#   ./scripts/deploy_bare.sh --local --version 19 --port 8069
#
#   # Fresh local test from a clean slate
#   ./scripts/deploy_bare.sh --local --fresh --version 19 --port 8069
#
#   # DigitalOcean with Nginx + SSL
#   ./scripts/deploy_bare.sh \
#     --ip 165.22.100.50 --user ubuntu --key ~/.ssh/do_key \
#     --version 19 --domain odoo.example.com --email admin@example.com
#
#   # AWS EC2 with default ubuntu user
#   ./scripts/deploy_bare.sh \
#     --ip 54.10.20.30 --key ~/.ssh/aws_key.pem \
#     --version 18 --port 8069 --domain odoo.mycompany.com --email me@mycompany.com
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# INSTALL_SCRIPT is resolved after ODOO_VERSION is known (see validation section below)

# ---------------------------------------------------------------------------
# Defaults (can be overridden by env vars or CLI flags)
# ---------------------------------------------------------------------------
IP="${DEPLOY_IP:-}"
SSH_USER="${DEPLOY_USER:-ubuntu}"
KEY_PATH="${DEPLOY_KEY:-${HOME}/.ssh/id_rsa}"
ODOO_VERSION="${DEPLOY_ODOO_VERSION:-19}"
OE_PORT="${DEPLOY_PORT:-8069}"
DOMAIN="${DEPLOY_DOMAIN:-}"
ADMIN_EMAIL="${DEPLOY_ADMIN_EMAIL:-odoo@example.com}"
IS_ENTERPRISE="${DEPLOY_ENTERPRISE:-False}"
LOCAL_MODE="${DEPLOY_LOCAL:-False}"
FRESH_MODE="${DEPLOY_FRESH:-False}"
REMOTE_DIR="/opt/dafeapp-install"
SSH_TIMEOUT=30

# ---------------------------------------------------------------------------
# Parse CLI arguments
# ---------------------------------------------------------------------------
print_usage() {
  sed -n '/^# Usage:/,/^# =====$/p' "$0" | head -n 40
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--ip)        IP="$2";              shift 2 ;;
    -u|--user)      SSH_USER="$2";        shift 2 ;;
    -k|--key)       KEY_PATH="$2";        shift 2 ;;
    -v|--version)   ODOO_VERSION="$2";    shift 2 ;;
    -p|--port)      OE_PORT="$2";         shift 2 ;;
    -d|--domain)    DOMAIN="$2";          shift 2 ;;
    -e|--email)     ADMIN_EMAIL="$2";     shift 2 ;;
    --enterprise)   IS_ENTERPRISE="True"; shift   ;;
    --local)        LOCAL_MODE="True";    shift   ;;
    --fresh)        FRESH_MODE="True";    shift   ;;
    -h|--help)      print_usage; exit 0  ;;
    *) echo "[error] Unknown option: $1"; print_usage; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [[ "${LOCAL_MODE}" == "True" ]]; then
  IP="${IP:-127.0.0.1}"
  if [[ "${FRESH_MODE}" == "True" ]]; then
    echo "[clean] Removing stale local Odoo host state..."
    sudo rm -rf -- /odoo /etc/odoo-server.conf /etc/init.d/odoo-server /var/log/odoo
    echo "[clean] Local Odoo host state removed."
  fi
else
  if [[ "${FRESH_MODE}" == "True" ]]; then
    echo "[warn] --fresh is only supported with --local. Ignoring."
  fi
  if [[ -z "${IP}" ]]; then
    echo "[error] Server IP is required. Use --ip <IP> or set DEPLOY_IP."
    exit 1
  fi

  if [[ ! -f "${KEY_PATH}" ]]; then
    echo "[error] SSH key not found: ${KEY_PATH}"
    echo "        Use --key <PATH> or set DEPLOY_KEY."
    exit 1
  fi
fi

if [[ "${ODOO_VERSION}" != "17" && "${ODOO_VERSION}" != "18" && "${ODOO_VERSION}" != "19" ]]; then
  echo "[error] Unsupported Odoo version '${ODOO_VERSION}'. Use 17, 18, or 19."
  exit 1
fi

# Resolve the version-specific install script
INSTALL_SCRIPT="${SCRIPT_DIR}/installscript/${ODOO_VERSION}/odoo_install.sh"

if [[ ! -f "${INSTALL_SCRIPT}" ]]; then
  echo "[error] Install script not found: ${INSTALL_SCRIPT}"
  echo "        Expected: scripts/installscript/${ODOO_VERSION}/odoo_install.sh"
  exit 1
fi

# Derive Nginx / SSL flags from domain
if [[ -n "${DOMAIN}" ]]; then
  INSTALL_NGINX="True"
  if [[ "${ADMIN_EMAIL}" != "odoo@example.com" ]]; then
    ENABLE_SSL="True"
  else
    ENABLE_SSL="False"
    echo "[warn] Domain set but ADMIN_EMAIL is still the placeholder."
    echo "       SSL (certbot) will be skipped. Pass --email to enable it."
  fi
else
  INSTALL_NGINX="False"
  ENABLE_SSL="False"
  DOMAIN="_"
fi

# ---------------------------------------------------------------------------
# SSH helper – shared options
# ---------------------------------------------------------------------------
SSH_OPTS=(
  -i "${KEY_PATH}"
  -o StrictHostKeyChecking=no
  -o ConnectTimeout="${SSH_TIMEOUT}"
  -o BatchMode=yes
)

ssh_cmd() { ssh "${SSH_OPTS[@]}" "${SSH_USER}@${IP}" "$@"; }
scp_cmd() { scp "${SSH_OPTS[@]}" "$@"; }

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo "============================================================"
echo "  DafeApp — Odoo Bare-Metal Installer"
echo "============================================================"
if [[ "${LOCAL_MODE}" == "True" ]]; then
  echo "  Target       : local machine"
else
  echo "  Target       : ${SSH_USER}@${IP}"
  echo "  SSH key      : ${KEY_PATH}"
fi
echo "  Odoo version : ${ODOO_VERSION}"
echo "  Port         : ${OE_PORT}"
echo "  Nginx        : ${INSTALL_NGINX}"
echo "  SSL/certbot  : ${ENABLE_SSL}"
echo "  Domain       : ${DOMAIN}"
echo "  Admin email  : ${ADMIN_EMAIL}"
echo "  Enterprise   : ${IS_ENTERPRISE}"
echo "============================================================"

# ---------------------------------------------------------------------------
# Step 1: Verify SSH connectivity
# ---------------------------------------------------------------------------
echo ""
echo "[1/5] Verifying SSH connectivity..."
if [[ "${LOCAL_MODE}" == "True" ]]; then
  echo "      Local mode selected. Skipping SSH connectivity check."
else
  if ! ssh_cmd "echo dafeapp-ok" | grep -q dafeapp-ok; then
    echo "[error] Cannot reach ${IP} via SSH."
    echo "        Check the IP, SSH user, and key path."
    exit 1
  fi
  echo "      SSH OK."
fi

# ---------------------------------------------------------------------------
# Step 2: Check Ubuntu version
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] Checking Ubuntu version..."
if [[ "${LOCAL_MODE}" == "True" ]]; then
  UBUNTU_VER=$(lsb_release -rs 2>/dev/null || echo unknown)
else
  UBUNTU_VER=$(ssh_cmd "lsb_release -rs 2>/dev/null || echo unknown")
fi
if [[ "${UBUNTU_VER}" != "22.04" && "${UBUNTU_VER}" != "24.04" ]]; then
  echo "[warn] Detected Ubuntu ${UBUNTU_VER}. Script targets 22.04/24.04."
  echo "       Continuing anyway – results may vary."
else
  echo "      Ubuntu ${UBUNTU_VER} detected. OK."
fi

# ---------------------------------------------------------------------------
# Step 3: Create a patched copy of odoo_install.sh with our variables
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] Preparing patched install script..."
PATCHED_SCRIPT=$(mktemp /tmp/odoo_install_patched.XXXXXX.sh)
trap 'rm -f "${PATCHED_SCRIPT}"' EXIT

cp "${INSTALL_SCRIPT}" "${PATCHED_SCRIPT}"

# Patch the configurable variables at the top of the script
sed -i "s|^OE_VERSION=\"[^\"]*\"|OE_VERSION=\"${ODOO_VERSION}.0\"|"     "${PATCHED_SCRIPT}"
sed -i "s|^OE_PORT=\"[^\"]*\"|OE_PORT=\"${OE_PORT}\"|"                  "${PATCHED_SCRIPT}"
sed -i "s|^INSTALL_NGINX=\"[^\"]*\"|INSTALL_NGINX=\"${INSTALL_NGINX}\"| " "${PATCHED_SCRIPT}"
sed -i "s|^ENABLE_SSL=\"[^\"]*\"|ENABLE_SSL=\"${ENABLE_SSL}\"|"          "${PATCHED_SCRIPT}"
sed -i "s|^ADMIN_EMAIL=\"[^\"]*\"|ADMIN_EMAIL=\"${ADMIN_EMAIL}\"|"       "${PATCHED_SCRIPT}"
sed -i "s|^WEBSITE_NAME=\"[^\"]*\"|WEBSITE_NAME=\"${DOMAIN}\"|"         "${PATCHED_SCRIPT}"
sed -i "s|^IS_ENTERPRISE=\"[^\"]*\"|IS_ENTERPRISE=\"${IS_ENTERPRISE}\"|" "${PATCHED_SCRIPT}"

echo "      Script patched OK."

# ---------------------------------------------------------------------------
# Step 4: Stage the patched script on the target host
# ---------------------------------------------------------------------------
echo ""
if [[ "${LOCAL_MODE}" == "True" ]]; then
  echo "[4/5] Staging installer on the local machine..."
else
  echo "[4/5] Uploading installer to ${IP}:${REMOTE_DIR}/..."
fi
if [[ "${LOCAL_MODE}" == "True" ]]; then
  echo "      Local mode selected. Skipping upload."
else
  ssh_cmd "sudo mkdir -p ${REMOTE_DIR} && sudo chmod 755 ${REMOTE_DIR}"
  scp_cmd "${PATCHED_SCRIPT}" "${SSH_USER}@${IP}:${REMOTE_DIR}/odoo_install.sh"
  ssh_cmd "sudo chmod +x ${REMOTE_DIR}/odoo_install.sh"
  echo "      Upload OK."
fi

# ---------------------------------------------------------------------------
# Step 5: Execute the installer on the target host
#   - Runs directly on the target host
#   - Remote mode streams output through SSH
# ---------------------------------------------------------------------------
echo ""
if [[ "${LOCAL_MODE}" == "True" ]]; then
  echo "[5/5] Running Odoo installer locally..."
else
  echo "[5/5] Running Odoo installer on remote server..."
fi
echo "      This typically takes 5–15 minutes depending on network speed."
echo "      Output is streamed below."
echo "------------------------------------------------------------"

if [[ "${LOCAL_MODE}" == "True" ]]; then
  sudo bash "${PATCHED_SCRIPT}" 2>&1
else
  ssh_cmd "sudo bash ${REMOTE_DIR}/odoo_install.sh 2>&1"
fi

echo "------------------------------------------------------------"
echo ""

# ---------------------------------------------------------------------------
# Post-install summary
# ---------------------------------------------------------------------------
echo "[done] Fetching post-install summary..."
if [[ "${LOCAL_MODE}" == "True" ]]; then
  ADMIN_PASS=$(sudo grep 'admin_passwd' /etc/odoo-server.conf 2>/dev/null || echo '(not found)')
  SERVICE_STATUS=$(sudo service odoo-server status 2>/dev/null | head -5 || echo '(status unavailable)')
else
  ADMIN_PASS=$(ssh_cmd "sudo grep 'admin_passwd' /etc/odoo-server.conf 2>/dev/null || echo '(not found)'")
  SERVICE_STATUS=$(ssh_cmd "sudo service odoo-server status 2>/dev/null | head -5 || echo '(status unavailable)'")
fi

echo ""
echo "============================================================"
echo "  Odoo ${ODOO_VERSION} installation complete!"
echo "============================================================"
echo "  Server IP     : ${IP}"
echo "  Odoo port     : ${OE_PORT}"
echo "  Config file   : /etc/odoo-server.conf"
echo "  Log file      : /var/log/odoo/odoo-server.log"
echo "  ${ADMIN_PASS}"
if [[ "${INSTALL_NGINX}" == "True" ]]; then
  echo "  Nginx site    : /etc/nginx/sites-available/${DOMAIN}"
fi
if [[ "${ENABLE_SSL}" == "True" ]]; then
  echo "  SSL           : enabled (Let's Encrypt)"
fi
echo ""
echo "  Service management:"
echo "    sudo service odoo-server start"
echo "    sudo service odoo-server stop"
echo "    sudo service odoo-server restart"
echo ""
echo "  Open Odoo:    http://${IP}:${OE_PORT}"
if [[ -n "${DOMAIN}" && "${DOMAIN}" != "_" ]]; then
  PROTO="http"
  [[ "${ENABLE_SSL}" == "True" ]] && PROTO="https"
  echo "             ${PROTO}://${DOMAIN}"
fi
echo "============================================================"
echo ""
echo "  Service status:"
echo "${SERVICE_STATUS}"
echo "============================================================"
