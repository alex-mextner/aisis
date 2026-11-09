#!/usr/bin/env python3
"""TCP-прокси: 192.168.0.56:31116 -> 127.0.0.1:31116 (quasar-server API).

quasar-server в docker слушает только loopback; Home Assistant на другой
машине (192.168.0.39) не может достучаться. Прокси не трогает контейнер
(пересоздание рвёт сессию колонки) и поднимается отдельным systemd-сервисом.
"""
import os
import socket
import socketserver
import threading

LISTEN_HOST = os.environ.get("LISTEN_HOST", "192.168.0.56")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "31116"))
TARGET_HOST = os.environ.get("TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "31116"))


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
        sockets = [self.request, upstream]
        try:
            while True:
                self._pump(sockets)
        except OSError:
            pass
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _pump(sockets):
        import select

        ready, _, _ = select.select(sockets, [], [], 30)
        if not ready:
            return
        for src in ready:
            dst = sockets[1] if src is sockets[0] else sockets[0]
            data = src.recv(65536)
            if not data:
                raise OSError("closed")
            dst.sendall(data)


class ThreadingProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadingProxyServer((LISTEN_HOST, LISTEN_PORT), Proxy)
    print(f"quasar LAN proxy {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}")
    server.serve_forever()
