#!/usr/bin/env python3
import json
import os
import secrets
import shlex
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).parent
PORT = int(os.getenv("PORT", "8080"))
KUBE_HOST = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
KUBE_PORT = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
KUBE_BASE = os.getenv("MGMT_KUBE_API_BASE", f"https://{KUBE_HOST}:{KUBE_PORT}")
AGENTS = json.loads(os.getenv("MGMT_AGENT_NODES", "[]"))
REPORT_TOKENS = json.loads(os.getenv("MGMT_AGENT_REPORT_TOKENS", "{}"))
REPORTS = {}
REPORTS_LOADED = False
REPORTS_CONFIGMAP = os.getenv("MGMT_REPORTS_CONFIGMAP", "maintenance-agent-reports")
JOBS = {}
JOBS_LOADED = False
JOBS_CONFIGMAP = os.getenv("MGMT_JOBS_CONFIGMAP", "maintenance-agent-jobs")
ENROLLMENT = None
PUBLIC_KUBE_API_SERVER = os.getenv("MGMT_PUBLIC_KUBE_API_SERVER", "").rstrip("/")
CONFIGURED_PUBLIC_URL = os.getenv("MGMT_PUBLIC_URL", "").rstrip("/")
APP_BUNDLE = {
    "microk8s-mgmt-agent.py": "microk8s-mgmt-agent.py",
    "update_manager.py": "update_manager.py",
    "microk8s-mgmt-agent.service": "microk8s-mgmt-agent.service",
    "microk8s-mgmt-agent.default": "microk8s-mgmt-agent.default",
}
AGENT_UPDATE_VERSION = os.getenv("MGMT_AGENT_UPDATE_VERSION", "")
AGENT_UPDATE_CHANNEL = os.getenv("MGMT_AGENT_UPDATE_CHANNEL", "stable")
AGENT_UPDATE_BASE_URL = os.getenv("MGMT_AGENT_UPDATE_BASE_URL", "").rstrip("/")
AGENT_CERT_DIR = Path(os.getenv("MGMT_AGENT_CERT_DIR", "/etc/maintenance-agent-certs"))
SETTINGS_WRITABLE = os.getenv("MGMT_SETTINGS_WRITE_ENABLED", "false").lower() == "true"
NAMESPACE = os.getenv("MGMT_NAMESPACE", "cluster-maintenance")
SETTINGS_NAME = os.getenv("MGMT_SETTINGS_CONFIGMAP", "maintenance-settings")
DEFAULT_SETTINGS = {
    "longhornProtection": True,
    "specialNodeMode": "auto",
    "agentStatusIntervalSeconds": 900,
}

def kube_get(path):
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    token = Path(token_path).read_text().strip()
    request = urllib.request.Request(KUBE_BASE + path, headers={"Authorization": f"Bearer {token}"})
    context = ssl.create_default_context(cafile=ca_path)
    with urllib.request.urlopen(request, context=context, timeout=8) as response:
        return json.loads(response.read())

