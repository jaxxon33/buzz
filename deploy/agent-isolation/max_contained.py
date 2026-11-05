"""Keep Max's existing Node application with its harness inside the boundary."""
import os
import signal
import socket
import subprocess
import threading
import time

import contained


def main():
    if not os.environ.get('BUZZ_PILOT_RELAY_TARGET'):
        raise ValueError('Max requires the production relay tunnel')
    application = subprocess.Popen(['node', 'server/index.js'],
        cwd='/workspace/REPOS/you-are-here', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stopping = threading.Event()
    try:
        for _ in range(50):
            if application.poll() is not None:
                raise RuntimeError('Max application exited during startup')
            try:
                with socket.create_connection(('127.0.0.1', 8080), timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('Max application did not listen on its existing port')

        def monitor():
            application.wait()
            if not stopping.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=monitor, daemon=True).start()
        return contained.main()
    finally:
        stopping.set()
        if application.poll() is None:
            application.terminate()
            try:
                application.wait(timeout=10)
            except subprocess.TimeoutExpired:
                application.kill()
                application.wait()


if __name__ == '__main__':
    raise SystemExit(main())
