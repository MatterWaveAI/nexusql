"""
Column-level encryption and compliance helpers.

Everything here runs in the application. The database only ever stores and
returns ciphertext, blind-index digests and whatever the caller writes, so a
dump of the tables, a replica, or a read-only account cannot recover the
protected values without the keys the application holds.

Nothing in this module is wired into ``DatabaseManager.execute()``: the
caller decides which columns are protected and encrypts before writing and
decrypts after reading. Docs: docs/ENCRYPTION_COMPLIANCE.md.

The AES-GCM primitive comes from the ``cryptography`` package, which is an
optional extra (``pip install nexusql[crypto]``). ``blind_index``, ``mask``
and ``erase`` use only the standard library and work without it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import unicodedata
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # the crypto extra is not installed
    AESGCM = None  # type: ignore[assignment,misc]
    CRYPTOGRAPHY_AVAILABLE = False

if TYPE_CHECKING:
    from .manager import DatabaseManager

__all__ = [
    'CRYPTOGRAPHY_AVAILABLE',
    'DecryptionError',
    'FieldEncryptor',
    'KeyRing',
    'blind_index',
    'erase',
    'mask',
    'token_key_id',
]


_INSTALL_HINT = (
    "Field encryption needs the 'cryptography' package; "
    "install it with: pip install nexusql[crypto]"
)

# The token carries a format version so a future scheme can live beside
# tokens already stored, and the key id so a KeyRing can pick the right key
# without trial decryption.
TOKEN_VERSION = "v1"
_TOKEN_SEPARATOR = ":"

# Key ids travel inside the token, so they must not contain the separator and
# are kept to a conservative alphabet.
_KEY_ID = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')

# AES-256 key size and the 96-bit nonce NIST recommends for GCM.
_KEY_BYTES = 32
_NONCE_BYTES = 12

# One SQL identifier (unquoted form) for erase(). Deliberately strict: the
# names are interpolated into the UPDATE, so anything beyond a plain
# identifier is refused rather than quoted around.
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')

# Parameter name erase() adds for the tombstone value; chosen so it cannot
# plausibly collide with a caller's own parameters.
_TOMBSTONE_PARAM = "nexusql_erase_tombstone"


class DecryptionError(ValueError):
    """
    A token could not be decrypted.

    Raised for a token that was tampered with, encrypted under a different
    key, malformed, or that names a key id the KeyRing does not hold. The
    message never includes the token or key material.
    """


# --- keys ----------------------------------------------------------------------

def _decode_key(key: bytes | str) -> bytes:
    """
    Accept a key as 32 raw bytes, 64 hex characters, or base64 (either
    alphabet, padding optional) of 32 bytes.
    """
    if isinstance(key, (bytes, bytearray, memoryview)):
        raw = bytes(key)
        if len(raw) != _KEY_BYTES:
            raise ValueError(
                f"AES-256 key must be {_KEY_BYTES} bytes, got {len(raw)}"
            )
        return raw

    if not isinstance(key, str):
        raise TypeError(f"key must be bytes or str, got {type(key).__name__}")

    text = key.strip()
    if len(text) == _KEY_BYTES * 2:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass  # not hex; fall through to base64

    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("key string is neither hex nor base64") from exc
    if len(raw) != _KEY_BYTES:
        raise ValueError(
            f"AES-256 key must decode to {_KEY_BYTES} bytes, got {len(raw)}"
        )
    return raw


def _check_key_id(key_id: str) -> str:
    if not isinstance(key_id, str) or not _KEY_ID.match(key_id):
        raise ValueError(
            "key_id must be 1-64 characters from [A-Za-z0-9_.-]; "
            f"got {key_id!r}"
        )
    return key_id


def _split_token(token: str) -> Tuple[str, str, str]:
    """Split ``v1:<key_id>:<payload>`` or raise DecryptionError."""
    if not isinstance(token, str):
        raise DecryptionError(f"token must be a str, got {type(token).__name__}")
    parts = token.split(_TOKEN_SEPARATOR, 2)
    if len(parts) != 3 or not all(parts):
        raise DecryptionError("malformed token: expected 'v1:<key_id>:<payload>'")
    version, key_id, payload = parts
    if version != TOKEN_VERSION:
        raise DecryptionError(f"unsupported token version {version!r}")
    if not _KEY_ID.match(key_id):
        raise DecryptionError("malformed token: bad key id")
    return version, key_id, payload


def token_key_id(token: str) -> str:
    """
    The key id a token was encrypted under, without decrypting it.

    Useful for inventories ("how many rows still use k1?") before a
    rotation. Raises DecryptionError for a malformed token.
    """
    return _split_token(token)[1]


# --- field encryption ----------------------------------------------------------

class FieldEncryptor:
    """
    Encrypt and decrypt individual values with AES-256-GCM.

    Each call to ``encrypt`` draws a fresh random nonce, so encrypting the
    same value twice yields different tokens; equality search over an
    encrypted column therefore needs a ``blind_index``. Tokens are text
    (``v1:<key_id>:<base64url(nonce + ciphertext)>``) so they fit a VARCHAR
    or TEXT column on every backend and the key id and format version
    travel with the value.

    The key id is bound into the authenticated data, so a token whose header
    was edited to name another key is rejected rather than decrypted under
    the wrong key.

        key = FieldEncryptor.generate_key()   # store in a secret manager
        enc = FieldEncryptor(key, key_id="k1")
        token = enc.encrypt("4111 1111 1111 1111")
        enc.decrypt(token)                      # '4111 1111 1111 1111'

    Requires the ``cryptography`` package (``pip install nexusql[crypto]``).
    """

    def __init__(self, key: bytes | str, key_id: str = "k1"):
        if not CRYPTOGRAPHY_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        self.key_id = _check_key_id(key_id)
        self._aead = AESGCM(_decode_key(key))

    @staticmethod
    def generate_key() -> str:
        """
        A fresh random AES-256 key as a base64url string.

        A string rather than bytes because keys are normally carried in an
        environment variable or secret store; ``FieldEncryptor`` accepts it
        back as-is.
        """
        return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode("ascii")

    def _associated_data(self) -> bytes:
        return f"{TOKEN_VERSION}{_TOKEN_SEPARATOR}{self.key_id}".encode("ascii")

    def encrypt(self, value: str | bytes) -> str:
        """
        Encrypt ``value`` and return a token string.

        A str is encoded as UTF-8; ``decrypt`` returns it as str and
        ``decrypt_bytes`` as the raw bytes.
        """
        if isinstance(value, str):
            plaintext = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            plaintext = bytes(value)
        else:
            raise TypeError(
                f"value must be str or bytes, got {type(value).__name__}"
            )
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext, self._associated_data())
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return _TOKEN_SEPARATOR.join((TOKEN_VERSION, self.key_id, payload))

    def decrypt_bytes(self, token: str) -> bytes:
        """
        Decrypt a token and return the plaintext bytes.

        Raises DecryptionError if the token is malformed, names a different
        key id, was altered, or was encrypted under another key.
        """
        _, key_id, payload = _split_token(token)
        if key_id != self.key_id:
            raise DecryptionError(
                f"token was encrypted under key {key_id!r}, this encryptor holds {self.key_id!r}"
            )
        try:
            blob = base64.urlsafe_b64decode(payload)
        except (binascii.Error, ValueError) as exc:
            raise DecryptionError("malformed token: payload is not base64url") from exc
        if len(blob) <= _NONCE_BYTES:
            raise DecryptionError("malformed token: payload too short")
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, self._associated_data())
        except Exception as exc:
            # cryptography raises InvalidTag; wrap it so callers depend on one
            # exception type and never see partial plaintext.
            raise DecryptionError("authentication failed: token altered or wrong key") from exc

    def decrypt(self, token: str) -> str:
        """Decrypt a token produced from a str and return that str."""
        plaintext = self.decrypt_bytes(token)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecryptionError(
                "plaintext is not UTF-8; use decrypt_bytes() for binary values"
            ) from exc


class KeyRing:
    """
    Several encryptors, one of which is active, for key rotation.

    New values are encrypted under the active key; any token is decrypted
    with the key its header names, so old rows stay readable after a new key
    is introduced. Rotation is then a background job: read each token,
    ``rotate`` it, write it back, and retire the old key once
    ``needs_rotation`` is false everywhere.

        ring = KeyRing([
            FieldEncryptor(old_key, key_id="k1"),
            FieldEncryptor(new_key, key_id="k2"),
        ], active="k2")
    """

    def __init__(self, encryptors: Iterable[FieldEncryptor], active: str):
        self._encryptors: Dict[str, FieldEncryptor] = {}
        for encryptor in encryptors:
            if encryptor.key_id in self._encryptors:
                raise ValueError(f"duplicate key id {encryptor.key_id!r} in KeyRing")
            self._encryptors[encryptor.key_id] = encryptor
        if active not in self._encryptors:
            raise ValueError(
                f"active key {active!r} is not in the ring "
                f"(have {sorted(self._encryptors)})"
            )
        self.active_key_id = active

    @property
    def key_ids(self) -> List[str]:
        """Key ids held by this ring."""
        return list(self._encryptors)

    @property
    def active(self) -> FieldEncryptor:
        """The encryptor new values are written with."""
        return self._encryptors[self.active_key_id]

    def _for_token(self, token: str) -> FieldEncryptor:
        key_id = token_key_id(token)
        try:
            return self._encryptors[key_id]
        except KeyError:
            raise DecryptionError(
                f"token names key {key_id!r}, which this KeyRing does not hold"
            ) from None

    def encrypt(self, value: str | bytes) -> str:
        """Encrypt with the active key."""
        return self.active.encrypt(value)

    def decrypt(self, token: str) -> str:
        """Decrypt with whichever key the token names."""
        return self._for_token(token).decrypt(token)

    def decrypt_bytes(self, token: str) -> bytes:
        """Decrypt with whichever key the token names, returning bytes."""
        return self._for_token(token).decrypt_bytes(token)

    def needs_rotation(self, token: str) -> bool:
        """True if the token is not encrypted under the active key."""
        return token_key_id(token) != self.active_key_id

    def rotate(self, token: str) -> str:
        """
        Re-encrypt a token under the active key.

        A token already under the active key is returned unchanged, so a
        rotation job can be re-run safely.
        """
        if not self.needs_rotation(token):
            return token
        return self.active.encrypt(self.decrypt_bytes(token))


# --- blind index ---------------------------------------------------------------

def blind_index(value: str | bytes, key: bytes | str, length: int = 16,
                normalize: bool = True) -> str:
    """
    Keyed digest of a value for equality search over an encrypted column.

    Store the digest in a second, indexed column next to the ciphertext and
    query it with ``WHERE email_idx = :idx``. The digest is HMAC-SHA256 under
    ``key`` truncated to ``length`` hex characters (16 by default, 64 bits),
    so without the key it cannot be reversed or precomputed from guesses.

    With ``normalize`` the input is NFKC-normalised, case-folded and stripped
    before hashing, so ``" Ada@Example.COM"`` and ``"ada@example.com"`` index
    alike. Pass ``normalize=False`` when the exact bytes matter.

    Leakage trade-off: equal plaintexts always produce the same digest, so an
    attacker holding the table learns which rows share a value and how often
    each value occurs, and can confirm a guess if they also obtain the key.
    That is the price of being searchable; use a key separate from the
    encryption key, and do not blind-index low-entropy fields (a boolean, a
    country code) where the frequency itself is the secret.
    """
    if isinstance(value, str):
        text = value
        if normalize:
            text = unicodedata.normalize("NFKC", text).casefold().strip()
        data = text.encode("utf-8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    else:
        raise TypeError(f"value must be str or bytes, got {type(value).__name__}")

    key_bytes = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if not key_bytes:
        raise ValueError("blind_index key must not be empty")
    if not 1 <= length <= hashlib.sha256().digest_size * 2:
        raise ValueError("length must be between 1 and 64 hex characters")

    return hmac.new(key_bytes, data, hashlib.sha256).hexdigest()[:length]


# --- masking -------------------------------------------------------------------

_MASK_KINDS = ("email", "phone", "card", "name", "generic")
_DEFAULT_KEEP = {"email": 1, "phone": 4, "card": 4, "name": 1, "generic": 1}


def mask(value: Optional[str], kind: str = "generic",
         keep: Optional[int] = None) -> Optional[str]:
    """
    Redact a value for logs, exports and support screens.

    Kinds and what ``keep`` means for each (defaults in brackets):

    - ``email``: keep the first ``keep`` [1] characters of the local part,
      the domain stays: ``j***@example.com``
    - ``phone``, ``card``: keep the last ``keep`` [4] digits, drop
      formatting, fixed-width prefix so the length is not revealed:
      ``****1234``
    - ``name``: keep the first ``keep`` [1] letters of each word:
      ``J*** D**``
    - ``generic``: keep the first ``keep`` [1] characters: ``s*******``

    A value too short to keep anything is fully masked rather than returned
    in clear. ``None`` passes through so optional columns need no guard.
    This is presentation-level redaction, not encryption: the output must
    still be treated as personal data if the kept part identifies someone.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if kind not in _MASK_KINDS:
        raise ValueError(f"kind must be one of {_MASK_KINDS}, got {kind!r}")
    if keep is None:
        keep = _DEFAULT_KEEP[kind]
    if keep < 0:
        raise ValueError("keep must be >= 0")
    if value == "":
        return ""

    if kind == "email":
        local, sep, domain = value.partition("@")
        if not sep:
            return _mask_prefix(value, keep)
        shown = min(keep, max(len(local) - 1, 0))
        return f"{local[:shown]}***@{domain}"

    if kind in ("phone", "card"):
        digits = "".join(ch for ch in value if ch.isdigit())
        if keep == 0 or len(digits) <= keep:
            return "****"
        return "****" + digits[-keep:]

    if kind == "name":
        return " ".join(_mask_prefix(word, keep) for word in value.split(" "))

    return _mask_prefix(value, keep)


