"""Start the API: HTTPS on 127.0.0.1 and on the gateway's LAN address, port VIDEO_API_PORT (8443).

Remote requests arrive on 127.0.0.1 through the gateway's outbound connection; the building
network uses the LAN address. Nothing listens on other interfaces.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket

import uvicorn


def addresses(env: dict) -> list[str]:
    out = ["127.0.0.1"]
    lan = env.get("GATEWAY_LAN_IP", "")
    try:
        if lan and not ipaddress.ip_address(lan).is_loopback:
            out.append(lan)
    except ValueError:
        pass
    return out


def main() -> None:
    env = dict(os.environ)
    port = int(env.get("VIDEO_API_PORT", "8443"))
    cert, key = env.get("VIDEO_API_TLS_CERT", "/tls/cert.pem"), env.get("VIDEO_API_TLS_KEY", "/tls/key.pem")
    socks = []
    for host in addresses(env):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        socks.append(s)
        print(f"video-api: https://{host}:{port}/v1/health", flush=True)
    config = uvicorn.Config("vivo_api.main:create_app", factory=True, ssl_certfile=cert, ssl_keyfile=key,
                            proxy_headers=False, server_header=False, date_header=True, access_log=False,
                            ws="websockets", timeout_keep_alive=30, log_level=env.get("LOG_LEVEL", "info"))
    asyncio.run(uvicorn.Server(config).serve(sockets=socks))


if __name__ == "__main__":
    main()
