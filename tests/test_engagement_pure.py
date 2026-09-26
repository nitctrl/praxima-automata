"""CRM rules and personal-data protection, without a database."""

import os
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from praxima.modules.engagement.application.vault import Vault
from praxima.modules.engagement.domain.work_items import check_transition
from praxima.shared.errors import Conflict, ValidationFailed
from praxima.shared.security.lookup import PhoneLookup
from praxima.shared.security.privacy import PiiCipher


def make_vault() -> Vault:
    return Vault(PiiCipher({"v1": os.urandom(32)}, "v1"), PhoneLookup(os.urandom(32)))


def test_sealed_data_only_opens_in_its_workspace_record_and_field():
    vault, ws, record = make_vault(), uuid.uuid4(), uuid.uuid4()
    sealed = vault.seal(ws, record, "phone", "+919876543210")
    assert sealed is not None and b"9876543210" not in sealed
    assert vault.open(ws, record, "phone", sealed, "v1") == "+919876543210"
    for other in (
        (uuid.uuid4(), record, "phone"),
        (ws, uuid.uuid4(), "phone"),
        (ws, record, "name"),
    ):
        with pytest.raises(InvalidTag):  # copied ciphertext is useless elsewhere
            vault.open(*other, sealed, "v1")
    assert vault.seal(ws, record, "phone", None) is None


def test_phone_digests_are_workspace_specific():
    lookup = PhoneLookup(os.urandom(32))
    a, b = uuid.uuid4(), uuid.uuid4()
    assert lookup.digest(a, "+919876543210") == lookup.digest(a, "+919876543210")
    assert lookup.digest(a, "+919876543210") != lookup.digest(b, "+919876543210")
    with pytest.raises(ValueError):
        lookup.digest(a, "98765 43210")
    with pytest.raises(ValueError):
        PhoneLookup(b"short")


STAGES = ["new", "contacted", "confirmed_externally", "closed", "cancelled"]
TERMINAL = ["closed", "cancelled"]


def test_stage_transitions():
    check_transition(STAGES, TERMINAL, "new", "contacted")
    check_transition(STAGES, TERMINAL, "contacted", "new")  # reopening a live item is fine
    with pytest.raises(ValidationFailed):
        check_transition(STAGES, TERMINAL, "new", "booked")
    with pytest.raises(Conflict, match="already"):
        check_transition(STAGES, TERMINAL, "new", "new")
    with pytest.raises(Conflict, match="closed"):
        check_transition(STAGES, TERMINAL, "closed", "contacted")
