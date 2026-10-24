"""Switch a camera's PoE port off and on, for the disconnect tests (M3, M10).

Backends, chosen with POE_BACKEND in /etc/vivo/site.env:
  manual   ask the operator to unplug / plug the camera (default)
  command  run POE_OFF_CMD / POE_ON_CMD from site.env, with {port} replaced
           e.g. POE_OFF_CMD=snmpset -v2c -c private 10.0.0.2 1.3.6.1.2.1.105.1.1.1.3.1.{port} i 2
                POE_ON_CMD=snmpset -v2c -c private 10.0.0.2 1.3.6.1.2.1.105.1.1.1.3.1.{port} i 1
           (pethPsePortAdminEnable from the standard POWER-ETHERNET-MIB)
The camera's port number is `poe_port` in cameras.yaml.
"""
import shlex
import subprocess

import vivo


def _cmd(template: str, port) -> None:
    subprocess.run(shlex.split(template.format(port=port)), check=True, capture_output=True, timeout=30)


def set_power(cam: dict, on: bool) -> None:
    backend = vivo.SITE.get("POE_BACKEND", "manual")
    port = cam.get("poe_port")
    if backend == "command":
        if port is None:
            raise RuntimeError(f"{cam['id']}: poe_port missing in cameras.yaml")
        _cmd(vivo.SITE["POE_ON_CMD" if on else "POE_OFF_CMD"], port)
    elif backend == "manual":
        input(f">>> {'PLUG IN' if on else 'UNPLUG'} camera {cam['id']} ({cam['name']}), then press Enter ")
    else:
        raise RuntimeError(f"unknown POE_BACKEND {backend}")
