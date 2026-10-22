import hashlib
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.fernet import Fernet

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def verify_password(encoded, password):
    try:
        return hasher.verify(encoded or DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False


class Vault:
    def __init__(self, key):
        self.fernet = Fernet(key.encode())

    def protect(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def reveal(self, value):
        return self.fernet.decrypt(value.encode()).decode()
