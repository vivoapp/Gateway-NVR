"""Settings, read from the environment.

On the gateway, Compose passes /etc/vivo/site.env and /etc/vivo/video-api.env as the container's
environment. Every limit in contract section 11.1 is a setting here.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.replace(";", ",").split(",") if v.strip())


def _networks(value: str) -> tuple:
    out = []
    for item in _list(value):
        out.append(ipaddress.ip_network(item, strict=False))
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    gateway_id: str = "vivo-gw"
    keys_file: Path = Path("/etc/vivo/video-api-keys/rails.jwks.json")
    inventory_file: Path = Path("/opt/vivo-gateway/cameras.yaml")
    data_dir: Path = Path("/srv/vivo/video-api")
    recorder: str = "frigate"                      # frigate | mock
    mock_dir: Path = Path("/srv/vivo/video-api/mock")

    frigate_url: str = "https://127.0.0.1:8971"
    frigate_user: str = "video_api"
    frigate_password: str = ""
    go2rtc_url: str = "http://127.0.0.1:1984"
    go2rtc_user: str = ""
    go2rtc_password: str = ""
    health_url: str = "http://127.0.0.1:9101/health"

    local_cidrs: tuple = field(default_factory=tuple)
    grant_issuer: str = "vivo-rails"
    grant_max_lifetime: int = 120
    grant_leeway: int = 5

    live_site_remote_max: int = 6
    live_camera_max: int = 5
    live_renew_after: int = 30
    live_gate_dir: Path = Path("/run/vivo-live-gate")
    stun_urls: tuple[str, ...] = ()
    turn_urls: tuple[str, ...] = ()
    turn_secret: str = ""
    turn_ttl: int = 600

    timeline_max_window: int = 86400
    gap_threshold: float = 3.0

    clip_max_seconds: int = 1800
    clip_workers: int = 2
    clip_queue_max: int = 6
    clip_keep: int = 3600
    clip_store_max_bytes: int = 5 * 1000 ** 3

    audit_days: int = 30
    cors_origins: tuple[str, ...] = ()

    @property
    def audience(self) -> str:
        return f"vivo-gw:{self.gateway_id}"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)

        def s(name: str, default: str) -> str:
            return e.get(name, default) or default

        def i(name: str, default: int) -> int:
            return int(e.get(name) or default)

        local = e.get("LOCAL_CIDRS") or e.get("LAN_CIDRS") or ""
        return cls(
            gateway_id=s("GATEWAY_ID", "vivo-gw"),
            keys_file=Path(s("VIDEO_API_KEYS", "/etc/vivo/video-api-keys/rails.jwks.json")),
            inventory_file=Path(s("VIDEO_API_INVENTORY", "/opt/vivo-gateway/cameras.yaml")),
            data_dir=Path(s("VIDEO_API_DATA", "/srv/vivo/video-api")),
            recorder=s("RECORDER", "frigate"),
            mock_dir=Path(s("MOCK_DIR", "/srv/vivo/video-api/mock")),
            frigate_url=s("FRIGATE_URL", "https://127.0.0.1:8971"),
            frigate_user=s("FRIGATE_USER", "video_api"),
            frigate_password=e.get("FRIGATE_PASSWORD", ""),
            go2rtc_url=s("GO2RTC_URL", "http://127.0.0.1:1984"),
            go2rtc_user=e.get("GO2RTC_API_USER", ""),
            go2rtc_password=e.get("GO2RTC_API_PASSWORD", ""),
            health_url=s("HEALTH_URL", "http://127.0.0.1:9101/health"),
            local_cidrs=_networks(local),
            grant_issuer=s("GRANT_ISSUER", "vivo-rails"),
            grant_max_lifetime=i("GRANT_MAX_LIFETIME", 120),
            grant_leeway=i("GRANT_LEEWAY", 5),
            live_site_remote_max=i("LIVE_SITE_REMOTE_MAX", 6),
            live_camera_max=i("LIVE_CAMERA_MAX", 5),
            live_renew_after=i("LIVE_RENEW_AFTER", 30),
            live_gate_dir=Path(s("LIVE_GATE_DIR", "/run/vivo-live-gate")),
            stun_urls=_list(e.get("STUN_URLS", "")),
            turn_urls=_list(e.get("TURN_URLS", "")),
            turn_secret=e.get("TURN_SECRET", ""),
            turn_ttl=i("TURN_TTL", 600),
            timeline_max_window=i("TIMELINE_MAX_WINDOW", 86400),
            gap_threshold=float(e.get("GAP_THRESHOLD") or 3.0),
            clip_max_seconds=i("CLIP_MAX_SECONDS", 1800),
            clip_workers=i("CLIP_WORKERS", 2),
            clip_queue_max=i("CLIP_QUEUE_MAX", 6),
            clip_keep=i("CLIP_KEEP", 3600),
            clip_store_max_bytes=int(float(e.get("CLIP_STORE_MAX_GB") or 5) * 1000 ** 3),
            audit_days=i("AUDIT_DAYS", 30),
            cors_origins=_list(e.get("CORS_ORIGINS", "")),
        )

    def is_local(self, client_ip: str | None) -> bool:
        """True for an address on the building network. 127.0.0.1 is not local: remote requests
        arrive through the gateway's outbound connection (M21, or the test tunnel) on loopback."""
        if not client_ip:
            return False
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(addr in net for net in self.local_cidrs)
