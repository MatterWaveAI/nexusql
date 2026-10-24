"""
nexusql.crypto: field encryption, key rotation, blind index, masking, erase.

The encryption tests need the optional ``cryptography`` package and skip
without it; blind_index, mask and erase are stdlib-only and always run.
erase() is exercised here against SQLite, which needs no server; the other
backends are covered by tests/integration/test_erase.py.
"""

import importlib
import sys

import pytest

import nexusql
import nexusql.crypto as crypto
from nexusql import (
    CRYPTOGRAPHY_AVAILABLE,
    DatabaseManager,
    DecryptionError,
    FieldEncryptor,
    KeyRing,
    blind_index,
    mask,
    token_key_id,
)

requires_crypto = pytest.mark.skipif(
    not CRYPTOGRAPHY_AVAILABLE, reason="cryptography not installed (pip install nexusql[crypto])"
)


@pytest.fixture
def key() -> str:
    return FieldEncryptor.generate_key()


# ---------------------------------------------------------------------------
# FieldEncryptor
# ---------------------------------------------------------------------------

@requires_crypto
class TestFieldEncryptor:
    def test_round_trip_str(self, key):
        enc = FieldEncryptor(key)
        token = enc.encrypt("Grüße, 世界 — ünïcode")
        assert enc.decrypt(token) == "Grüße, 世界 — ünïcode"

    def test_round_trip_bytes(self, key):
        enc = FieldEncryptor(key)
        payload = bytes(range(256))
        token = enc.encrypt(payload)
        assert enc.decrypt_bytes(token) == payload

    def test_empty_value(self, key):
        enc = FieldEncryptor(key)
        assert enc.decrypt(enc.encrypt("")) == ""

    def test_token_format(self, key):
        enc = FieldEncryptor(key, key_id="prod-2026.1")
        token = enc.encrypt("x")
        version, key_id, payload = token.split(":")
        assert version == "v1"
        assert key_id == "prod-2026.1"
        assert payload and all(ch.isalnum() or ch in "-_=" for ch in payload)
        assert token_key_id(token) == "prod-2026.1"

    def test_same_value_gives_different_tokens(self, key):
        enc = FieldEncryptor(key)
        assert enc.encrypt("same") != enc.encrypt("same")

    def test_tamper_detected(self, key):
        enc = FieldEncryptor(key)
        token = enc.encrypt("do not touch")
        head, payload = token.rsplit(":", 1)
        # Flip one character inside the ciphertext body.
        pos = len(payload) // 2
        flipped = ("A" if payload[pos] != "A" else "B")
        tampered = f"{head}:{payload[:pos]}{flipped}{payload[pos + 1:]}"
        with pytest.raises(DecryptionError):
            enc.decrypt(tampered)

    def test_header_tamper_detected(self, key):
        # Same key under two ids: a token re-labelled with the other id must
        # fail even though the key would decrypt the body.
        a = FieldEncryptor(key, key_id="a")
        b = FieldEncryptor(key, key_id="b")
        token = a.encrypt("payload")
        relabelled = token.replace("v1:a:", "v1:b:", 1)
        with pytest.raises(DecryptionError):
            b.decrypt(relabelled)

    def test_wrong_key_fails(self, key):
        token = FieldEncryptor(key).encrypt("secret")
        other = FieldEncryptor(FieldEncryptor.generate_key())
        with pytest.raises(DecryptionError):
            other.decrypt(token)

    def test_wrong_key_id_fails(self, key):
        token = FieldEncryptor(key, key_id="k1").encrypt("secret")
        with pytest.raises(DecryptionError, match="k1"):
            FieldEncryptor(key, key_id="k2").decrypt(token)

    @pytest.mark.parametrize("bad", [
        "", "v1", "v1:k1", "v1:k1:", "v0:k1:AAAA", "v1:k1:!!!", "v1:k1:AAAA",
        "v1:bad id:AAAA", "plaintext",
    ])
    def test_malformed_tokens(self, key, bad):
        with pytest.raises(DecryptionError):
            FieldEncryptor(key).decrypt(bad)

    def test_non_str_token(self, key):
        with pytest.raises(DecryptionError):
            FieldEncryptor(key).decrypt(None)  # type: ignore[arg-type]

    def test_binary_plaintext_via_decrypt_raises(self, key):
        enc = FieldEncryptor(key)
        token = enc.encrypt(b"\xff\xfe\x00")
        with pytest.raises(DecryptionError, match="decrypt_bytes"):
            enc.decrypt(token)

    def test_key_formats_are_equivalent(self):
        import base64
        raw = bytes(range(32))
        encryptors = [
            FieldEncryptor(raw),
            FieldEncryptor(raw.hex()),
            FieldEncryptor(raw.hex().upper()),
            FieldEncryptor(base64.b64encode(raw).decode()),
            FieldEncryptor(base64.urlsafe_b64encode(raw).decode().rstrip("=")),
        ]
        token = encryptors[0].encrypt("shared")
        for enc in encryptors[1:]:
            assert enc.decrypt(token) == "shared"

    @pytest.mark.parametrize("bad", [b"short", b"x" * 33, "abc", "00" * 31, 12345])
    def test_bad_keys_rejected(self, bad):
        with pytest.raises((ValueError, TypeError)):
            FieldEncryptor(bad)

    @pytest.mark.parametrize("bad", ["", "a:b", "with space", "x" * 65])
    def test_bad_key_ids_rejected(self, key, bad):
        with pytest.raises(ValueError):
            FieldEncryptor(key, key_id=bad)

    def test_generate_key_is_32_bytes_and_random(self):
        a, b = FieldEncryptor.generate_key(), FieldEncryptor.generate_key()
        assert a != b
        assert len(crypto._decode_key(a)) == 32

    def test_non_text_value_rejected(self, key):
        with pytest.raises(TypeError):
            FieldEncryptor(key).encrypt(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# KeyRing
# ---------------------------------------------------------------------------

@requires_crypto
class TestKeyRing:
    @pytest.fixture
    def ring(self):
        k1 = FieldEncryptor(FieldEncryptor.generate_key(), key_id="k1")
        k2 = FieldEncryptor(FieldEncryptor.generate_key(), key_id="k2")
        return k1, k2, KeyRing([k1, k2], active="k2")

    def test_encrypts_with_active(self, ring):
        _, _, r = ring
        assert token_key_id(r.encrypt("v")) == "k2"
        assert r.active_key_id == "k2"
        assert sorted(r.key_ids) == ["k1", "k2"]

    def test_decrypts_old_and_new(self, ring):
        k1, k2, r = ring
        assert r.decrypt(k1.encrypt("old")) == "old"
        assert r.decrypt(k2.encrypt("new")) == "new"
        assert r.decrypt_bytes(k1.encrypt(b"\x00\x01")) == b"\x00\x01"

    def test_rotate(self, ring):
        k1, k2, r = ring
        old = k1.encrypt("rotate me")
        assert r.needs_rotation(old)
        new = r.rotate(old)
        assert token_key_id(new) == "k2"
        assert not r.needs_rotation(new)
        assert k2.decrypt(new) == "rotate me"
        # Idempotent: already-active tokens come back untouched.
        assert r.rotate(new) == new

    def test_unknown_key_id(self, ring):
        _, _, r = ring
        stray = FieldEncryptor(FieldEncryptor.generate_key(), key_id="k9").encrypt("x")
        with pytest.raises(DecryptionError, match="k9"):
            r.decrypt(stray)

    def test_bad_ring(self):
        k1 = FieldEncryptor(FieldEncryptor.generate_key(), key_id="k1")
        with pytest.raises(ValueError):
            KeyRing([k1], active="missing")
        with pytest.raises(ValueError):
            KeyRing([k1, FieldEncryptor(FieldEncryptor.generate_key(), key_id="k1")], active="k1")


# ---------------------------------------------------------------------------
# blind_index (stdlib only)
# ---------------------------------------------------------------------------

class TestBlindIndex:
    def test_equality_and_determinism(self):
        a = blind_index("ada@example.com", b"index-key")
        b = blind_index("ada@example.com", b"index-key")
        assert a == b
        assert len(a) == 16
        assert all(ch in "0123456789abcdef" for ch in a)

    def test_normalisation(self):
        base = blind_index("ada@example.com", "k")
        assert blind_index("  Ada@Example.COM ", "k") == base
        # NFKC folds the full-width form to ASCII.
        assert blind_index("ａda@example.com", "k") == base
        assert blind_index("Ada@Example.COM", "k", normalize=False) != base

    def test_key_and_value_sensitivity(self):
        assert blind_index("v", "k1") != blind_index("v", "k2")
        assert blind_index("v1", "k") != blind_index("v2", "k")

    def test_length_and_bytes(self):
        assert len(blind_index("v", "k", length=64)) == 64
        assert blind_index(b"raw", b"k") == blind_index("raw", "k", normalize=False)
        for bad in (0, 65):
            with pytest.raises(ValueError):
                blind_index("v", "k", length=bad)

    def test_bad_inputs(self):
        with pytest.raises(ValueError):
            blind_index("v", b"")
        with pytest.raises(TypeError):
            blind_index(1, "k")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# mask (stdlib only)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, kind, keep, expected", [
    ("john@example.com", "email", None, "j***@example.com"),
    ("john@example.com", "email", 2, "jo***@example.com"),
    ("j@example.com", "email", None, "***@example.com"),
    ("not-an-email", "email", None, "n***********"),
    ("+1 (555) 123-1234", "phone", None, "****1234"),
    ("555-1234", "phone", 2, "****34"),
    ("123", "phone", None, "****"),
    ("4111 1111 1111 1234", "card", None, "****1234"),
    ("4111111111111234", "card", 0, "****"),
    ("John Doe", "name", None, "J*** D**"),
    ("John Ronald Reuel Tolkien", "name", 2, "Jo** Ro**** Re*** To*****"),
    ("Jo", "name", 2, "**"),
    ("secret", "generic", None, "s*****"),
    ("secret", "generic", 3, "sec***"),
    ("ab", "generic", 5, "**"),
    ("", "email", None, ""),
    (None, "card", None, None),
    (12345678, "generic", 2, "12******"),
])
def test_mask_table(value, kind, keep, expected):
    assert mask(value, kind, keep) == expected


