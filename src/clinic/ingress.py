"""Trusted SIP ingress for every clinic: LiveKit-set attributes only.

The destination (called number + trunk) is set by LiveKit's SIP service from the
carrier INVITE. Caller ID, custom headers and caller speech never select a tenant.
The database resolver maps the destination to exactly one active clinic or fails.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from livekit import rtc

from clinic.resolver import ClinicUnavailable, InboundDestination


@dataclass(frozen=True)
class SipIngress:
    destination: InboundDestination
    call_id: str


def trusted_destination(kind: int, attributes: Mapping[str, str]) -> SipIngress:
    if kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        raise ClinicUnavailable("Not a native SIP participant")
    call_id = attributes.get("sip.callID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id):
        raise ClinicUnavailable("SIP call identifier unavailable")
    provider = os.environ.get("TELEPHONY_PROVIDER", "plivo")
    # InboundDestination validates the E.164 number, trunk format and provider.
    return SipIngress(
        InboundDestination(
            attributes.get("sip.trunkPhoneNumber", ""),
            attributes.get("sip.trunkID", ""),
            provider,
        ),
        call_id,
    )
