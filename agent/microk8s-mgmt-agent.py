#!/usr/bin/env python3
"""Minimal, allowlisted Ubuntu node agent for MicroK8s maintenance.

The agent is intentionally host-local and systemd-managed. It never accepts an
arbitrary shell command; every operation maps to a fixed command sequence.
"""
import json
import os
import ssl
import subprocess
import threading
import time
import uuid
import datetime
import urllib.request
import urllib.error
from update_manager import VERSION as AGENT_VERSION, CHANNEL as AGENT_CHANNEL, UPDATE_STATUS_PATH, apply_available_update
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.getenv("MGMT_AGENT_PORT", "9443"))
STATE_DIR = Path(os.getenv("MGMT_AGENT_STATE_DIR", "/var/lib/microk8s-mgmt-agent"))
LOG_DIR = Path(os.getenv("MGMT_AGENT_LOG_DIR", "/var/log/microk8s-mgmt-agent"))
CERT_DIR = Path(os.getenv("MGMT_AGENT_CERT_DIR", "/etc/microk8s-mgmt-agent"))
UPGRADE_SCRIPT = os.getenv("MGMT_AGENT_UPGRADE_SCRIPT", "/usr/local/lib/microk8s-mgmt-agent/Upgrade-MicroK8sNode.sh")
MICROK8S_BIN = os.getenv("MGMT_AGENT_MICROK8S_BIN") or next(
    (candidate for candidate in ("/snap/bin/microk8s", "/usr/bin/microk8s", "microk8s")
     if candidate == "microk8s" or Path(candidate).is_file()), "microk8s")
ENABLE_ACTIONS = os.getenv("MGMT_AGENT_ENABLE_ACTIONS", "false").lower() == "true"
STATUS_INTERVAL = int(os.getenv("MGMT_AGENT_STATUS_INTERVAL", "900"))
NODE_NAME = os.uname().nodename.split(".", 1)[0].lower()
LOCK = threading.Lock()
STATUS_PATH = STATE_DIR / "status.json"
REPORT_URL = os.getenv("MGMT_AGENT_REPORT_URL", "")
REPORT_TOKEN = os.getenv("MGMT_AGENT_REPORT_TOKEN", "")
REPORT_CA = os.getenv("MGMT_AGENT_REPORT_CA", "/etc/microk8s-mgmt-agent/ca.crt")
REPORT_CLIENT_CERT = os.getenv("MGMT_AGENT_REPORT_CLIENT_CERT", "")
REPORT_CLIENT_KEY = os.getenv("MGMT_AGENT_REPORT_CLIENT_KEY", "")

OPERATIONS = {
    "apt-update": ["/usr/bin/apt-get", "update"],
}
JOB_RESULTS_PATH = STATE_DIR / "job-results.json"
ACTIVE_JOBS = set()

def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    os.replace(temporary, path)

def command_output(command, timeout=120):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)

def log_tail(path, limit=80):
    try:
        lines = path.read_text(errors="replace").splitlines()
        return lines[-limit:]
    except FileNotFoundError:
        return []

def refresh_update_status():
    """Refresh package/snap state from the configured online repositories.

    This only refreshes package metadata and simulates upgrades; it never
    installs packages. The cached result is served to the controller.
    """
    if not LOCK.acquire(blocking=False):
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        apt_update_rc, apt_update_out, apt_update_err = command_output(
            ["/usr/bin/apt-get", "-qq", "update"], timeout=600)
        sim_rc, sim_out, sim_err = command_output(
            ["/usr/bin/apt-get", "-s", "-q", "upgrade"], timeout=120)
        snap_rc, snap_out, snap_err = command_output(
            ["/usr/bin/snap", "refresh", "--list"], timeout=120)
        upgrade_lines = [line.strip() for line in sim_out.splitlines()
                         if line.strip().startswith(("Inst ", "The following packages"))]
        snap_lines = [line.strip() for line in snap_out.splitlines() if line.strip()]
        snap_update = snap_rc == 0 and any("microk8s" in line.lower() for line in snap_lines)
        result = {
            "checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "onlineChecks": True,
            "apt": {
                "updateSucceeded": apt_update_rc == 0,
                "upgradeCheckSucceeded": sim_rc == 0,
                "upgradeAvailable": bool(upgrade_lines),
                "packages": upgrade_lines[:50],
                "error": apt_update_err.strip() or sim_err.strip() or None,
            },
            "microk8s": {
                "checkSucceeded": snap_rc == 0,
                "upgradeAvailable": snap_update,
                "details": snap_lines[:20],
                "error": snap_err.strip() or None,
            },
        }
        write_json(STATUS_PATH, result)
    finally:
        LOCK.release()

