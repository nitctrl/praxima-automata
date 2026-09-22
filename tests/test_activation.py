"""Offline activation guards and explicitly opted-in, rollback-only SQL tests.

Never invoke the activation CLI/register action or Supabase Auth API. Auth rows
below exist only inside conftest's nested force_rollback transactions.
"""

import base64
import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from psycopg.pq import TransactionStatus

from clinic import activation
from clinic.development import fixture_id
from clinic.settings import ConfigurationError
from clinic.snapshot import Snapshot

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "a" * 20
A = fixture_id("A")
KEY = base64.b64encode(b"k" * 32).decode()
TEST_ROUTE = ("test", "fixture-only-not-a-live-trunk")


def private_file(root, **overrides):
    values = {
        "SUPABASE_PROJECT_REF": PROJECT,
        "CLINIC_ENVIRONMENT": "development",
        "CLINIC_TEST_CLINIC_ID": str(A),
        "CLINIC_PII_KEY_VERSION": "dev-v1",
        "CLINIC_PII_KEYS": json.dumps({"dev-v1": KEY}),
    }
    values.update(overrides)
    path = root / activation.DEV_FILE
    path.write_text("".join(f"{name}='{value}'\n" for name, value in values.items()))
    path.chmod(0o600)
    return path


def test_provision_keys_creates_private_scoped_usable_key(tmp_path, capsys):
    activation.provision_keys(tmp_path, PROJECT)
    path = tmp_path / activation.DEV_FILE
    info = path.stat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid()
    clinic, cipher = activation.load_development(tmp_path, PROJECT)
    assert clinic == A
    assert cipher.current == "dev-v1"
    assert set(cipher.keys) == {"dev-v1"}
    assert len(cipher.keys["dev-v1"]) == 32
    resource = uuid4()
    encrypted = cipher.encrypt("Fictional caller", clinic, resource, "name")
    assert cipher.decrypt(encrypted, cipher.current, clinic, resource, "name") == "Fictional caller"
    assert capsys.readouterr() == ("", "")


def test_provision_keys_does_not_rotate_overwrite_or_discard_old_versions(tmp_path, monkeypatch):
    path = private_file(
        tmp_path,
        CLINIC_PII_KEYS=json.dumps({"old-v1": KEY, "dev-v1": KEY}),
    )
    contents, info = path.read_bytes(), path.stat()
    random = Mock(side_effect=AssertionError("Existing keys must not be regenerated"))
    monkeypatch.setattr(activation.os, "urandom", random)
    activation.provision_keys(tmp_path, PROJECT)
    activation.provision_keys(tmp_path, PROJECT)
    assert path.read_bytes() == contents
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        info.st_ino,
        info.st_mtime_ns,
        info.st_mode,
    )
    assert set(activation.load_development(tmp_path, PROJECT)[1].keys) == {"old-v1", "dev-v1"}
    random.assert_not_called()


def test_provision_keys_exclusive_create_does_not_overwrite_racing_file(tmp_path, monkeypatch):
    original_open = os.open
    path = tmp_path / activation.DEV_FILE

    def racing_open(filename, flags, mode=0o777):
        assert flags & os.O_EXCL
        assert flags & os.O_NOFOLLOW
        path.write_text("Concurrent writer's artifact\n")
        return original_open(filename, flags, mode)

    monkeypatch.setattr(activation.os, "open", racing_open)
    with pytest.raises(FileExistsError):
        activation.provision_keys(tmp_path, PROJECT)
    assert path.read_text() == "Concurrent writer's artifact\n"


@pytest.mark.parametrize("operation", [activation.load_development, activation.provision_keys])
@pytest.mark.parametrize("dangling", [False, True], ids=["existing-target", "dangling"])
def test_private_file_rejects_symlinks_without_touching_target(tmp_path, operation, dangling):
    target = tmp_path / "target"
    if not dangling:
        target.write_text("Do not overwrite\n")
        target.chmod(0o600)
    path = tmp_path / activation.DEV_FILE
    path.symlink_to(target)
    with pytest.raises(OSError) as error:
        operation(tmp_path, PROJECT)
    assert error.value.errno == errno.ELOOP
    assert path.is_symlink()
    if dangling:
        assert not target.exists()
    else:
        assert target.read_text() == "Do not overwrite\n"


