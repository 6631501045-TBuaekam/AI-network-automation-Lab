#!/usr/bin/env bash
# Run before use (on the machine that runs Docker): checks settings, routers and containers.
set -u
cd "$(dirname "$0")/.."
ok()  { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
FAILED=0

echo "Config files"
[ -f .env ] && ok ".env present" || bad ".env missing (cp .env.example .env)"
[ -f inventory/devices.yaml ] && ok "inventory/devices.yaml present" || bad "inventory/devices.yaml missing"
if [ -f inventory/devices.yaml ] && grep -q "<R[0-9]*_MGMT_IP>" inventory/devices.yaml; then
  bad "inventory/devices.yaml still has <..._MGMT_IP> placeholders - put the real router IPs"
fi
if [ -f .env ]; then
  perm=$(stat -c %a .env); [ "$perm" = "600" ] && ok ".env permissions 600" || bad ".env permissions $perm (run: chmod 600 .env)"
  grep -q '^ADMIN_TOKEN=change-me' .env && bad "ADMIN_TOKEN still default"
  grep -qE '^ROUTER_SSH_PASSWORD=.+' .env && ok "router password set" || bad "ROUTER_SSH_PASSWORD empty"
  if grep -qE '^GNS3_ENABLED=true' .env && ! grep -qE '^GNS3_URL=.+' .env; then
    bad "GNS3_ENABLED=true but GNS3_URL is empty"
  fi
  prov=$(grep -E '^AI_PROVIDER=' .env | cut -d= -f2 | tr -d '\r')
  prov=${prov:-openai}   # same default as the backend (Google Gemini)
  case "$prov" in anthropic|openai|local) ok "AI provider: $prov" ;; *) bad "AI_PROVIDER must be openai, anthropic or local (got '$prov')" ;; esac
  [ "$prov" = "openai" ] && { grep -qE '^OPENAI_API_KEY=.+' .env && ok "OPENAI_API_KEY set" || bad "OPENAI_API_KEY empty (needs a Gemini key)"; }
  [ "$prov" = "anthropic" ] && { grep -qE '^ANTHROPIC_API_KEY=.+' .env && ok "ANTHROPIC_API_KEY set" || bad "ANTHROPIC_API_KEY empty"; }
fi

echo "Router management reachability (SSH port 22 from this machine)"
if [ -f inventory/devices.yaml ]; then
  for host in $(grep -E '^\s+host:' inventory/devices.yaml | awk '{print $2}'); do
    timeout 4 bash -c "</dev/tcp/$host/22" 2>/dev/null && ok "$host:22 reachable" || bad "$host:22 NOT reachable"
  done
fi

echo "Docker stack"
docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null | while read -r s st h; do
  [ "$st" = "running" ] && ok "$s $st $h" || echo "  ...  $s $st"
done
curl -fsS http://localhost:8000/api/health >/dev/null && ok "backend API answers on :8000" || bad "backend API not answering"
exit $FAILED
