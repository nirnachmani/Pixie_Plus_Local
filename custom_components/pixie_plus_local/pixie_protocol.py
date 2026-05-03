#!/usr/bin/env python3
"""
Pixie Protocol - encryption and envelope handling for TCP handshake.

This module provides the encryption and envelope handling needed for Pixie Plus protocol communication.
Based on Java p0/b.c and a.java analysis.

Dependencies:
  - pycryptodome: AES-128-CBC encryption (install via: pip install pycryptodome)
"""

import json
import base64
import binascii
import logging
from typing import Dict, Any, Optional, Tuple
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

LOGGER = logging.getLogger(__name__)

# Envelope flags
FLAG_SINGLE_DATA = 1
FLAG_DUAL_DATA = 0
FLAG_EACK = 2
FLAG_HEARTBEAT = 5


class PixieCrypto:
    """AES-128-CBC encryption/decryption with PKCS7 padding.
    
    Matches Java p0/a.java implementation exactly:
    - IV: 16 bytes of ASCII '0' (0x30) - from static byte[] c 
    - Key padding: fills entire array with zeros, then copies key bytes (Java method g())
    - Mode: AES/CBC/PKCS7Padding
    """

    # Java p0/a.java: static byte[] c = new byte[]{48,48,...} = ASCII '0' repeated 16 times
    IV = b'0' * 16  # bytes([0x30] * 16)

    @staticmethod
    def _pad_key_java_style(key_bytes: bytes) -> bytes:
        """
        Pad key to 16-byte boundary, matching Java p0/a.g() implementation.
        
        Java logic:
        - Create array of size: ceil(length / 16) * 16
        - Fill ENTIRE array with zeros using Arrays.fill()
        - Copy key bytes starting at offset 0
        
        This is different from simply appending zeros - the array is pre-filled!
        """
        if len(key_bytes) % 16 == 0:
            return key_bytes
        
        # Calculate padded size: (length // 16 + 1) * 16
        padded_length = ((len(key_bytes) // 16) + 1) * 16
        
        # Create array filled with zeros (Java: Arrays.fill(var3, (byte)0))
        padded = bytearray(padded_length)  # bytearray initializes to zeros
        
        # Copy key bytes starting at offset 0
        padded[:len(key_bytes)] = key_bytes
        
        return bytes(padded)

    @staticmethod
    def encrypt(data: str, key: str) -> bytes:
        """Encrypt string with AES-128-CBC (Java p0/a style)."""
        key_bytes = key.encode('utf-8')
        padded_key = PixieCrypto._pad_key_java_style(key_bytes)
        cipher = AES.new(
            padded_key,
            AES.MODE_CBC,
            PixieCrypto.IV
        )
        return cipher.encrypt(pad(data.encode('utf-8'), AES.block_size))

    @staticmethod
    def decrypt(data: bytes, key: str) -> str:
        """
        Decrypt bytes with AES-128-CBC (matches Java p0/a.c() implementation).
        
        Args:
            data: Raw encrypted bytes
            key: Key string (will be converted to bytes and padded)
        
        Returns:
            Decrypted plaintext as UTF-8 string
        
        Raises:
            Exception: If decryption or unpadding fails
        """
        key_bytes = key.encode('utf-8')
        padded_key = PixieCrypto._pad_key_java_style(key_bytes)
        cipher = AES.new(
            padded_key,
            AES.MODE_CBC,
            PixieCrypto.IV
        )
        decrypted = cipher.decrypt(data)
        plaintext = unpad(decrypted, AES.block_size)
        return plaintext.decode('utf-8')


class PixieEnvelope:
    """Envelope format encoder/decoder (matches Java p0/b.c)."""

    @staticmethod
    def encode(data: Dict[str, Any], key: str, flag: int = FLAG_SINGLE_DATA) -> bytes:
        """
        Encode data into Pixie envelope format.

        Args:
            data: Dictionary to encode
            key: Encryption key (16 bytes for AES-128)
            flag: Envelope flag (0 = dual, 1 = single)

        Returns:
            Encoded bytes (flag + encrypted data)
        """
        data_str = json.dumps(data)
        encrypted = PixieCrypto.encrypt(data_str, key)

        if flag == FLAG_DUAL_DATA:  # 0
            # Two-block format: flag1 + data1 (16) + flag2 + data2 (remainder)
            flag1 = 0
            flag2 = 0
            data1 = encrypted[:16]
            data2 = encrypted[16:32] if len(encrypted) > 16 else b''
            return bytes([flag1]) + data1 + bytes([flag2]) + data2
        else:  # FLAG_SINGLE_DATA = 1
            # One-block format: flag1 + data (variable)
            return bytes([flag]) + encrypted

    @staticmethod
    def decode(data: bytes) -> Optional[Dict[str, Any]]:
        """
        Decode Pixie envelope structure (matches Java p0/b.c).
        
        Does NOT decrypt - just extracts flags and encrypted blocks as hex.
        Decryption happens in a separate layer.

        - flag == 0: dual-block:
            byte0=flag1
            bytes1-16=encrypted_data1 (hex)
            byte17=flag2
            bytes18-33=encrypted_data2 (hex)

        - flag != 0: single-block:
            byte0=flag1
            bytes1..end=encrypted_data (hex)

        Args:
            data: Raw envelope bytes (base64 decoded)

        Returns:
            Dict with flag1, data1, [flag2], [data2] as hex strings. None if invalid.
        """
        if not data or len(data) < 2:
            return None

        flag = data[0]
        result = {}

        try:
            if flag == FLAG_DUAL_DATA and len(data) >= 34:
                # Dual-block format
                block1 = data[1:17]
                flag2 = data[17]
                block2 = data[18:34]

                result["flag1"] = flag
                result["data1"] = block1.hex()
                result["flag2"] = flag2
                result["data2"] = block2.hex()
                return result

            elif len(data) > 1:
                # Non-dual format: first byte is the envelope flag and the
                # remaining bytes are a single encrypted payload.
                encrypted = data[1:]
                result["flag1"] = flag
                result["data1"] = encrypted.hex()
                return result

        except Exception as e:
            LOGGER.debug("Envelope decode error: %s", e, exc_info=True)
            return None

        return result if result else None

    @staticmethod
    def decrypt_dual_parts(envelope_struct: Dict[str, Any], key: str) -> Optional[Tuple[str, str]]:
        """
        Decrypt dual-block envelope into (part1, part2).

        Java 2.22 flow:
        - part1 = decrypt(data1, netID)        -> session key used for TCP messages
        - part2 = decrypt(data2, part1)        -> mesh validation value (meshNet/meshNet2)
        """
        if not envelope_struct or envelope_struct.get("flag1") != FLAG_DUAL_DATA:
            return None

        data1_hex = envelope_struct.get("data1")
        data2_hex = envelope_struct.get("data2")
        if not data1_hex or not data2_hex:
            return None

        try:
            block1_bytes = binascii.unhexlify(data1_hex)
            part1 = PixieCrypto.decrypt(block1_bytes, key)

            block2_bytes = binascii.unhexlify(data2_hex)
            part2 = PixieCrypto.decrypt(block2_bytes, part1)
            return part1, part2
        except Exception as e:
            LOGGER.debug("Dual decrypt error: %s", e, exc_info=True)
            return None

    @staticmethod
    def decrypt_envelope(envelope_struct: Dict[str, Any], key: str) -> Optional[str]:
        """
        Decrypt envelope structure with CHAINED key derivation (Java p0.b.a() flow).
        
        For flag=0 (dual-block): 
        - Decrypt block1 with initial key (netID) → intermediate key
        - Decrypt block2 with block1 result → rest of key
        - Concatenate both to get session key
        
        For flag=1 (single-block):
        - Decrypt single block with initial key

        Args:
            envelope_struct: Dict with flag1, data1, [flag2], [data2] as hex strings
            key: Initial decryption key as string (typically netID)

        Returns:
            Decrypted session key/data as string, or None if invalid
        """
        if not envelope_struct or "flag1" not in envelope_struct:
            return None

        flag1 = envelope_struct.get("flag1")
        
        try:
            if flag1 == FLAG_DUAL_DATA:
                # Backward-compatible return value: concat(part1, part2).
                # Prefer decrypt_dual_parts() for Java-accurate semantics.
                parts = PixieEnvelope.decrypt_dual_parts(envelope_struct, key)
                if not parts:
                    return None
                plain1, plain2 = parts
                return plain1 + plain2
                
            elif flag1 != FLAG_DUAL_DATA:
                # Non-dual format  
                data1_hex = envelope_struct.get("data1")
                if not data1_hex:
                    return None
                
                block_bytes = binascii.unhexlify(data1_hex)
                plaintext = PixieCrypto.decrypt(block_bytes, key)
                return plaintext
                
        except Exception as e:
            LOGGER.debug("Envelope decrypt error: %s", e, exc_info=True)
            return None

        return None

    @staticmethod
    def to_base64(envelope: bytes) -> str:
        """Convert envelope to base64 for transport."""
        return base64.b64encode(envelope).decode('utf-8')

    @staticmethod
    def from_base64(b64_data: str) -> bytes:
        """Decode base64 envelope."""
        return base64.b64decode(b64_data.encode('utf-8'))

    @staticmethod
    def encode_to_base64(data: Dict[str, Any], key: str, flag: int) -> str:
        """Encode data to base64 envelope (matches Java p0/b.b)."""
        envelope = PixieEnvelope.encode(data, key, flag)
        return PixieEnvelope.to_base64(envelope)


class PixieMessage:
    """Message builder for common Pixie protocol messages."""

    @staticmethod
    def build_gwdata_init(key: str) -> str:
        """Build app-like initial GwData message observed right after session init."""
        data = {
            "data": {
                "type": "GwData",
                "data": "fffe01010100000400003400d568",
            }
        }
        return PixieEnvelope.encode_to_base64(data, key, flag=1)

    @staticmethod
    def build_heartbeat(key: str) -> str:
        """Build heartbeat message (matches Java q0/b.H("heartbeat"))."""
        data = {"op": "ack", "code": 0}
        return PixieEnvelope.encode_to_base64(data, key, flag=5)

    @staticmethod
    def build_eack(key: str) -> str:
        """Build eack message (matches Java q0/b.H("eack"))."""
        data = {"op": "ack", "code": 0}
        return PixieEnvelope.encode_to_base64(data, key, flag=2)
