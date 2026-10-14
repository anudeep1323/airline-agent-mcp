"""
Password hashing with the standard library (scrypt). We store salt + hash, never the password.

  hash_password("hunter2")         -> "scrypt$<salt hex>$<hash hex>"
  verify_password("hunter2", stored) -> True / False
"""
import hashlib
import hmac
import os


def hash_password(password: str) -> str:
    salt = os.urandom(16)                                    # random per user: same password -> different hash
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)   # deliberately slow to brute-force
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, salt_hex, digest_hex = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)     # constant-time compare