def status_loop():
    while True:
        refresh_update_status()
        apply_available_update(context=update_context())
        report_status()
        time.sleep(STATUS_INTERVAL)

def update_context():
    context = ssl.create_default_context(cafile=REPORT_CA) if Path(REPORT_CA).is_file() else ssl._create_unverified_context()
    if REPORT_CLIENT_CERT and REPORT_CLIENT_KEY:
        context.load_cert_chain(REPORT_CLIENT_CERT, REPORT_CLIENT_KEY)
    return context

def report_status():
    if not REPORT_URL or not REPORT_TOKEN:
        return
    payload = {"node": NODE_NAME, "agentVersion": AGENT_VERSION, "channel": AGENT_CHANNEL,
               "actionsEnabled": ENABLE_ACTIONS,
               "current": read_json(STATE_DIR / "current.json", None),
               "last": read_json(STATE_DIR / "last.json", None),
               "updates": read_json(STATUS_PATH, None),
               "agentUpdate": read_json(UPDATE_STATUS_PATH, None),
               "jobResults": read_json(JOB_RESULTS_PATH, [])[-20:]}
    try:
        request = urllib.request.Request(REPORT_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Mgmt-Agent-Token": REPORT_TOKEN})
        context = ssl.create_default_context(cafile=REPORT_CA) if Path(REPORT_CA).is_file() else ssl._create_unverified_context()
        if REPORT_CLIENT_CERT and REPORT_CLIENT_KEY:
            context.load_cert_chain(REPORT_CLIENT_CERT, REPORT_CLIENT_KEY)
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            response_body = json.loads(response.read() or b"{}")
            print(f"status report accepted: HTTP {response.status}", flush=True)
        for job in response_body.get("jobs", []):
            job_id = job.get("id")
            if ENABLE_ACTIONS and job_id and job_id not in ACTIVE_JOBS:
                ACTIVE_JOBS.add(job_id)
                threading.Thread(target=execute_job, args=(job,), daemon=True).start()
    except urllib.error.HTTPError as exc:
        print(f"status report failed: HTTP {exc.code} {exc.read().decode(errors='replace')[:200]}", flush=True)
    except Exception as exc:
        print(f"status report failed: {exc}", flush=True)

def kubectl(*args, capture=True):
    return subprocess.run([MICROK8S_BIN, "kubectl", *args], capture_output=capture,
                          text=True, timeout=120, check=False)

def prepare_node():
    node = kubectl("get", "node", NODE_NAME, "-o", "json")
    if node.returncode != 0:
        detail = (node.stderr or node.stdout).strip()
        raise RuntimeError(detail or "this host is not available through the MicroK8s API")
    data = json.loads(node.stdout)
    tainted = any(t.get("effect") in ("NoSchedule", "NoExecute")
                  for t in data.get("spec", {}).get("taints", []))
    scaled = []
    if tainted:
        pods = kubectl("get", "pods", "--all-namespaces",
                       f"--field-selector=spec.nodeName={NODE_NAME}", "-o", "json")
        if pods.returncode != 0:
            raise RuntimeError(pods.stderr.strip() or "could not inspect node workloads")
        seen = set()
        for pod in json.loads(pods.stdout).get("items", []):
            meta = pod.get("metadata", {})
            namespace = meta.get("namespace", "default")
            owners = [o for o in meta.get("ownerReferences", []) if o.get("controller")]
            if not owners:
                raise RuntimeError(f"standalone pod {namespace}/{meta.get('name')} blocks safe maintenance")
            owner = owners[0]
            kind, name = owner.get("kind"), owner.get("name")
            if kind == "DaemonSet":
                continue
            if kind == "ReplicaSet":
                rs = kubectl("-n", namespace, "get", "rs", name, "-o", "json")
                refs = json.loads(rs.stdout).get("metadata", {}).get("ownerReferences", []) if rs.returncode == 0 else []
                deployment = next((r.get("name") for r in refs if r.get("kind") == "Deployment"), None)
                if not deployment:
                    raise RuntimeError(f"unsupported ReplicaSet owner {namespace}/{name}")
                kind, name = "Deployment", deployment
            if kind not in ("Deployment", "StatefulSet"):
                raise RuntimeError(f"unsupported workload owner {kind}/{name}")
            key = (namespace, kind, name)
            if key in seen:
                continue
            seen.add(key)
            resource = kind.lower()
            current = kubectl("-n", namespace, "get", resource, name, "-o", "json")
            if current.returncode != 0:
                raise RuntimeError(current.stderr.strip() or f"could not read {kind}/{name}")
            replicas = json.loads(current.stdout).get("spec", {}).get("replicas", 1)
            scaled.append({"namespace": namespace, "kind": resource, "name": name, "replicas": replicas})
            result = kubectl("-n", namespace, "scale", f"{resource}/{name}", "--replicas=0")
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"could not scale {kind}/{name}")
        write_json(STATE_DIR / "scaled.json", scaled)
    result = kubectl("cordon", NODE_NAME)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not cordon node")
    result = kubectl("drain", NODE_NAME, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=20m")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not drain node")
    return {"tainted": tainted, "scaled": scaled}