@pytest.mark.parametrize("operation", [activation.load_development, activation.provision_keys])
@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644, 0o660, 0o700])
def test_private_file_rejects_incorrect_mode_without_repair(tmp_path, operation, mode):
    path = private_file(tmp_path)
    before = path.read_bytes()
    path.chmod(mode)
    with pytest.raises(ConfigurationError, match="owner-only mode 0600"):
        operation(tmp_path, PROJECT)
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_private_file_rejects_foreign_owner(tmp_path, monkeypatch):
    private_file(tmp_path)
    monkeypatch.setattr(activation.os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(ConfigurationError, match="owner-only mode 0600"):
        activation.load_development(tmp_path, PROJECT)


def test_private_file_rejects_nonregular_descriptor(tmp_path, monkeypatch):
    private_file(tmp_path)
    real_fstat = os.fstat

    def nonregular_fstat(fd):
        fields = list(real_fstat(fd))
        fields[0] = stat.S_IFIFO | 0o600
        return os.stat_result(fields)

    # Do not open a real FIFO: the helper's blocking open could hang the suite.
    monkeypatch.setattr(activation.os, "fstat", nonregular_fstat)
    with pytest.raises(ConfigurationError, match="owner-only mode 0600"):
        activation.load_development(tmp_path, PROJECT)


@pytest.mark.parametrize("operation", [activation.load_development, activation.provision_keys])
@pytest.mark.parametrize(
    "overrides",
    [
        {"SUPABASE_PROJECT_REF": "b" * 20},
        {"CLINIC_ENVIRONMENT": "production"},
        {"CLINIC_ENVIRONMENT": ""},
        {"CLINIC_TEST_CLINIC_ID": str(fixture_id("B"))},
        {"CLINIC_TEST_CLINIC_ID": "not-a-uuid"},
    ],
    ids=["project", "production", "missing-environment", "clinic-B", "invalid-clinic"],
)
def test_private_file_rejects_scope_mismatch_without_overwrite(tmp_path, operation, overrides):
    path = private_file(tmp_path, **overrides)
    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match="scope mismatch"):
        operation(tmp_path, PROJECT)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "keys,version",
    [
        ("", "dev-v1"),
        ("not-json", "dev-v1"),
        ("null", "dev-v1"),
        ("[]", "dev-v1"),
        ("{}", "dev-v1"),
        (json.dumps({"dev-v1": KEY}), ""),
        (json.dumps({"dev-v1": KEY}), "missing"),
        (json.dumps({"dev-v1": "%%%"}), "dev-v1"),
        (json.dumps({"dev-v1": base64.b64encode(b"k" * 31).decode()}), "dev-v1"),
        (json.dumps({"dev-v1": base64.b64encode(b"k" * 33).decode()}), "dev-v1"),
        (json.dumps({"": KEY}), ""),
        (json.dumps({"v" * 81: KEY}), "v" * 81),
        (json.dumps({"dev-v1": 123}), "dev-v1"),
        (json.dumps({"dev-v1": None}), "dev-v1"),
        (json.dumps({"dev-v1": KEY, "old": "invalid"}), "dev-v1"),
    ],
    ids=[
        "empty",
        "bad-json",
        "null",
        "list",
        "empty-map",
        "missing-version",
        "unknown-version",
        "bad-base64",
        "short-key",
        "long-key",
        "empty-name",
        "long-name",
        "numeric-value",
        "null-value",
        "invalid-old-key",
    ],
)
def test_invalid_key_contents_rejected_without_regeneration(tmp_path, keys, version):
    path = private_file(tmp_path, CLINIC_PII_KEYS=keys, CLINIC_PII_KEY_VERSION=version)
    before = path.read_bytes()
    for operation in (activation.load_development, activation.provision_keys):
        with pytest.raises(ConfigurationError, match="Invalid private key configuration") as error:
            operation(tmp_path, PROJECT)
        assert KEY not in str(error.value)
        assert path.read_bytes() == before


