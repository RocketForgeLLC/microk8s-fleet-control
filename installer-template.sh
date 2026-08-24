#!/usr/bin/env bash
set -Eeuo pipefail

CODE=__ENROLLMENT_CODE__
MGMT_CONSOLE=__MGMT_CONSOLE__
BUNDLE_PROXY=__BUNDLE_PROXY__
REPORT_URL=__REPORT_URL__
UPDATE_MANIFEST_URL="${REPORT_URL%/api/agent/status}/api/agent/update/manifest"
INSTALL_DIR=/usr/local/lib/microk8s-mgmt-agent
CERT_DIR=/etc/microk8s-mgmt-agent
TMP_DIR="$(mktemp -d /var/tmp/mgmt-agent.XXXXXX)"
PROXY_PID=""
NODE_NAME="$(hostname | tr '[:upper:]' '[:lower:]')"
cleanup() { [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null || true; rm -rf "$TMP_DIR"; }
trap cleanup EXIT

command -v microk8s >/dev/null || { echo "microk8s is required on this node" >&2; exit 2; }
command -v openssl >/dev/null || { echo "openssl is required on this node" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required on this node" >&2; exit 2; }

echo "[$(date -Is)] Starting management-agent enrollment for $NODE_NAME"
microk8s kubectl proxy --port=18080 --address=127.0.0.1 >/var/tmp/mgmt-agent-kubectl-proxy.log 2>&1 &
PROXY_PID=$!
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:18080/version >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:18080/version >/dev/null

install -d -m 0750 "$TMP_DIR" "$INSTALL_DIR" "$CERT_DIR" /var/lib/microk8s-mgmt-agent /var/log/microk8s-mgmt-agent
for file in microk8s-mgmt-agent.py update_manager.py microk8s-mgmt-agent.service microk8s-mgmt-agent.default; do
  curl -fsSL "$BUNDLE_PROXY/api/agent/bundle/$file" -o "$TMP_DIR/$file"
done
UPDATE_PUBLIC_KEY_URL="${REPORT_URL%/api/agent/status}/api/agent/update/public-key"
curl -fsSL "$UPDATE_PUBLIC_KEY_URL" -o "$TMP_DIR/update-public.pem" || true
install -m 0750 "$TMP_DIR/microk8s-mgmt-agent.py" "$INSTALL_DIR/microk8s-mgmt-agent.py"
install -m 0750 "$TMP_DIR/update_manager.py" "$INSTALL_DIR/update_manager.py"
if [[ -s "$TMP_DIR/update-public.pem" ]]; then
  install -m 0644 "$TMP_DIR/update-public.pem" "$CERT_DIR/update-public.pem"
fi
install -m 0644 "$TMP_DIR/microk8s-mgmt-agent.service" /etc/systemd/system/microk8s-mgmt-agent.service

openssl genrsa -out "$TMP_DIR/agent.key" 3072 2>/dev/null
openssl req -new -key "$TMP_DIR/agent.key" -out "$TMP_DIR/agent.csr" -subj "/CN=maintenance-agent:${NODE_NAME}/O=maintenance-agents"
CSR_NAME="maintenance-agent-${NODE_NAME//[^a-zA-Z0-9-]/-}-$(date +%s)"
CSR_B64="$(base64 -w0 "$TMP_DIR/agent.csr")"
cat >"$TMP_DIR/csr.yaml" <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: $CSR_NAME
spec:
  request: $CSR_B64
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: 31536000
  usages:
  - client auth
EOF
microk8s kubectl apply -f "$TMP_DIR/csr.yaml" >/dev/null
microk8s kubectl certificate approve "$CSR_NAME" >/dev/null
CERT_B64=""
for _ in $(seq 1 30); do
  CERT_B64="$(microk8s kubectl get csr "$CSR_NAME" -o jsonpath='{.status.certificate}' 2>/dev/null || true)"
  [[ -n "$CERT_B64" ]] && break
  sleep 2
done
[[ -n "$CERT_B64" ]] || { echo "Kubernetes did not issue the agent certificate" >&2; exit 1; }
printf '%s' "$CERT_B64" | base64 -d >"$TMP_DIR/agent.crt"
install -m 0600 "$TMP_DIR/agent.key" "$CERT_DIR/agent.key"
install -m 0644 "$TMP_DIR/agent.crt" "$CERT_DIR/agent.crt"
install -m 0644 /var/snap/microk8s/current/certs/ca.crt "$CERT_DIR/ca.crt"

ENROLL_RESPONSE="$(curl -fsS -X POST "$BUNDLE_PROXY/api/agent/enroll" -H "Content-Type: application/json" -H "X-Mgmt-Enrollment-Code: $CODE" --data "$(python3 -c 'import json,sys; print(json.dumps({"node":sys.argv[1]}))' "$NODE_NAME")")"
REPORT_TOKEN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["reportToken"])' <<<"$ENROLL_RESPONSE")"
install -m 0644 "$TMP_DIR/microk8s-mgmt-agent.default" /etc/default/microk8s-mgmt-agent
cat >>/etc/default/microk8s-mgmt-agent <<EOF
MGMT_AGENT_REPORT_URL=$REPORT_URL
MGMT_AGENT_REPORT_TOKEN=$REPORT_TOKEN
MGMT_AGENT_REPORT_CA=$CERT_DIR/ca.crt
MGMT_AGENT_REPORT_CLIENT_CERT=$CERT_DIR/agent.crt
MGMT_AGENT_REPORT_CLIENT_KEY=$CERT_DIR/agent.key
MGMT_AGENT_NODE_NAME=$NODE_NAME
MGMT_AGENT_ENABLE_ACTIONS=true
MGMT_AGENT_UPDATE_MANIFEST_URL=$UPDATE_MANIFEST_URL
MGMT_AGENT_UPDATE_PUBLIC_KEY=$CERT_DIR/update-public.pem
EOF

systemctl daemon-reload
systemctl enable microk8s-mgmt-agent.service
systemctl restart microk8s-mgmt-agent.service
systemctl --no-pager --full status microk8s-mgmt-agent.service
echo "[$(date -Is)] Enrollment completed for $NODE_NAME"

