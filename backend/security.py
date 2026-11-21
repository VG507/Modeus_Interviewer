import hashlib
import hmac
import re
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    parsed = re.fullmatch(r"scrypt\$([0-9a-f]{32})\$([0-9a-f]{128})", stored)
    if parsed is None:
        return False
    salt, expected = parsed.groups()
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(digest, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