@pytest.mark.parametrize("confirmation", ["", "b" * 20])
def test_development_settings_requires_matching_explicit_confirmation(tmp_path, confirmation):
    (tmp_path / ".env").write_text(f"SUPABASE_PROJECT_REF={PROJECT}\n")
    with pytest.raises(ConfigurationError, match="Explicit development project confirmation"):
        activation.development_settings(tmp_path, confirmation)


def test_development_settings_rejects_dsn_for_another_project(tmp_path):
    dsn = (
        f"postgresql://postgres.{'b' * 20}:fake@"
        "aws-0-example.pooler.supabase.com:5432/postgres?sslmode=require"
    )
    (tmp_path / ".env").write_text(
        f"SUPABASE_PROJECT_REF={PROJECT}\nMIGRATION_DATABASE_URL={dsn}\n"
    )
    with pytest.raises(ConfigurationError):
        activation.development_settings(tmp_path, PROJECT)


@pytest.mark.parametrize("clinic", [fixture_id("B"), UUID(int=0)])
def test_require_fictional_rejects_other_clinic_before_query(clinic):
    conn = Mock()
    with pytest.raises(ConfigurationError, match="only activates fictional clinic A"):
        activation.require_fictional(conn, clinic)
    conn.execute.assert_not_called()


@pytest.mark.parametrize(
    "row,phones",
    [
        (None, [TEST_ROUTE]),
        (("fictional-clinic-a",), []),
        (("fictional-clinic-a",), [("plivo", TEST_ROUTE[1])]),
        (("fictional-clinic-a",), [("test", "real-trunk")]),
        (("fictional-clinic-a",), [TEST_ROUTE, ("plivo", "real-trunk")]),
    ],
    ids=["missing-clinic", "no-phones", "live-provider", "live-trunk", "mixed-routes"],
)
def test_require_fictional_rejects_missing_or_live_routes(row, phones):
    conn = Mock()
    conn.execute.side_effect = [
        Mock(fetchone=Mock(return_value=row)),
        Mock(fetchall=Mock(return_value=phones)),
    ]
    with pytest.raises(ConfigurationError, match="Fictional-only route verification failed"):
        activation.require_fictional(conn, A)
    assert all(call.args[1] == (A,) for call in conn.execute.call_args_list)


def test_require_fictional_accepts_only_test_routes_and_locks_clinic():
    conn = Mock()
    conn.execute.side_effect = [
        Mock(fetchone=Mock(return_value=("fictional-clinic-a",))),
        Mock(fetchall=Mock(return_value=[TEST_ROUTE, TEST_ROUTE])),
    ]
    activation.require_fictional(conn, A)
    assert "FOR NO KEY UPDATE" in conn.execute.call_args_list[0].args[0]
    assert all(call.args[1] == (A,) for call in conn.execute.call_args_list)


@pytest.mark.parametrize("count", [0, 2])
def test_verified_user_requires_exactly_one_match(count):
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [(uuid4(),)] * count
    with pytest.raises(ConfigurationError, match="Create and verify"):
        activation.verified_user(conn, "fictional@example.invalid")


