from __future__ import annotations

import socket

import pytest


def test_socket_create_connection_is_blocked() -> None:
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 80), timeout=1)


def test_raw_socket_connect_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(RuntimeError):
        sock.connect(("example.com", 80))
