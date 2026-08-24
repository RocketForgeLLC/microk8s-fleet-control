#!/usr/bin/env python3
"""Verified, rollback-safe MicroK8s management-agent updater."""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = os.getenv("MGMT_AGENT_VERSION", "0.1.1")
CHANNEL = os.getenv("MGMT_AGENT_CHANNEL", "stable")
STATE_DIR = Path(os.getenv("MGMT_AGENT_STATE_DIR", "/var/lib/microk8s-mgmt-agent"))
INSTALL_DIR = Path(os.getenv("MGMT_AGENT_INSTALL_DIR", "/usr/local/lib/microk8s-mgmt-agent"))
PUBLIC_KEY = Path(os.getenv("MGMT_AGENT_UPDATE_PUBLIC_KEY", "/etc/microk8s-mgmt-agent/update-public.pem"))
MANIFEST_URL = os.getenv("MGMT_AGENT_UPDATE_MANIFEST_URL", "")
UPDATE_STATUS_PATH = STATE_DIR / "update-status.json"

def version_tuple(value):
    return tuple(int(part) for part in value.lstrip("v").split(".")[:3])

def newer(candidate, current=VERSION):
    try:
        return version_tuple(candidate) > version_tuple(current)
    except (ValueError, TypeError):
        return False

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def verify_signature(archive, signature):
    if not PUBLIC_KEY.is_file():
        return False
    result = subprocess.run([
        "/usr/bin/openssl", "dgst", "-sha256", "-verify", str(PUBLIC_KEY),
        "-signature", str(signature), str(archive)
    ], capture_output=True, text=True, check=False)
    return result.returncode == 0 and "Verified OK" in result.stdout

def download(url, destination, context=None):
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(request, context=context, timeout=60) as response:
        destination.write_bytes(response.read())

def check_manifest(url, context=None):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, context=context, timeout=15) as response:
        manifest = json.loads(response.read())
    release = manifest.get("channels", {}).get(CHANNEL)
    if not release or not newer(release.get("version")):
        return {"currentVersion": VERSION, "channel": CHANNEL, "updateAvailable": False}
    return {"currentVersion": VERSION, "channel": CHANNEL, "updateAvailable": True,
            "version": release["version"], "url": release["url"],
            "sha256": release["sha256"], "signatureUrl": release["signatureUrl"]}

def install_release(release, context=None):
    """Download, verify, stage, and atomically install a signed release."""
    with tempfile.TemporaryDirectory(prefix="mgmt-agent-update-") as temp:
        root = Path(temp)
        archive, signature = root / "agent.tgz", root / "agent.sig"
        download(release["url"], archive, context=context)
        download(release["signatureUrl"], signature, context=context)
        if sha256(archive) != release["sha256"] or not verify_signature(archive, signature):
            raise RuntimeError("agent update failed signature or checksum validation")
        stage = root / "stage"
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(stage)
        payload = stage / "microk8s-mgmt-agent"
        if not (payload / "microk8s-mgmt-agent.py").is_file():
            raise RuntimeError("agent update payload is incomplete")
        backup = INSTALL_DIR.with_name(INSTALL_DIR.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if INSTALL_DIR.exists():
            INSTALL_DIR.rename(backup)
        payload.rename(INSTALL_DIR)
        (STATE_DIR / "installed-version").write_text(release["version"])
    return release["version"]

def check_for_update(context=None):
    if not MANIFEST_URL:
        return {"currentVersion": VERSION, "channel": CHANNEL, "updateAvailable": False,
                "reason": "update channel is not configured"}
    try:
        return check_manifest(MANIFEST_URL, context=context)
    except Exception as exc:
        return {"currentVersion": VERSION, "channel": CHANNEL, "updateAvailable": False,
                "error": str(exc)}

def apply_available_update(context=None):
    """Install one verified release and ask systemd to restart the agent."""
    release = check_for_update(context=context)
    UPDATE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not release.get("updateAvailable"):
        UPDATE_STATUS_PATH.write_text(json.dumps(release, indent=2))
        return release
    try:
        installed = install_release(release, context=context)
        result = {**release, "installedVersion": installed, "status": "installed"}
        UPDATE_STATUS_PATH.write_text(json.dumps(result, indent=2))
        subprocess.Popen(["/usr/bin/systemctl", "restart", "microk8s-mgmt-agent.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result
    except Exception as exc:
        result = {**release, "status": "failed", "error": str(exc)}
        UPDATE_STATUS_PATH.write_text(json.dumps(result, indent=2))
        return result

