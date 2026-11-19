# TURN server and test tunnel: runbook

For the M15 remote test (a phone on cellular), and the same way for Vivo's production TURN server.
The test server stands in for two parts that are not ready yet: the production TURN server (Vivo)
and Haris's M21 connection. Templates are in `testserver/` and `gateway/files/vivo-test-tunnel.service`.

## What Vivo provides

- A small Linux server with a public IP address in Vivo's account: 1 to 2 vCPU, 1 to 2 GB RAM,
  Ubuntu 24.04, SSH access for the tester. Below, its address is `P` (for example 203.0.113.10).
- Its firewall (cloud security group) open for: 22/tcp from the tester, 80 and 443/tcp, 3478/udp and
  3478/tcp, 49152 to 65535/udp.
- For the direct-path test only: Jorge forwards UDP 8555 on the site router to the gateway, and
  `WEBRTC_PUBLIC=1` is set in the gateway's `/etc/vivo/site.env` (a firewall change, announced first).

## 1. TURN server (coturn)

```bash
sudo apt-get update && sudo apt-get install -y coturn
SECRET="$(openssl rand -hex 32)"                     # becomes TURN_SECRET on the gateway; keep it private
sudo sed -e "s/@TURN_SECRET@/$SECRET/" -e "s/@PUBLIC_IP@/P/" turnserver.conf | sudo tee /etc/turnserver.conf >/dev/null
sudo chmod 640 /etc/turnserver.conf && sudo chgrp turnserver /etc/turnserver.conf
sudo sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
sudo systemctl enable --now coturn
sudo ufw allow 3478 && sudo ufw allow 49152:65535/udp
```

The `denied-peer-ip` lines stop the server relaying into any private network, including its own.
The full relay port range matters: a TURN server hands relay ports out again (findings F4).

## 2. Test tunnel (stand-in for M21)

On the test server, an account that can only accept one forwarded port:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin vivo-tunnel
sudo install -d -m 700 -o vivo-tunnel -g vivo-tunnel ~vivo-tunnel/.ssh
# the gateway's public key (made below), with every right removed except that one forwarded port:
echo 'restrict,port-forwarding,permitlisten="127.0.0.1:18443",command="/bin/false" ssh-ed25519 AAAA... vivo-gateway-tunnel' \
  | sudo tee ~vivo-tunnel/.ssh/authorized_keys >/dev/null
sudo chown vivo-tunnel: ~vivo-tunnel/.ssh/authorized_keys && sudo chmod 600 ~vivo-tunnel/.ssh/authorized_keys
```

On the gateway (announced first: a new outbound connection):

```bash
sudo install -d -m 700 /etc/vivo/test-tunnel
sudo ssh-keygen -t ed25519 -N '' -C vivo-gateway-tunnel -f /etc/vivo/test-tunnel/id_ed25519
sudo sh -c 'ssh-keyscan -t ed25519 P > /etc/vivo/test-tunnel/known_hosts'     # check the fingerprint with the server
echo 'TEST_SERVER=P' | sudo tee /etc/vivo/test-tunnel/server.env >/dev/null
sudo install -m 644 video-api/gateway/files/vivo-test-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vivo-test-tunnel.service
```

On the test server, `curl -sk https://127.0.0.1:18443/v1/health` now answers from the gateway.
Requests through the tunnel arrive at the gateway on 127.0.0.1, so they count as remote viewers.

## 3. HTTPS address, test player and grant issuer (Caddy)

```bash
sudo apt-get install -y caddy python3-jwt python3-cryptography
sudo install -d /srv/vivo-test && sudo cp video-api/web/player.html /srv/vivo-test/index.html
HASH="$(caddy hash-password --plaintext 'a tester password')"
sudo sed -e "s/@HOST@/P-with-dashes.sslip.io/" -e "s#@TESTER_HASH@#$HASH#" Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy && sudo ufw allow 80,443/tcp
```

The test grant issuer runs on the test server only, on 127.0.0.1, behind the tester password. It
needs a test key whose public half is on the gateway:

```bash
python3 tools/grant_issuer.py keygen --dir ~/vivo-test-keys          # on the test server
# copy ~/vivo-test-keys/rails.jwks.json to the gateway's /etc/vivo/video-api-keys/rails.jwks.json
python3 tools/grant_issuer.py serve --key ~/vivo-test-keys/test-1.private.pem --aud vivo-gw:<GATEWAY_ID>
```

## 4. Gateway settings

In `/etc/vivo/video-api.env` (then `sudo systemctl restart vivo-video-api`):

```
STUN_URLS=stun:P:3478
TURN_URLS=turn:P:3478?transport=udp,turn:P:3478?transport=tcp
TURN_SECRET=<secret-from-step-1>
```

## 5. The test

On a phone on cellular (Wi-Fi off), open `https://P-with-dashes.sslip.io/?api=https://P-with-dashes.sslip.io&issuer=https://P-with-dashes.sslip.io/test`.

1. Direct path: on the gateway `sudo video-api/tests/m15_capture.sh direct 180 P`; on the phone "Run attempts" (10).
2. Through TURN: tick "Force TURN relay"; `sudo video-api/tests/m15_capture.sh turn 180 P`; "Run attempts" again.
3. "Revocation test" through TURN.
4. Copy the results table from the phone; the capture summaries are in `video-api/reports/`.

## 6. Afterwards

- Gateway: `sudo systemctl disable --now vivo-test-tunnel.service`, delete `/etc/vivo/test-tunnel/`,
  set `WEBRTC_PUBLIC=0` again if the direct path is not wanted in production (announced).
- Test server: stop the grant issuer, delete `~/vivo-test-keys` and the tunnel account.

## Production TURN server

The same coturn file with TLS added for networks that only allow HTTPS: a certificate for a real
host name, `tls-listening-port=5349` (or 443), and `turns:host:5349?transport=tcp` in `TURN_URLS`.
Size: each relayed viewer costs the server its stream twice (in and out), about 1 Mbit/s for a
substream viewer and up to 12 Mbit/s for a main-stream viewer.
