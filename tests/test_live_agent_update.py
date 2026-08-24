import importlib
import os
import tempfile
from pathlib import Path


def test_live_signed_agent_update():
    manifest = os.environ["MGMT_LIVE_MANIFEST_URL"]
    expected = os.environ.get("MGMT_LIVE_EXPECTED_VERSION", "0.1.2")
    os.environ.update({
        "MGMT_AGENT_VERSION": os.environ.get("MGMT_LIVE_CURRENT_VERSION", "0.1.1"),
        "MGMT_AGENT_CHANNEL": "stable",
        "MGMT_AGENT_UPDATE_MANIFEST_URL": manifest,
        "MGMT_AGENT_UPDATE_PUBLIC_KEY": os.environ["MGMT_LIVE_PUBLIC_KEY"],
    })
    with tempfile.TemporaryDirectory(prefix="fleet-live-update-") as temp_name:
        os.environ["MGMT_AGENT_STATE_DIR"] = str(Path(temp_name) / "state")
        os.environ["MGMT_AGENT_INSTALL_DIR"] = str(Path(temp_name) / "install")
        import agent.update_manager as updater
        updater = importlib.reload(updater)
        ca = os.environ["MGMT_LIVE_CA"]
        context = __import__("ssl").create_default_context(cafile=ca)
        context.load_cert_chain(os.environ["MGMT_LIVE_CLIENT_CERT"], os.environ["MGMT_LIVE_CLIENT_KEY"])
        result = updater.apply_available_update(context=context)
        assert result["status"] == "installed", result
        assert result["installedVersion"] == expected, result
        assert (Path(temp_name) / "install" / "microk8s-mgmt-agent.py").is_file()
        print("live signed agent update test passed")


if __name__ == "__main__":
    test_live_signed_agent_update()

