"""Run the harness with an opaque local relay hop; all children stay unprivileged."""
from contextlib import ExitStack
import json
import os
import signal
import subprocess

from relay_tunnel import Tunnel


def main():
    setting = os.environ.pop("BUZZ_PILOT_RELAY_TARGET", None)
    if not setting:
        os.execv("/opt/buzz/bin/buzz-acp", ["buzz-acp"])
    host, port = json.loads(setting)
    with ExitStack() as cleanup:
        tunnel = cleanup.enter_context(Tunnel(("127.0.0.1", 443), (host, int(port))))
        tunnel.start()
        cleanup.callback(tunnel.shutdown)
        model_setting = os.environ.pop("BUZZ_PILOT_MODEL_TARGET", None)
        if model_setting:
            model_host, model_port = json.loads(model_setting)
            model_tunnel = cleanup.enter_context(Tunnel(("127.0.0.1", 8100), (model_host, int(model_port))))
            model_tunnel.start()
            cleanup.callback(model_tunnel.shutdown)
        child = subprocess.Popen(["/opt/buzz/bin/buzz-acp"])
        signal.signal(signal.SIGTERM, lambda *_: child.terminate())
        signal.signal(signal.SIGINT, lambda *_: child.terminate())
        return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
