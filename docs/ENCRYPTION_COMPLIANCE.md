# Encryption and Compliance Helpers

Status: implemented in `nexusql/crypto.py`. Optional extra: `pip install nexusql[crypto]`
(pulls in `cryptography`). `blind_index`, `mask` and `erase` are standard library only
and work without the extra.

## What this is

A small, explicit toolkit for keeping personal data out of the database in clear text
and for answering the compliance questions that follow: how do I search an encrypted
column, how do I rotate the key, how do I show a value in a log without leaking it,
and how do I honour a right-to-erasure request without breaking referential
integrity.

Everything runs in the application process. The database stores and returns opaque
text; it never holds a key and never performs a cryptographic operation. That is a
deliberate choice over database-side features (`pgcrypto`, SQL Server Always
Encrypted, MySQL `AES_ENCRYPT`): it behaves identically on all four backends, keys
never travel over the connection, and a database administrator or a leaked dump
cannot read the protected columns.

Nothing is wired into `DatabaseManager.execute()`. There is no "encrypted column"
declaration, no automatic encrypt-on-write. You call `encrypt()` before the INSERT and
`decrypt()` after the SELECT. Transparent encryption hides where the plaintext
boundary is, and that boundary is exactly what a compliance review needs to see.

## Threat model

What application-side encryption protects against:

- **Database compromise**: a stolen backup, a replica snapshot, a read-only
  reporting account, a misconfigured bucket, or a DBA reading tables directly. They
  see `v1:k1:...` tokens and HMAC digests, nothing else.
- **Tampering with stored ciphertext**: AES-GCM authenticates the ciphertext and the
  token header. A modified token, a token moved under a different key id, or a token
  from a different key fails with `DecryptionError` rather than yielding garbage.
- **Log and export leakage of the fields you mask**: `mask()` gives a consistent,
  low-information rendering for support tooling and audit logs.

What it does not protect against:

- **Compromise of the application or its key store.** The application holds the keys
  and decrypts routinely, so an attacker with code execution, or with access to the
  environment or secret manager the key lives in, has everything. Keep keys out of
  the repository and out of the database; load them from a secret manager or the
  environment at startup.
- **Queries other than equality.** Ciphertext cannot be sorted, ranged, pattern-matched
  or aggregated by the database. Only equality lookup is supported, through a blind
  index, and only for values you chose to index.
- **Metadata.** Row counts, write timestamps, which rows share a blind-index value,
  the approximate length of plaintext (ciphertext length = plaintext length + 28
  bytes before base64), and the fact that a field is populated at all are visible.
- **Everything you did not encrypt.** Foreign keys, free-text columns that happen to
  contain personal data, and copies in caches, queues or logs are unaffected.
- **Side channels in your own process**: plaintext in exception messages, debugger
  dumps, or ORM `__repr__` output.

This is defence in depth for data at rest, not a substitute for access control,
TLS between the application and the database, or a key-management policy.

## Field encryption

`FieldEncryptor(key, key_id="k1")` uses AES-256-GCM with a fresh 96-bit random nonce
per value. The key is 32 bytes, accepted as raw bytes, 64 hex characters or base64
(either alphabet). `FieldEncryptor.generate_key()` returns a new key as a base64url
string suitable for an environment variable.

Token format:

    v1:<key_id>:<base64url(nonce || ciphertext || tag)>

- `v1` is the format version, so a future scheme can coexist with stored tokens.
- `key_id` names the key, so a `KeyRing` can pick it without trial decryption. It is
  also bound into the authenticated associated data, which is why relabelling a token
  with another key id is detected.
- Tokens are plain ASCII and fit a `VARCHAR`/`TEXT` column on every backend. Size a
  column for the longest plaintext: roughly `len(key_id) + 4 + ceil((28 + n) * 4 / 3)`
  characters for an `n`-byte plaintext.

`encrypt()` takes `str` (UTF-8) or `bytes`; `decrypt()` returns `str` and
`decrypt_bytes()` returns the raw bytes. Any failure raises `DecryptionError`
(a `ValueError` subclass) with a message that never contains the token or the key.

Random nonces mean the same plaintext encrypts to a different token every time. This
is the right property for confidentiality, and it is why equality search needs the
blind index below rather than comparing ciphertexts. With 96-bit random nonces the
usual guidance is to rotate a key before it has encrypted on the order of 2^32 values.

## Key rotation

```python
ring = KeyRing([
    FieldEncryptor(old_key, key_id="k1"),
    FieldEncryptor(new_key, key_id="k2"),
], active="k2")
```

- `ring.encrypt(v)` writes under `k2`.
- `ring.decrypt(token)` reads with whichever key the token names.
- `ring.needs_rotation(token)` is true for anything not under `k2`.
- `ring.rotate(token)` re-encrypts under `k2`; a token already under `k2` is returned
  unchanged, so the job is idempotent.
- `token_key_id(token)` reads the key id without decrypting, for inventories.

