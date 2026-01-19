import argparse
import sys

from rock.cli.command.command import Command
from rock.logger import init_logger
from rock.utils.crypto_utils import AESEncryption

logger = init_logger("rock.cli.crypto")


class CryptoCommand(Command):
    name = "crypto"

    def __init__(self):
        super().__init__()

    async def arun(self, args: argparse.Namespace):
        if not args.crypto_action:
            raise ValueError("Crypto action is required (generate-key, encrypt, decrypt)")

        if args.crypto_action == "generate-key":
            await self._generate_key(args)
        elif args.crypto_action == "encrypt":
            await self._encrypt(args)
        elif args.crypto_action == "decrypt":
            await self._decrypt(args)
        else:
            raise ValueError(f"Unknown crypto action '{args.crypto_action}'")

    async def _generate_key(self, args: argparse.Namespace):
        key = AESEncryption.generate_key()
        print(f"Generated AES Key: {key}")
        logger.info("AES key generated successfully")

    async def _encrypt(self, args: argparse.Namespace):
        key = args.key
        if not key:
            logger.error("Encryption key is required. Use --key option or generate one with 'rock crypto generate-key'")
            sys.exit(1)

        plaintext = args.text
        if not plaintext:
            logger.error("No text provided. Use --text option")
            sys.exit(1)

        try:
            aes = AESEncryption(key)
            ciphertext = aes.encrypt(plaintext)
            print(ciphertext)
            logger.info("Text encrypted successfully")
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            sys.exit(1)

    async def _decrypt(self, args: argparse.Namespace):
        key = args.key
        if not key:
            logger.error("Decryption key is required. Use --key option")
            sys.exit(1)

        ciphertext = args.text
        if not ciphertext:
            logger.error("No ciphertext provided. Use --text option")
            sys.exit(1)

        try:
            aes = AESEncryption(key)
            plaintext = aes.decrypt(ciphertext)
            print(plaintext)
            logger.info("Text decrypted successfully")
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            sys.exit(1)

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        crypto_parser = subparsers.add_parser(
            "crypto",
            description="Cryptography operations using AES encryption",
            help="Encrypt and decrypt text using AES",
        )

        crypto_subparsers = crypto_parser.add_subparsers(dest="crypto_action", help="Crypto actions")

        crypto_subparsers.add_parser("generate-key", help="Generate a new AES encryption key")

        encrypt_parser = crypto_subparsers.add_parser("encrypt", help="Encrypt text using AES")
        encrypt_parser.add_argument(
            "-k", "--key", required=True, help="AES encryption key (generate one with 'rock crypto generate-key')"
        )
        encrypt_parser.add_argument("-t", "--text", required=True, help="Text to encrypt")

        decrypt_parser = crypto_subparsers.add_parser("decrypt", help="Decrypt text using AES")
        decrypt_parser.add_argument(
            "-k", "--key", required=True, help="AES decryption key (same key used for encryption)"
        )
        decrypt_parser.add_argument("-t", "--text", required=True, help="Text to decrypt")
