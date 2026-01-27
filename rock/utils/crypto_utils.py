import base64
import os
from abc import ABC, abstractmethod
from enum import Enum

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class EncryptionMode(Enum):
    AES_GCM_256 = "aes_gcm_256"


class EncryptionStrategy(ABC):
    @abstractmethod
    def encrypt(self, plaintext: bytes) -> str:
        pass

    @abstractmethod
    def decrypt(self, ciphertext: str) -> bytes:
        pass

    @abstractmethod
    def update_key(self, key: str):
        pass

    @staticmethod
    @abstractmethod
    def generate_key() -> str:
        pass


class AESGCM256Strategy(EncryptionStrategy):
    _nonce_len = 12
    _tag_len = 16

    def __init__(self, key: str | None = None):
        if key:
            self.key = base64.b64decode(key)
            if len(self.key) != 32:
                raise ValueError("AES-GCM-256 key should be 32 bytes")
        else:
            self.key = os.urandom(32)

    def encrypt(self, plaintext: bytes) -> str:
        nonce = os.urandom(self._nonce_len)
        cipher = Cipher(algorithms.AES(self.key), modes.GCM(nonce), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()

        # composition: nonce(12) + ciphertext + tag(16)
        return base64.b64encode(nonce + ciphertext + encryptor.tag).decode("utf-8")

    def decrypt(self, ciphertext: str) -> bytes:
        encrypted_data = base64.b64decode(ciphertext)

        # composition: [nonce(12) | ciphertext(variable) | tag(16)]
        nonce = encrypted_data[: self._nonce_len]
        ciphertext_bytes = encrypted_data[self._nonce_len : -self._tag_len]
        tag = encrypted_data[-self._tag_len :]

        cipher = Cipher(algorithms.AES(self.key), modes.GCM(nonce, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(ciphertext_bytes) + decryptor.finalize()

    def update_key(self, key: str):
        new_key = base64.b64decode(key)
        if len(new_key) != 32:
            raise ValueError("AES-GCM-256 key should be 32 bytes")
        if new_key == self.key:
            return
        self.key = new_key

    @staticmethod
    def generate_key() -> str:
        return base64.b64encode(os.urandom(32)).decode("utf-8")


class AESEncryption:
    _STRATEGY_MAP = {
        EncryptionMode.AES_GCM_256: AESGCM256Strategy,
    }

    def __init__(self, key: str | None = None, mode: EncryptionMode = EncryptionMode.AES_GCM_256):
        self.mode = mode
        strategy_class = self._STRATEGY_MAP.get(mode)
        if strategy_class is None:
            raise ValueError(f"Unsupported encryption mode: {mode}")

        self._strategy: EncryptionStrategy = strategy_class(key)

    def encrypt(self, plaintext: str | bytes) -> str:
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")

        return self._strategy.encrypt(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        plaintext_bytes = self._strategy.decrypt(ciphertext)
        return plaintext_bytes.decode("utf-8")

    def key_update(self, key: str):
        self._strategy.update_key(key)

    @classmethod
    def generate_key(cls, mode: EncryptionMode = EncryptionMode.AES_GCM_256) -> str:
        strategy_class = cls._STRATEGY_MAP.get(mode)
        if strategy_class is None:
            raise ValueError(f"Unsupported encryption mode: {mode}")

        return strategy_class.generate_key()


if __name__ == "__main__":
    print(f"AES_GCM_256 key: {AESEncryption.generate_key()}")
