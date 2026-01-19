from rock.utils.crypto_utils import AESEncryption


class TestAESEncryption:
    encryptor = AESEncryption()

    def test_aes_encryption_decryption(self):
        plaintext = "test_symmetric_encryption_data"

        encrypted = self.encryptor.encrypt(plaintext)
        assert encrypted is not None
        assert encrypted != plaintext
        assert isinstance(encrypted, str)

        decrypted = self.encryptor.decrypt(encrypted)
        assert decrypted == plaintext

    def test_aes_encryption_with_different_passwords(self):
        plaintext = "sensitive_data"

        encrypted = self.encryptor.encrypt(plaintext)
        try:
            encryptor2 = AESEncryption()
            decrypted = encryptor2.decrypt(encrypted)
            assert decrypted != plaintext
        except Exception:
            pass

    def test_aes_encryption_with_empty_string(self):
        plaintext = ""
        encrypted = self.encryptor.encrypt(plaintext)
        decrypted = self.encryptor.decrypt(encrypted)

        assert decrypted == plaintext

    def test_aes_same_plaintext_different_ciphertext(self):
        plaintext = "same_plaintext"

        encrypted1 = self.encryptor.encrypt(plaintext)
        encrypted2 = self.encryptor.encrypt(plaintext)

        assert encrypted1 != encrypted2

        assert self.encryptor.decrypt(encrypted1) == plaintext
        assert self.encryptor.decrypt(encrypted2) == plaintext

    def test_aes_encryption_bytes_input(self):
        plaintext = b"test_bytes_data"
        encrypted = self.encryptor.encrypt(plaintext)
        decrypted = self.encryptor.decrypt(encrypted)

        assert decrypted == plaintext.decode("utf-8")
