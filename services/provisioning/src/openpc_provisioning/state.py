"""Provisioning state machine and credential validation."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "ProvisioningState",
    "ProvisioningStateMachine",
    "WifiCredentials",
    "CredentialsError",
    "reject_unsafe_ssid",
]


class ProvisioningState(str, Enum):
    NO_NETWORK = "no_network"
    ADVERTISING = "advertising"
    RECEIVING_CREDENTIALS = "receiving_credentials"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


#: Legal transitions. Everything else raises, so a BLE write can never move the
#: machine into a state the hardware is not prepared for.
TRANSITIONS: dict[ProvisioningState, frozenset[ProvisioningState]] = {
    ProvisioningState.NO_NETWORK: frozenset({ProvisioningState.ADVERTISING}),
    ProvisioningState.ADVERTISING: frozenset(
        {ProvisioningState.RECEIVING_CREDENTIALS, ProvisioningState.CONNECTED, ProvisioningState.NO_NETWORK}
    ),
    ProvisioningState.RECEIVING_CREDENTIALS: frozenset({ProvisioningState.CONNECTING, ProvisioningState.FAILED}),
    ProvisioningState.CONNECTING: frozenset({ProvisioningState.CONNECTED, ProvisioningState.FAILED}),
    # A failed attempt returns to advertising so the phone can retry with a
    # corrected passphrase - the box has no keyboard, so a dead end would
    # require a factory reset.
    ProvisioningState.FAILED: frozenset({ProvisioningState.ADVERTISING, ProvisioningState.NO_NETWORK}),
    ProvisioningState.CONNECTED: frozenset({ProvisioningState.NO_NETWORK}),
}

_INVALID_SSID = re.compile(r"[\x00-\x1f\x7f]")


class CredentialsError(ValueError):
    """The supplied credentials are unusable; the message is safe to show."""


def reject_unsafe_ssid(ssid: str) -> None:
    if not ssid:
        raise CredentialsError("SSID is empty")
    raw = ssid.encode("utf-8")
    # 802.11 limits the SSID to 32 *bytes*, not 32 characters.
    if len(raw) > 32:
        raise CredentialsError("SSID is longer than 32 bytes")
    if _INVALID_SSID.search(ssid):
        # Control characters would corrupt the NetworkManager keyfile and the
        # BLE status string.
        raise CredentialsError("SSID contains control characters")


@dataclass(slots=True)
class WifiCredentials:
    """A validated WPA2/WPA3 passphrase pair.

    The password is held in a plain ``str`` because NetworkManager needs it, but
    it is registered with the redactor and never included in ``repr``.
    """

    ssid: str
    password: str = field(repr=False)
    hidden: bool = False

    @classmethod
    def create(cls, ssid: str, password: str, hidden: bool = False) -> WifiCredentials:
        if not isinstance(ssid, str) or not isinstance(password, str):
            raise CredentialsError("SSID and password must be strings")
        reject_unsafe_ssid(ssid)
        if password:
            if len(password) < 8 or len(password) > 63:
                raise CredentialsError("WPA passphrase must be 8-63 characters")
            if any(ord(c) < 0x20 or ord(c) == 0x7F for c in password):
                raise CredentialsError("passphrase contains control characters")
        return cls(ssid=ssid, password=password, hidden=bool(hidden))

    @property
    def is_open(self) -> bool:
        return self.password == ""

    def __repr__(self) -> str:  # never leak the passphrase through a log line
        return f"WifiCredentials(ssid={self.ssid!r}, hidden={self.hidden}, open={self.is_open})"


class ProvisioningStateMachine:
    """Explicit, observable state machine. Single-threaded by construction."""

    def __init__(
        self,
        on_change: Callable[[ProvisioningState, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = ProvisioningState.NO_NETWORK
        self.message = "no network"
        self.changed_at = clock()
        self._on_change = on_change
        self._clock = clock
        self.history: list[tuple[float, ProvisioningState, str]] = []

    def can(self, target: ProvisioningState) -> bool:
        return target in TRANSITIONS[self.state]

    def transition(self, target: ProvisioningState, message: str = "") -> ProvisioningState:
        if not self.can(target):
            raise ValueError(f"illegal provisioning transition {self.state.value} -> {target.value}")
        self.state = target
        self.message = message or target.value
        self.changed_at = self._clock()
        self.history.append((self.changed_at, self.state, self.message))
        if self._on_change is not None:
            self._on_change(self.state, self.message)
        return self.state

    # -- convenience -------------------------------------------------------

    def network_present(self) -> None:
        """Boot with Ethernet, or WiFi already configured: skip provisioning."""
        if self.state is ProvisioningState.NO_NETWORK:
            self.transition(ProvisioningState.ADVERTISING, "network present")
        if self.state is ProvisioningState.ADVERTISING:
            self.transition(ProvisioningState.CONNECTED, "network present")

    def network_lost(self) -> None:
        if self.state is not ProvisioningState.NO_NETWORK:
            self.transition(ProvisioningState.NO_NETWORK, "network lost")

    def begin_advertising(self) -> None:
        if self.state is ProvisioningState.FAILED:
            self.transition(ProvisioningState.ADVERTISING, "retrying")
        elif self.state is ProvisioningState.NO_NETWORK:
            self.transition(ProvisioningState.ADVERTISING, "advertising")

    def credentials_received(self, fingerprint: str) -> None:
        self.transition(ProvisioningState.RECEIVING_CREDENTIALS, f"received from {fingerprint}")

    def connecting(self, ssid: str) -> None:
        self.transition(ProvisioningState.CONNECTING, f"joining {ssid}")

    def connected(self, ip: str | None) -> None:
        self.transition(ProvisioningState.CONNECTED, f"online {ip or 'dhcp-pending'}")

    def failed(self, reason: str) -> None:
        self.transition(ProvisioningState.FAILED, reason)