Procedure: deploy the ring with the new key active and the old key still present;
run a background job that selects rows where the token does not start with
`v1:k2:`, rotates and writes them back in batches inside `db.transaction()`; once
no such rows remain, remove the old key from the ring. Nothing in the hot path
changes during rotation.

## Blind index

`blind_index(value, key, length=16, normalize=True)` returns the first `length`
hex characters of HMAC-SHA256 over the (normalised) value. Store it in a second
column next to the ciphertext, index that column, and query it with equality:

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL,        -- v1:k1:... token
    email_idx VARCHAR(16) NOT NULL
);
CREATE INDEX users_email_idx ON users (email_idx);
```

```python
idx = blind_index(email, index_key)
db.fetch_one("SELECT email FROM users WHERE email_idx = :idx", {"idx": idx})
```

Normalisation (NFKC, casefold, strip) makes `" Ada@Example.COM"` and
`"ada@example.com"` index alike, which is almost always what you want for emails,
usernames and identifiers. Pass `normalize=False` when exact bytes matter.

Leakage trade-off, stated plainly: equal plaintexts always produce equal digests.
Anyone who reads the table learns which rows share a value and how often each value
occurs; anyone who also obtains the index key can confirm a guessed value. The
16-hex-character default (64 bits) keeps accidental collisions negligible while
making the digest useless as a reversible fingerprint. Mitigations: use a key
separate from the encryption key, do not blind-index low-cardinality fields (a
boolean, a country code, a birth year) where the distribution itself is the secret,
and treat the index key with the same care as the encryption key.

## Masking

`mask(value, kind, keep=None)` is presentation-level redaction for logs, exports and
support screens. It is not encryption and the output is still personal data if the
kept part identifies someone.

| kind      | keep means                            | default | example                       |
|-----------|---------------------------------------|---------|-------------------------------|
| `email`   | leading chars of the local part       | 1       | `j***@example.com`            |
| `phone`   | trailing digits, formatting dropped   | 4       | `****1234`                    |
| `card`    | trailing digits, formatting dropped   | 4       | `****1234`                    |
| `name`    | leading letters of each word          | 1       | `J*** D**`                    |
| `generic` | leading characters                    | 1       | `s*****`                      |

Values too short to keep anything are fully masked rather than returned in clear.
`None` passes through so nullable columns need no guard. Phone and card use a
fixed `****` prefix so the length of the original is not revealed.

## GDPR erasure

`db.erase(table, where, params, columns, tombstone=None) -> int` blanks the named
columns for the rows the WHERE clause selects and returns the number of rows the
database reports as updated. It is a single `UPDATE` inside `db.transaction()`, so it
joins an enclosing transaction and rolls back with it.

```python
removed = db.erase(
    "users", "id = :id", {"id": 42},
    columns=["email", "email_idx", "phone", "full_name"],
    tombstone="[erased]",
)
```

Why update rather than delete: foreign keys, order history, invoices and audit trails
keep their shape; only the personal data goes. Use the `tombstone` for `NOT NULL`
columns or where an explicit marker is wanted; without it the columns become `NULL`.
Remember to erase the blind-index column as well as the ciphertext, and any
plaintext copies elsewhere.

Safety properties:

- `table` and each column must match `^[A-Za-z_][A-Za-z0-9_]{0,127}$` (table may be
  `schema.table`) and are quoted for the backend (backticks on MySQL, double quotes
  elsewhere). Anything else raises `ValueError` before any SQL runs.
- `where` is your SQL with `:name` placeholders; `params` supplies the values, which
  travel to the driver as bound parameters. No value is interpolated.
- An empty or whitespace WHERE is refused. Erasing a whole table must be spelled out
  (`where="1 = 1"`).
- The parameter name `nexusql_erase_tombstone` is reserved for the tombstone value.

Row count caveat: MySQL reports rows whose values actually changed, so a row that was
already blank is not counted there. SQLite, PostgreSQL and SQL Server report matched
rows.

Erasure does not remove ciphertext from backups or replicas taken before the UPDATE.
Deleting (or never retaining) the key is the only way to make previously stored
ciphertext unrecoverable; per-subject keys are out of scope here but compose with
`KeyRing` if you need them.

## Not included, and why

- **Automatic encryption of declared columns.** It hides the plaintext boundary and
  would need per-backend type handling in the hot path shared by twelve other
  features. The explicit calls are a few lines and make reviews easier.
- **Deterministic encryption** (AES-SIV) for searchable ciphertext. The blind index
  gives the same search capability with the same leakage and a simpler, separately
  keyed primitive.
- **Key derivation from passwords.** Keys here are random 32-byte secrets from a
  secret manager; deriving them from a passphrase is a separate concern (Argon2 or
  scrypt) with its own parameters to get right.
- **Database-side encryption** (`pgcrypto` and friends). Backend-specific, and the key
  would have to cross the connection.
