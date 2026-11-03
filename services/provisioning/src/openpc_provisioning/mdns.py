"""mDNS publishing so clients find the box by name, not by IP."""

from __future__ import annotations

import logging
from pathlib import Path
from xml.sax.saxutils import escape

from .wifi import CommandRunner, SubprocessRunner

log = logging.getLogger("openpc.provisioning.mdns")

AVAHI_SERVICE_FILE = Path("/etc/avahi/services/openpc.service")
SERVICE_TYPE = "_openpc._tcp"


def render_service_xml(instance: str, port: int, txt: dict[str, str]) -> str:
    """An Avahi service definition for ``_openpc._tcp``.

    Values are escaped: an SSID or hostname is attacker-influenced input as far
    as this file is concerned, and Avahi parses XML.
    """
    lines = [
        '<?xml version="1.0" standalone=\'no\'?><!--*-nxml-*-->',
        "<!DOCTYPE service-group SYSTEM \"avahi-service.dtd\">",
        "<service-group>",
        f"  <name replace-wildcards=\"yes\">{escape(instance)}</name>",
        "  <service>",
        f"    <type>{SERVICE_TYPE}</type>",
        f"    <port>{int(port)}</port>",
    ]
    for key, value in txt.items():
        lines.append(f"    <txt-record>{escape(str(key))}={escape(str(value))}</txt-record>")
    lines += ["  </service>", "</service-group>", ""]
    return "\n".join(lines)


class MdnsPublisher:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        path: Path = AVAHI_SERVICE_FILE,
        port: int = 8095,
    ) -> None:
        self.run = runner or SubprocessRunner()
        self.path = Path(path)
        self.port = port

    async def publish(self, instance: str, appliance_id: str, engine_port: int | None = None) -> bool:
        xml = render_service_xml(
            instance,
            engine_port or self.port,
            {"id": appliance_id, "proto": "1", "engine": "a-coder-cli"},
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(xml)
        except OSError as exc:
            log.error("cannot write avahi service file", extra={"detail": str(exc)})
            return False
        rc, out = await self.run("systemctl", "reload-or-restart", "avahi-daemon")
        if rc != 0:
            # Not fatal: the file is in place and Avahi picks it up on restart.
            log.warning("avahi reload failed", extra={"detail": out.strip()[:200]})
        log.info("mdns advertised", extra={"detail": f"{SERVICE_TYPE} {instance} :{self.port}"})
        return True