def test_mask_rejects_bad_kind_and_keep():
    with pytest.raises(ValueError):
        mask("x", kind="ssn")
    with pytest.raises(ValueError):
        mask("x", keep=-1)


# ---------------------------------------------------------------------------
# erase on SQLite
# ---------------------------------------------------------------------------

@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect()
    manager.execute(
        "CREATE TABLE people (id SERIAL PRIMARY KEY, org INTEGER, email TEXT, phone TEXT, notes TEXT)"
    )
    for i in range(1, 5):
        manager.execute(
            "INSERT INTO people (org, email, phone, notes) VALUES (:org, :email, :phone, :notes)",
            {"org": 1 if i <= 2 else 2, "email": f"p{i}@x.io", "phone": f"555-000{i}", "notes": f"n{i}"},
        )
    yield manager
    manager.disconnect()


class TestErase:
    def test_nulls_columns_and_counts(self, db):
        count = db.erase("people", "org = :org", {"org": 1}, columns=["email", "phone"])
        assert count == 2
        rows = db.execute("SELECT id, email, phone, notes FROM people ORDER BY id")
        assert rows[0] == {"id": 1, "email": None, "phone": None, "notes": "n1"}
        assert rows[1] == {"id": 2, "email": None, "phone": None, "notes": "n2"}
        # Rows outside the WHERE are untouched.
        assert rows[2] == {"id": 3, "email": "p3@x.io", "phone": "555-0003", "notes": "n3"}

    def test_tombstone(self, db):
        count = db.erase("people", "id = :id", {"id": 4}, columns=["email"], tombstone="[erased]")
        assert count == 1
        assert db.fetch_one("SELECT email, phone FROM people WHERE id = 4") == {
            "email": "[erased]", "phone": "555-0004",
        }

    def test_no_match_returns_zero(self, db):
        assert db.erase("people", "id = :id", {"id": 999}, columns=["email"]) == 0

    def test_single_column_as_str(self, db):
        assert db.erase("people", "id = :id", {"id": 1}, columns="email") == 1

    def test_refuses_empty_where(self, db):
        with pytest.raises(ValueError, match="WHERE"):
            db.erase("people", "", None, columns=["email"])
        with pytest.raises(ValueError, match="WHERE"):
            db.erase("people", "   ", None, columns=["email"])

    def test_refuses_no_columns(self, db):
        with pytest.raises(ValueError, match="column"):
            db.erase("people", "id = :id", {"id": 1}, columns=[])

    @pytest.mark.parametrize("bad", [
        "people; DROP TABLE people", 'people"', "people--", "a.b.c", "1people", "", "peo ple",
    ])
    def test_rejects_bad_identifiers(self, db, bad):
        with pytest.raises(ValueError, match="identifier"):
            db.erase(bad, "id = :id", {"id": 1}, columns=["email"])
        with pytest.raises(ValueError, match="identifier"):
            db.erase("people", "id = :id", {"id": 1}, columns=[bad])
        assert db.table_exists("people")

    def test_reserved_param_name(self, db):
        with pytest.raises(ValueError, match="reserved"):
            db.erase("people", "id = :nexusql_erase_tombstone",
                     {"nexusql_erase_tombstone": 1}, columns=["email"])

    def test_values_never_interpolated(self, db):
        # A hostile value travels as a bound parameter and matches nothing.
        count = db.erase("people", "email = :email", {"email": "' OR 1=1 --"}, columns=["email"])
        assert count == 0
        assert db.fetch_one("SELECT COUNT(*) AS n FROM people WHERE email IS NULL") == {"n": 0}

    def test_failure_rolls_back_and_connection_usable(self, db):
        with pytest.raises(Exception):
            db.erase("people", "no_such_column = :v", {"v": 1}, columns=["email"])
        assert db.fetch_one("SELECT COUNT(*) AS n FROM people WHERE email IS NOT NULL") == {"n": 4}

    def test_joins_outer_transaction(self, db):
        class Abort(Exception):
            pass
        with pytest.raises(Abort):
            with db.transaction():
                assert db.erase("people", "org = :org", {"org": 2}, columns=["email"]) == 2
                raise Abort
        assert db.fetch_one("SELECT COUNT(*) AS n FROM people WHERE email IS NULL") == {"n": 0}


# ---------------------------------------------------------------------------
# import without cryptography
# ---------------------------------------------------------------------------

def test_import_without_cryptography(monkeypatch):
    # Pretend the package is absent: None in sys.modules makes the import
    # raise ImportError. Reload the module under that condition, then reload
    # again after the monkeypatch is undone so later tests see the real state.
    for name in list(sys.modules):
        if name == "cryptography" or name.startswith("cryptography."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    try:
        reloaded = importlib.reload(crypto)
        assert reloaded.CRYPTOGRAPHY_AVAILABLE is False
        importlib.reload(nexusql)
        assert nexusql.CRYPTOGRAPHY_AVAILABLE is False
        # The stdlib helpers keep working.
        assert nexusql.mask("john@example.com", "email") == "j***@example.com"
        assert len(nexusql.blind_index("v", "k")) == 16
        with pytest.raises(ImportError, match=r"nexusql\[crypto\]"):
            reloaded.FieldEncryptor(b"\x00" * 32)
    finally:
        monkeypatch.undo()
        importlib.reload(crypto)
        importlib.reload(nexusql)
    assert nexusql.CRYPTOGRAPHY_AVAILABLE is CRYPTOGRAPHY_AVAILABLE
