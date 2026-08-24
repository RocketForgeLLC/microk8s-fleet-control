import hashlib
import http.server
import importlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path


def test_signed_agent_update_installation():
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="fleet-update-test-") as temp_name:
        temp = Path(temp_name)
        served = temp / "served"
        served.mkdir()
        for name in ("agent-release.tgz", "agent-release.sig", "update-public.pem"):
            shutil.copy2(project / name, served / name)

        handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
            *args, directory=str(served), **kwargs)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            archive = served / "agent-release.tgz"
            manifest = {"channels": {"stable": {
                "version": "0.1.1",
                "url": f"{base}/agent-release.tgz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "signatureUrl": f"{base}/agent-release.sig",
            }}}
            (served / "manifest.json").write_text(json.dumps(manifest))
            os.environ.update({
                "MGMT_AGENT_VERSION": "0.1.0",
                "MGMT_AGENT_CHANNEL": "stable",
                "MGMT_AGENT_STATE_DIR": str(temp / "state"),
                "MGMT_AGENT_INSTALL_DIR": str(temp / "install"),
                "MGMT_AGENT_UPDATE_PUBLIC_KEY": str(served / "update-public.pem"),
                "MGMT_AGENT_UPDATE_MANIFEST_URL": f"{base}/manifest.json",
            })
            import agent.update_manager as updater
            updater = importlib.reload(updater)
            result = updater.apply_available_update()
            assert result["status"] == "installed"
            assert result["installedVersion"] == "0.1.1"
            assert (temp / "install" / "microk8s-mgmt-agent.py").is_file()
            assert (temp / "state" / "update-status.json").is_file()
        finally:
            server.shutdown()


if __name__ == "__main__":
    test_signed_agent_update_installation()
    print("signed agent update test passed")