@pytest.mark.parametrize("history", ["matching", "missing", "changed", "extra", "empty-checkout"])
def test_verify_history_requires_exact_checkout(tmp_path, history):
    migrations = tmp_path / "supabase" / "migrations"
    migrations.mkdir(parents=True)
    content = b"-- offline fixture\n"
    digest = hashlib.sha256(content).hexdigest()
    if history != "empty-checkout":
        (migrations / "001.sql").write_bytes(content)
    rows = [("001.sql", digest)]
    if history in {"missing", "empty-checkout"}:
        rows = []
    elif history == "changed":
        rows = [("001.sql", "0" * 64)]
    elif history == "extra":
        rows.append(("002.sql", digest))
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = rows
    if history == "matching":
        activation.verify_history(conn, tmp_path)
    else:
        with pytest.raises(ConfigurationError, match="Migration state"):
            activation.verify_history(conn, tmp_path)


@pytest.fixture
def rollback_auth_user(db):
    # db and admin_connection both use force_rollback=True. No Auth API, commit,
    # passwords, or existing account modifications are permitted in these tests.
    assert db.info.transaction_status == TransactionStatus.INTRANS
    user = uuid4()
    email = f"activation-{user}@example.invalid"
    db.execute(
        "INSERT INTO auth.users(id,email,email_confirmed_at) VALUES(%s,%s,now())",
        (user, email),
    )
    return user, email


@pytest.fixture
def vacant_fictional_clinic(db):
    activation.verify_history(db, ROOT)
    activation.require_fictional(db, A)
    if db.execute("SELECT 1 FROM clinic_users WHERE clinic_id=%s", (A,)).fetchone():
        pytest.skip("Bootstrap needs an unclaimed fixture; never remove existing memberships")
    return A


@pytest.mark.integration
@pytest.mark.parametrize("state", ["verified", "unconfirmed", "deleted", "banned", "expired-ban"])
def test_verified_user_database_filters(db, rollback_auth_user, state):
    user, email = rollback_auth_user
    statements = {
        "unconfirmed": "UPDATE auth.users SET email_confirmed_at=NULL WHERE id=%s",
        "deleted": "UPDATE auth.users SET deleted_at=now() WHERE id=%s",
        "banned": "UPDATE auth.users SET banned_until=now()+interval '1 day' WHERE id=%s",
        "expired-ban": "UPDATE auth.users SET banned_until=now()-interval '1 day' WHERE id=%s",
    }
    if state in statements:
        db.execute(statements[state], (user,))
    if state in {"verified", "expired-ban"}:
        assert activation.verified_user(db, email.upper()) == user
    else:
        with pytest.raises(ConfigurationError, match="Create and verify"):
            activation.verified_user(db, email)


@pytest.mark.integration
def test_verified_user_database_missing_account(db):
    with pytest.raises(ConfigurationError, match="Create and verify"):
        activation.verified_user(db, f"missing-{uuid4()}@example.invalid")


@pytest.mark.integration
def test_bootstrap_owner_is_idempotent_audited_and_route_preserving(
    db, rollback_auth_user, vacant_fictional_clinic
):
    user, email = rollback_auth_user
    routes = db.execute("SELECT * FROM phone_numbers ORDER BY id").fetchall()
    for _ in range(2):
        assert activation.bootstrap_owner(db, ROOT, email) == user
    membership = db.execute(
        "SELECT id,auth_user_id,role,status FROM clinic_users WHERE clinic_id=%s", (A,)
    ).fetchall()
    assert len(membership) == 1
    assert membership[0][1:] == (user, "owner", "active")
    assert db.execute(
        "SELECT actor_id,resource_type,resource_id FROM audit_logs "
        "WHERE clinic_id=%s AND action='dev_owner_bootstrap' AND actor_id=%s",
        (A, user),
    ).fetchall() == [(user, "clinic_users", membership[0][0])]
    assert db.execute("SELECT * FROM phone_numbers ORDER BY id").fetchall() == routes


