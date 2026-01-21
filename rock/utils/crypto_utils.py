from cryptography.fernet import Fernet


class AESEncryption:
    """
    AES对称加密/解密工具类
    使用Fernet（AES-128-CBC + HMAC）
    """

    def __init__(self, key: str | None = None):
        if key:
            self.key = key.encode("utf-8")
        else:
            self.key = Fernet.generate_key()
        self.fernet = Fernet(self.key)

    def encrypt(self, plaintext: str | bytes) -> str:
        if isinstance(plaintext, str):
            plaintext = plaintext.encode()
        return self.fernet.encrypt(plaintext).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self.fernet.decrypt(ciphertext.encode()).decode()

    def key_update(self, key: str):
        new_key = key.encode("utf-8")
        if new_key == self.key:
            return
        self.key = new_key
        self.fernet = Fernet(self.key)

    @classmethod
    def generate_key(cls):
        return Fernet.generate_key().decode()


if __name__ == "__main__":
    # 生成新的AES密钥
    print(f"AES密钥: {AESEncryption.generate_key()}")