def restore_node():
    scaled = read_json(STATE_DIR / "scaled.json", [])
    for item in scaled:
        kubectl("-n", item["namespace"], "scale", f'{item["kind"]}/{item["name"]}',
                f'--replicas={item["replicas"]}')
    try:
        (STATE_DIR / "scaled.json").unlink()
    except FileNotFoundError:
        pass
    kubectl("uncordon", NODE_NAME)

def publish_current(result):
    write_json(STATE_DIR / "current.json", result)
    report_status()

def reconcile_current():
    """Remove an operation marker left behind by a terminated agent process."""
    path = STATE_DIR / "current.json"
    current = read_json(path, None)
    if not current:
        return
    owner_pid = current.get("pid")
    active = current.get("id") in ACTIVE_JOBS
    if owner_pid != os.getpid() or not active:
        current["status"] = "failed"
        current["phase"] = "interrupted"
        current["error"] = "operation interrupted when the management agent stopped"
        current["finished"] = time.time()
        write_json(STATE_DIR / "last.json", current)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

def run_operation(operation, channel=None, external_id=None):
    if operation == "microk8s-upgrade":
        if not channel or "/" not in channel:
            raise ValueError("microk8s-upgrade requires a channel such as 1.30/stable")
        if not Path(UPGRADE_SCRIPT).is_file():
            raise FileNotFoundError(f"validated upgrade script is missing: {UPGRADE_SCRIPT}")
        command = [UPGRADE_SCRIPT, channel]
    elif operation == "apt-upgrade":
        command = []
    elif operation in OPERATIONS:
        command = OPERATIONS[operation]
    else:
        raise ValueError("operation is not allowlisted")

    job_id = external_id or str(uuid.uuid4())
    started = time.time()
    log_path = LOG_DIR / f"{job_id}.log"
    result = {"id": job_id, "node": NODE_NAME, "operation": operation, "channel": channel,
              "started": started, "pid": os.getpid(), "command": command, "status": "running",
              "phase": "preparing", "logPath": str(log_path)}
    write_json(STATE_DIR / "current.json", result)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / f"{job_id}.log").open("w") as log:
        if operation == "apt-upgrade":
            result["phase"] = "apt-update"
            publish_current(result)
            refresh = subprocess.run(["/usr/bin/apt-get", "update"], stdout=log,
                                     stderr=subprocess.STDOUT, text=True, check=False)
            if refresh.returncode != 0:
                process = refresh
            else:
                result["phase"] = "draining"
                publish_current(result)
                preparation = prepare_node()
                log.write(json.dumps({"event": "node-prepared", **preparation}) + "\n")
                result["phase"] = "apt-upgrade"
                result["logTail"] = log_tail(log_path)
                publish_current(result)
                process = subprocess.run(["/usr/bin/apt-get", "-y", "upgrade"], stdout=log,
                                         stderr=subprocess.STDOUT, text=True, check=False)
                result["phase"] = "restoring"
                result["logTail"] = log_tail(log_path)
                publish_current(result)
                restore_node()
                result["rebootRequired"] = Path("/var/run/reboot-required").exists()
        else:
            result["phase"] = operation
            publish_current(result)
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True, check=False)
    result.update({"finished": time.time(), "returncode": process.returncode,
                   "status": "succeeded" if process.returncode == 0 else "failed",
                   "phase": "completed" if process.returncode == 0 else "failed",
                   "logTail": log_tail(log_path)})
    write_json(STATE_DIR / "last.json", result)
    try:
        (STATE_DIR / "current.json").unlink()
    except FileNotFoundError:
        pass
    return result

