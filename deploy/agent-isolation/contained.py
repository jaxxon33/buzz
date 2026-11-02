"""Run the harness with an opaque local relay hop; all children stay unprivileged."""
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
    with Tunnel(("127.0.0.1", 443), (host, int(port))) as tunnel:
        tunnel.start()
        child = subprocess.Popen(["/opt/buzz/bin/buzz-acp"])
        signal.signal(signal.SIGTERM, lambda *_: child.terminate())
        signal.signal(signal.SIGINT, lambda *_: child.terminate())
        try:
            return child.wait()
        finally:
            tunnel.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