@pytest.mark.integration
@pytest.mark.parametrize(
    "role,status", [("manager", "active"), ("viewer", "active"), ("owner", "inactive")]
)
def test_bootstrap_owner_refuses_role_takeover(
    db, rollback_auth_user, vacant_fictional_clinic, role, status
):
    user, email = rollback_auth_user
    db.execute(
        "INSERT INTO clinic_users(clinic_id,auth_user_id,role,status) VALUES(%s,%s,%s,%s)",
        (A, user, role, status),
    )
    with pytest.raises(ConfigurationError, match="no automatic role takeover"):
        activation.bootstrap_owner(db, ROOT, email)
    assert db.execute(
        "SELECT role,status FROM clinic_users WHERE clinic_id=%s AND auth_user_id=%s", (A, user)
    ).fetchone() == (role, status)
    assert db.execute(
        "SELECT count(*) FROM audit_logs WHERE actor_id=%s AND action='dev_owner_bootstrap'",
        (user,),
    ).fetchone() == (0,)


@pytest.mark.integration
def test_bootstrap_owner_refuses_another_membership(
    db, rollback_auth_user, vacant_fictional_clinic
):
    _, email = rollback_auth_user
    other = uuid4()
    db.execute("INSERT INTO auth.users(id) VALUES(%s)", (other,))
    db.execute(
        "INSERT INTO clinic_users(clinic_id,auth_user_id,role) VALUES(%s,%s,'owner')", (A, other)
    )
    with pytest.raises(ConfigurationError, match="manual review"):
        activation.bootstrap_owner(db, ROOT, email)
    assert db.execute(
        "SELECT auth_user_id,role,status FROM clinic_users WHERE clinic_id=%s", (A,)
    ).fetchall() == [(other, "owner", "active")]


@pytest.mark.integration
def test_publish_fictional_validates_and_reuses_v2_without_route_changes(db, rollback_auth_user):
    user, email = rollback_auth_user
    activation.verify_history(db, ROOT)
    activation.require_fictional(db, A)
    db.execute(
        "INSERT INTO clinic_users(clinic_id,auth_user_id,role) VALUES(%s,%s,'owner')", (A, user)
    )
    routes = db.execute("SELECT * FROM phone_numbers ORDER BY id").fetchall()
    before = db.execute(
        "SELECT id,schema_version FROM configuration_versions "
        "WHERE clinic_id=%s AND status='published'",
        (A,),
    ).fetchone()
    assert before is not None, "Requires an already seeded fictional publication"
    # SET LOCAL ROLE survives a successful helper savepoint until outer rollback.
    # Restore it explicitly so subsequent helper calls can read operator-only data.
    try:
        version = activation.publish_fictional(db, ROOT, email)
    finally:
        db.execute("RESET ROLE")
    if before[1] == 1:
        assert version != before[0]
        assert db.execute(
            "SELECT status FROM configuration_versions WHERE id=%s", (before[0],)
        ).fetchone() == ("superseded",)
    else:
        assert version == before[0]
    row = db.execute(
        "SELECT schema_version,snapshot FROM configuration_versions "
        "WHERE clinic_id=%s AND id=%s AND status='published'",
        (A, version),
    ).fetchone()
    assert row is not None and row[0] == 2
    assert Snapshot.model_validate(row[1]).clinic_id == A
    assert db.execute(
        "SELECT active_configuration_version_id FROM clinics WHERE id=%s", (A,)
    ).fetchone() == (version,)
    versions = db.execute(
        "SELECT id,status FROM configuration_versions WHERE clinic_id=%s ORDER BY id", (A,)
    ).fetchall()
    try:
        assert activation.publish_fictional(db, ROOT, email) == version
    finally:
        db.execute("RESET ROLE")
    assert (
        db.execute(
            "SELECT id,status FROM configuration_versions WHERE clinic_id=%s ORDER BY id", (A,)
        ).fetchall()
        == versions
    )
    assert db.execute("SELECT * FROM phone_numbers ORDER BY id").fetchall() == routes


@pytest.mark.integration
def test_publish_fictional_rejects_verified_nonmember(db, rollback_auth_user):
    _, email = rollback_auth_user
    with pytest.raises(ConfigurationError, match="authorized fictional publication"):
        activation.publish_fictional(db, ROOT, email)
