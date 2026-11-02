"""Bounded opaque TCP forwarding to one operator-selected destination."""
import selectors
import socket
import socketserver
import threading


class Tunnel(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, bind, target):
        self.target = target
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(bind, _Connection)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Expected disconnects must not log any payload or credential material.
        pass

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self.server_address[1]


class _Connection(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(self.server.target, timeout=10) as upstream:
                self.request.settimeout(10)
                upstream.settimeout(10)
                with selectors.DefaultSelector() as ready:
                    ready.register(self.request, selectors.EVENT_READ, upstream)
                    ready.register(upstream, selectors.EVENT_READ, self.request)
                    while True:
                        events = ready.select(timeout=1200)
                        if not events:
                            return
                        for key, _ in events:
                            data = key.fileobj.recv(65536)
                            if not data:
                                return
                            key.data.sendall(data)
        except OSError:
            return