def _mask_prefix(text: str, keep: int) -> str:
    """Keep the first ``keep`` characters, star the rest; all stars if too short."""
    if not text:
        return text
    if len(text) <= keep:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep)


# --- GDPR erasure --------------------------------------------------------------

def _quote_identifier(name: str, db_type: Any) -> str:
    """
    Validate and quote one identifier (optionally ``schema.table``).

    Quoting follows the backend: MySQL reads double quotes as string
    literals unless ANSI_QUOTES is on, so it gets backticks; the others take
    the SQL-standard double quotes.
    """
    from .interfaces import DatabaseType

    parts = name.split(".")
    if len(parts) > 2 or not all(_IDENTIFIER.match(part) for part in parts):
        raise ValueError(
            f"invalid identifier {name!r}: expected letters, digits and "
            "underscores, optionally schema-qualified"
        )
    quote = "`" if db_type == DatabaseType.MYSQL else '"'
    return ".".join(f"{quote}{part}{quote}" for part in parts)


def erase(db: "DatabaseManager", table: str, where: str,
          params: Optional[Dict[str, Any]], columns: Sequence[str],
          tombstone: Optional[str] = None) -> int:
    """
    Blank the named columns for the rows a WHERE clause selects.

    Implements right-to-erasure requests without deleting the row, so
    foreign keys, order history and audit trails keep their shape while the
    personal data goes. Each column is set to NULL, or to ``tombstone`` when
    one is given (for NOT NULL columns, or to leave an explicit marker such
    as ``"[erased]"``). Runs inside a transaction and returns the number of
    rows the database reports as updated.

    ``table`` and ``columns`` are validated against a strict identifier
    pattern and quoted. ``where`` is the caller's SQL with ``:name``
    placeholders and ``params`` supplies the values, so no value is ever
    interpolated into the statement. An empty WHERE is refused: erasing a
    whole table must be spelled out (``where="1 = 1"``).

    MySQL reports only rows whose values actually changed, so a row already
    blank is not counted there.
    """
    if not isinstance(where, str) or not where.strip():
        raise ValueError("erase() needs a WHERE clause; refusing to erase every row implicitly")
    if isinstance(columns, str):
        columns = [columns]
    if not columns:
        raise ValueError("erase() needs at least one column")
    if params and _TOMBSTONE_PARAM in params:
        raise ValueError(f"parameter name {_TOMBSTONE_PARAM!r} is reserved by erase()")

    db_type = db.config.database_type
    target = _quote_identifier(table, db_type)
    replacement = "NULL" if tombstone is None else f":{_TOMBSTONE_PARAM}"
    seen: Dict[str, None] = {}
    for column in columns:
        seen.setdefault(_quote_identifier(column, db_type), None)
    assignments = ", ".join(f"{column} = {replacement}" for column in seen)

    statement = f"UPDATE {target} SET {assignments} WHERE {where}"
    bound: Dict[str, Any] = dict(params or {})
    if tombstone is not None:
        bound[_TOMBSTONE_PARAM] = tombstone

    # execute() returns rows rather than a row count, so run the statement
    # on the pinned connection directly and read the cursor's count. The
    # transaction owns the commit (and the rollback if the UPDATE fails).
    with db.transaction():
        with db._borrow() as conn:
            cursor = db._execute_raw(statement, bound, conn)
            try:
                count = cursor.rowcount
            finally:
                cursor.close()
    return int(count) if count is not None and count >= 0 else 0
