"""Private WSL stdio relay used by the Windows loopback sign-in helper.

Only proxies the fixed WSL loopback callback. Do not log the HTTP byte stream.
"""
import os
import select
import socket
import sys

with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=5) as connection:
    connection.settimeout(None)
    inputs = [connection, sys.stdin.buffer]
    while True:
        ready, _, _ = select.select(inputs, [], [], 30)
        if not ready:
            break
        if connection in ready:
            chunk = connection.recv(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        if sys.stdin.buffer in ready:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                connection.shutdown(socket.SHUT_WR)
                inputs.remove(sys.stdin.buffer)
            else:
                connection.sendall(chunk)