def kube_request(path, method, payload):
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    token = Path(token_path).read_text().strip()
    body = json.dumps(payload).encode()
    request = urllib.request.Request(KUBE_BASE + path, data=body, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/merge-patch+json"})
    context = ssl.create_default_context(cafile=ca_path)
    with urllib.request.urlopen(request, context=context, timeout=8) as response:
        return json.loads(response.read() or b"{}")

def load_reports():
    global REPORTS_LOADED
    if REPORTS_LOADED:
        return
    try:
        raw = kube_get(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{REPORTS_CONFIGMAP}")
        REPORTS.update(json.loads(raw.get("data", {}).get("reports.json", "{}")))
    except Exception:
        pass
    REPORTS_LOADED = True

def persist_reports():
    try:
        kube_request(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{REPORTS_CONFIGMAP}", "PATCH",
                     {"data": {"reports.json": json.dumps(REPORTS)}})
    except Exception as exc:
        print(f"report persistence failed: {exc}", flush=True)

def persist_report_tokens():
    encoded = __import__("base64").b64encode(json.dumps(REPORT_TOKENS).encode()).decode()
    kube_request(f"/api/v1/namespaces/{NAMESPACE}/secrets/maintenance-agent-report-auth", "PATCH",
                 {"data": {"MGMT_AGENT_REPORT_TOKENS": encoded}})

def load_jobs():
    global JOBS_LOADED
    if JOBS_LOADED:
        return
    try:
        raw = kube_get(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{JOBS_CONFIGMAP}")
        JOBS.update(json.loads(raw.get("data", {}).get("jobs.json", "{}")))
    except Exception:
        pass
    JOBS_LOADED = True

def persist_jobs():
    kube_request(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{JOBS_CONFIGMAP}", "PATCH",
                 {"data": {"jobs.json": json.dumps(JOBS)}})

def queued_jobs(node):
    load_jobs()
    return [job for job in JOBS.values() if job.get("node") == node and job.get("status") == "queued"]

def latest_job(node):
    load_jobs()
    jobs = [job for job in JOBS.values() if job.get("node") == node]
    return max(jobs, key=lambda job: job.get("createdAt", ""), default=None)

def bundle_bytes(name):
    if name not in APP_BUNDLE:
        return None
    path = APP_DIR / APP_BUNDLE[name]
    return path.read_bytes() if path.is_file() else None

def request_public_url(handler):
    if CONFIGURED_PUBLIC_URL:
        return CONFIGURED_PUBLIC_URL
    host = handler.headers.get("Host", "").strip()
    if not host or any(ch in host for ch in "\r\n"):
        raise RuntimeError("the request host is unavailable; configure MGMT_PUBLIC_URL")
    forwarded = handler.headers.get("X-Forwarded-Proto", "https").split(",", 1)[0].strip()
    scheme = forwarded if forwarded in ("http", "https") else "https"
    return f"{scheme}://{host}"

def enrollment_script(code, public_url):
    if not PUBLIC_KUBE_API_SERVER:
        raise RuntimeError("MGMT_PUBLIC_KUBE_API_SERVER must be configured before enrollment scripts can be generated")
    proxy = f"http://127.0.0.1:18080/api/v1/namespaces/{NAMESPACE}/services/maintenance-controller:8080/proxy"
    report_url = f"{PUBLIC_KUBE_API_SERVER}/api/v1/namespaces/{NAMESPACE}/services/maintenance-controller:8080/proxy/api/agent/status"
    template = (APP_DIR / "installer-template.sh").read_text()
    return (template.replace("__ENROLLMENT_CODE__", shlex.quote(code))
                   .replace("__MGMT_CONSOLE__", shlex.quote(public_url))
                   .replace("__BUNDLE_PROXY__", shlex.quote(proxy))
                   .replace("__REPORT_URL__", shlex.quote(report_url)))

def settings():
    try:
        raw = kube_get(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{SETTINGS_NAME}")
        values = dict(DEFAULT_SETTINGS)
        for key, value in raw.get("data", {}).items():
            if key in ("longhornProtection",):
                values[key] = value.lower() == "true"
            elif key == "agentStatusIntervalSeconds":
                values[key] = int(value)
            elif key == "specialNodeMode":
                values[key] = value
        return values
    except Exception:
        return dict(DEFAULT_SETTINGS)

def status():
    nodes = kube_get("/api/v1/nodes").get("items", [])
    result = []
    for node in nodes:
        meta, spec, st = node.get("metadata", {}), node.get("spec", {}), node.get("status", {})
        conditions = {c.get("type"): c.get("status") for c in st.get("conditions", [])}
        result.append({
            "name": meta.get("name"),
            "ip": next((a.get("address") for a in st.get("addresses", []) if a.get("type") == "InternalIP"), ""),
            "version": st.get("nodeInfo", {}).get("kubeletVersion", "unknown"),
            "ready": conditions.get("Ready") == "True",
            "unschedulable": bool(spec.get("unschedulable")),
            "labels": meta.get("labels", {}),
            "taints": spec.get("taints", []),
        })
    agents = agent_status()
    by_name = {a["name"]: a for a in agents}
    for node in result:
        node["agent"] = by_name.get(node["name"], {"reachable": False, "error": "agent not configured"})
        node["maintenance"] = latest_job(node["name"])
        current = node["agent"].get("status", {}).get("current") if node.get("agent") else None
        if current:
            node["maintenance"] = {**(node["maintenance"] or {}), "id": current.get("id"),
                                    "node": node["name"], "operation": current.get("operation"),
                                    "status": "running", "phase": current.get("phase"),
                                    "result": current}
    return {"nodes": result, "agents": agents, "jobs": JOBS, "settings": settings(), "settingsWritable": SETTINGS_WRITABLE}

def agent_request(agent, method="GET", path="/v1/status", payload=None):
    context = ssl.create_default_context(cafile=str(AGENT_CERT_DIR / "ca.crt"))
    context.load_cert_chain(AGENT_CERT_DIR / "controller.crt", AGENT_CERT_DIR / "controller.key")
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(agent["url"] + path, data=body, method=method,
        headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(request, context=context, timeout=8) as response:
        return json.loads(response.read())

def agent_status():
    load_reports()
    values = []
    for name, report in REPORTS.items():
        values.append({"name": name, "reachable": True, "status": report, "source": "outbound"})
    for agent in AGENTS:
        if agent.get("name") in REPORTS:
            continue
        try:
            values.append({"name": agent["name"], "url": agent["url"], "reachable": True,
                           "status": agent_request(agent)})
        except Exception as exc:
            values.append({"name": agent["name"], "url": agent["url"], "reachable": False,
                           "error": str(exc)})
    return values

class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/agent/update/manifest":
            if not AGENT_UPDATE_VERSION:
                self.send_json(404, {"error": "no agent release is configured"})
                return
            try:
                base = AGENT_UPDATE_BASE_URL or request_public_url(self)
            except RuntimeError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            checksum = os.getenv("MGMT_AGENT_UPDATE_SHA256", "")
            if not checksum:
                self.send_json(503, {"error": "agent release checksum is not configured"})
                return
            release = {"version": AGENT_UPDATE_VERSION,
                       "url": f"{base}/api/agent/update/bundle",
                       "sha256": checksum,
                       "signatureUrl": f"{base}/api/agent/update/signature"}
            self.send_json(200, {"channels": {AGENT_UPDATE_CHANNEL: release}})
            return
        if self.path in ("/api/agent/update/bundle", "/api/agent/update/signature", "/api/agent/update/public-key"):
            names = {"/api/agent/update/bundle": "agent-release.tgz",
                     "/api/agent/update/signature": "agent-release.sig",
                     "/api/agent/update/public-key": "update-public.pem"}
            path = APP_DIR / names[self.path]
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/agent/bundle/"):
            name = self.path.rsplit("/", 1)[-1]
            body = bundle_bytes(name)
            if body is None:
                self.send_error(404)
                return
            content_type = "text/plain; charset=utf-8" if name.endswith((".py", ".service", ".default")) else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/onboarding":
            body = (APP_DIR / "onboarding.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/enrollment/status":
            if not ENROLLMENT:
                self.send_json(200, {"active": False})
            else:
                self.send_json(200, {"active": ENROLLMENT["expiresAt"] > time.time(),
                                     "expiresAt": ENROLLMENT["expiresAt"],
                                     "remaining": max(0, ENROLLMENT["maxEnrollments"] - ENROLLMENT["used"])})
            return
        if self.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                try:
                    self.wfile.write(("data: " + json.dumps(status()) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(10)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:
                    try:
                        self.wfile.write(("event: error\ndata: " + json.dumps({"error": str(exc)}) + "\n\n").encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    time.sleep(10)
            return
        if self.path == "/api/status":
            try:
                self.send_json(200, status())
            except Exception as exc:
                self.send_json(503, {"error": str(exc)})
            return
        if self.path == "/api/settings":
            self.send_json(200, settings())
            return
        if self.path in ("/", "/index.html"):
            body = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/settings":
            body = (APP_DIR / "settings.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        global ENROLLMENT
        if self.path == "/api/enrollment/generate":
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length) or b"{}")
            minutes = max(15, min(120, int(incoming.get("minutes", 60))))
            maximum = max(1, min(250, int(incoming.get("maxEnrollments", 25))))
            code = secrets.token_urlsafe(32)
            ENROLLMENT = {"code": code, "expiresAt": time.time() + minutes * 60,
                          "maxEnrollments": maximum, "used": 0}
            try:
                script = enrollment_script(code, request_public_url(self))
            except RuntimeError as exc:
                ENROLLMENT = None
                self.send_json(409, {"error": str(exc)})
                return
            self.send_json(200, {"code": code, "expiresAt": ENROLLMENT["expiresAt"],
                                 "remaining": maximum, "script": script})
            return
        if self.path == "/api/agent/enroll":
            code = self.headers.get("X-Mgmt-Enrollment-Code", "")
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length) or b"{}")
            node = str(incoming.get("node", "")).strip()
            if not ENROLLMENT or ENROLLMENT["expiresAt"] <= time.time() or ENROLLMENT["used"] >= ENROLLMENT["maxEnrollments"]:
                self.send_json(410, {"error": "enrollment code is expired or exhausted"})
                return
            if not code or not secrets.compare_digest(code, ENROLLMENT["code"]):
                self.send_json(401, {"error": "invalid enrollment code"})
                return
            if not node or len(node) > 253 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for ch in node):
                self.send_json(400, {"error": "invalid node name"})
                return
            token = secrets.token_hex(32)
            REPORT_TOKENS[node] = token
            try:
                persist_report_tokens()
            except Exception as exc:
                self.send_json(503, {"error": "could not persist enrollment credential", "detail": str(exc)})
                return
            ENROLLMENT["used"] += 1
            self.send_json(201, {"node": node, "reportToken": token,
                                 "reportUrl": f"{PUBLIC_KUBE_API_SERVER}/api/v1/namespaces/{NAMESPACE}/services/maintenance-controller:8080/proxy/api/agent/status"})
            return
        if self.path == "/api/agent/status":
            token = self.headers.get("X-Mgmt-Agent-Token", "")
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            node = payload.get("node")
            if not node or not token or REPORT_TOKENS.get(node) != token:
                self.send_json(401, {"error": "invalid node report credentials"})
                return
            REPORTS[node] = {"node": node, "agentVersion": payload.get("agentVersion"),
                             "channel": payload.get("channel"),
                             "actionsEnabled": bool(payload.get("actionsEnabled")),
                             "current": payload.get("current"), "last": payload.get("last"),
                             "updates": payload.get("updates"), "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            for result in payload.get("jobResults", []):
                job = JOBS.get(result.get("id"))
                if job and job.get("node") == node:
                    job.update({"status": result.get("status", "failed"), "result": result,
                                "completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            persist_jobs()
            persist_reports()
            to_deliver = queued_jobs(node)
            for job in to_deliver:
                job["status"] = "running"
                job["deliveredAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            persist_jobs()
            self.send_json(202, {"accepted": True, "node": node, "jobs": to_deliver})
            return
        if self.path == "/api/settings":
            if not SETTINGS_WRITABLE:
                self.send_json(423, {"error": "settings writes are disabled until an authenticated admin path is configured"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length) or b"{}")
            current = settings()
            if "longhornProtection" in incoming:
                current["longhornProtection"] = bool(incoming["longhornProtection"])
            if incoming.get("specialNodeMode") in ("auto", "reject", "scale-down"):
                current["specialNodeMode"] = incoming["specialNodeMode"]
            if isinstance(incoming.get("agentStatusIntervalSeconds"), int):
                current["agentStatusIntervalSeconds"] = max(60, min(86400, incoming["agentStatusIntervalSeconds"]))
            patch = {"data": {"longhornProtection": str(current["longhornProtection"]).lower(),
                               "specialNodeMode": current["specialNodeMode"],
                               "agentStatusIntervalSeconds": str(current["agentStatusIntervalSeconds"])}}
            try:
                kube_request(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{SETTINGS_NAME}", "PATCH", patch)
                self.send_json(200, current)
            except Exception as exc:
                self.send_json(502, {"error": str(exc)})
            return
        if self.path != "/api/actions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        node = payload.get("node")
        operation = payload.get("operation")
        if operation not in ("apt-update", "apt-upgrade", "microk8s-upgrade"):
            self.send_json(400, {"error": "operation is not allowlisted"})
            return
        load_reports()
        report = REPORTS.get(node, {})
        if not report or not report.get("actionsEnabled") or not report.get("receivedAt"):
            self.send_json(423, {"error": "agent has not passed secure action validation"})
            return
        load_jobs()
        job_id = secrets.token_urlsafe(18)
        JOBS[job_id] = {"id": job_id, "node": node, "operation": operation,
                        "channel": payload.get("channel"), "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "status": "queued"}
        try:
            persist_jobs()
            self.send_json(202, JOBS[job_id])
        except Exception as exc:
            JOBS.pop(job_id, None)
            self.send_json(502, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