def execute_job(job):
    job_id = job.get("id")
    try:
        if not ENABLE_ACTIONS:
            raise RuntimeError("agent actions are disabled")
        if not LOCK.acquire(timeout=900):
            raise RuntimeError("another operation is already running")
        try:
            result = run_operation(job.get("operation"), job.get("channel"), external_id=job_id)
        finally:
            LOCK.release()
    except Exception as exc:
        result = {"id": job_id, "node": NODE_NAME, "operation": job.get("operation"),
                  "status": "failed", "phase": "failed", "error": str(exc), "finished": time.time()}
        write_json(STATE_DIR / "last.json", result)
        try:
            (STATE_DIR / "current.json").unlink()
        except FileNotFoundError:
            pass
    results = read_json(JOB_RESULTS_PATH, [])
    results.append(result)
    write_json(JOB_RESULTS_PATH, results[-20:])
    ACTIVE_JOBS.discard(job_id)
    report_status()

class Handler(BaseHTTPRequestHandler):
    def respond(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/status":
            reconcile_current()
            current = read_json(STATE_DIR / "current.json", None)
            last = read_json(STATE_DIR / "last.json", None)
            self.respond(200, {"node": NODE_NAME, "agentVersion": AGENT_VERSION,
                               "channel": AGENT_CHANNEL, "actionsEnabled": ENABLE_ACTIONS,
                               "current": current, "last": last,
                               "updates": read_json(STATUS_PATH, None)})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/jobs":
            self.send_error(404)
            return
        if not ENABLE_ACTIONS:
            self.respond(423, {"error": "agent actions are disabled"})
            return
        if not LOCK.acquire(blocking=False):
            self.respond(409, {"error": "another operation is already running"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = run_operation(payload.get("operation"), payload.get("channel"))
            self.respond(200 if result["status"] == "succeeded" else 500, result)
        except Exception as exc:
            self.respond(400, {"error": str(exc)})
        finally:
            LOCK.release()

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

def main():
    threading.Thread(target=status_loop, daemon=True).start()
    time.sleep(2)
    report_status()
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(CERT_DIR / "agent.crt", CERT_DIR / "agent.key")
    context.load_verify_locations(CERT_DIR / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()

if __name__ == "__main__":
    main()

