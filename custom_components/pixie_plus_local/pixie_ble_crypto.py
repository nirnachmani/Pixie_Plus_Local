"""
Pixie Plus BLE Protocol Crypto Implementation
Reverse engineered from libbt_struct.so

Protocol flow:
1. Connect to device BLE
2. Write 20-byte login packet to char 1914
3. Read 17-byte response from char 1914
4. Derive the 16-byte session key (SK) from login materials
5. Write 0x01 directly to char 1911 (NOT CCCD descriptor) to enable notifications
6. Decrypt 20-byte notifications and encrypt 1912 command packets with SK

Char UUIDs (service 0000cdab-0000-1000-8000-00805f9b34fb):
  1914: login (write + read)
  1911: notifications - write 0x01 to enable; device sends 20-byte encrypted state
  1912: commands (write, 12 bytes encrypted)

Note: chars appear under alternate UUID base on all observed firmware:
  00010203-0405-0607-0809-0a0b0c0d1914
  00010203-0405-0607-0809-0a0b0c0d1911
  00010203-0405-0607-0809-0a0b0c0d1912
Always resolve by 4-hex suffix from discovered characteristics.

The active integration uses only the app-native aes_att packet helper captured
from Frida. process_login_response() returns the real session key. 1911 packets
decrypt with that key directly, and 1912 commands preserve packet[0:3], replace
packet[3:5] with generated auth, and encrypt packet[5:].
"""

from Crypto.Cipher import AES
import os


# ---------------------------------------------------------------------------
# Core crypto primitive
# ---------------------------------------------------------------------------

def fn_eacc(key: bytes, inp: bytes) -> bytes:
    """Core AES operation from libbt_struct.so:fn_eacc (offset 0xeacc).

    Equivalent to: reverse(AES_ECB(reverse(key), reverse(inp)))
    Used for SK derivation and packet crypto.
    NOT used directly for login packet construction (see build_login_packet).
    """
    assert len(key) == 16 and len(inp) == 16, \
        f"fn_eacc: key={len(key)}B inp={len(inp)}B (both must be 16)"
    return AES.new(key[::-1], AES.MODE_ECB).encrypt(inp[::-1])[::-1]


def _native_eacc(xor_rands: bytes, rp8: bytes) -> bytes:
    """AES operation used specifically for login packet construction.

    Replicates the native aes_att_er inner eacc call:
      - AES key   = reverse(rp8 + 0x00*8)  (rp8 zero-padded to 16, then reversed)
      - Plaintext = reverse(xor_rands)
      - Output    = reverse(AES_ECB(key, plaintext))

    Args:
        xor_rands: 16-byte XOR of name_pad and netid_pad
        rp8:       8-byte random nonce

    Returns:
        16-byte AES output (only first 8 bytes used in login packet)
    """
    assert len(xor_rands) == 16
    assert len(rp8) == 8
    aes_key = (rp8 + b'\x00' * 8)[::-1]
    plaintext = xor_rands[::-1]
    return AES.new(aes_key, AES.MODE_ECB).encrypt(plaintext)[::-1]


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def build_login_packet(device_name: str = "Smart Light",
                       netid: str = "349154808",
                       rand_phone: bytes = None) -> tuple:
    """Build the 20-byte login packet to write to char 1914.

    Login packet structure (confirmed by 10/10 live test with fresh random):
        [0x0c]           1 byte  command byte
        [rp8]            8 bytes raw random nonce (plaintext in packet)
        [eacc_out[:8]]   8 bytes first 8 bytes of _native_eacc(xor_rands, rp8)
        [0x00, 0x00, 0x00] 3 bytes padding
        Total: 20 bytes

    where xor_rands = name_pad XOR netid_pad
          name_pad  = device_name encoded and padded to 16 bytes
          netid_pad = netid encoded and padded to 16 bytes

    Args:
        device_name: mesh device name, default "Smart Light"
        netid:       Network ID string, default "349154808"
        rand_phone:  Optional 8-byte random nonce (generated if not provided)

    Returns:
        (login_pkt: bytes[20], rand_phone: bytes[8])
        Keep rand_phone for process_login_response().
    """
    if rand_phone is None:
        rand_phone = os.urandom(8)
    assert len(rand_phone) == 8, f"rand_phone must be 8 bytes, got {len(rand_phone)}"

    name_pad  = device_name.encode().ljust(16, b'\x00')[:16]
    netid_pad = netid.encode().ljust(16, b'\x00')[:16]
    xor_rands = bytes(a ^ b for a, b in zip(name_pad, netid_pad))

    eacc_out  = _native_eacc(xor_rands, rand_phone)
    payload   = rand_phone + eacc_out[:8]
    login_pkt = b'\x0c' + payload + b'\x00\x00\x00'

    assert len(login_pkt) == 20
    return login_pkt, rand_phone


def process_login_response(login_rsp: bytes,
                           rand_phone: bytes,
                           device_name: str = "Smart Light",
                           netid: str = "349154808") -> bytes:
    """Process 17-byte login response from char 1914, derive session key (SK).

    Args:
        login_rsp:   17 bytes read from char 1914 after writing login packet
        rand_phone:  8-byte random nonce returned by build_login_packet()
        device_name: mesh device name, default "Smart Light"
        netid:       Network ID string, default "349154808"

    Returns:
        SK: 16-byte session key

    Raises:
        ValueError if response is invalid or verification fails
    """
    if len(login_rsp) != 17 or login_rsp[0] != 0x0d:
        raise ValueError(f"Invalid login response (len={len(login_rsp)}, "
                         f"hdr=0x{login_rsp[0]:02x}): {login_rsp.hex()}")

    name_pad  = device_name.encode().ljust(16, b'\x00')[:16]
    netid_pad = netid.encode().ljust(16, b'\x00')[:16]
    xor_rands = bytes(a ^ b for a, b in zip(name_pad, netid_pad))

    rand_dev   = login_rsp[1:9]   # 8 random bytes from device
    verify_got = login_rsp[9:17]  # 8-byte verification field

    # Verify device's challenge response
    verify_expected = fn_eacc(rand_dev + b'\x00'*8, xor_rands)[:8]
    if verify_got != verify_expected:
        raise ValueError(f"Login verification failed: "
                         f"got {verify_got.hex()} expected {verify_expected.hex()}")

    # Derive SK:
    #   key1 = rand_phone  (8 bytes we sent)
    #   key2 = rand_dev    (8 bytes from device response)
    #   SK   = fn_eacc(xor_rands, key1 + key2)
    SK = fn_eacc(xor_rands, rand_phone[:8] + rand_dev[:8])
    return SK


def _packet_auth(SK: bytes, header8: bytes, body: bytes) -> bytes:
    """Generate aes_att packet auth/check bytes.

    This mirrors aes_att_encryption_packet/aes_att_decryption_packet:
      state = eacc(SK, header8 + len(body) + 7 zero bytes)
      for body bytes, XOR into state cyclically and eacc at each block/end
    """
    assert len(SK) == 16
    assert len(header8) == 8
    assert len(body) <= 255

    state = bytearray(fn_eacc(SK, header8 + bytes([len(body)]) + b'\x00' * 7))
    for idx, value in enumerate(body):
        state[idx & 0x0f] ^= value
        if idx == len(body) - 1 or (idx & 0x0f) == 0x0f:
            state = bytearray(fn_eacc(SK, bytes(state)))
    return bytes(state)


def _packet_xor_body(SK: bytes, header8: bytes, body: bytes) -> bytes:
    """XOR packet body with the aes_att stream for header8."""
    assert len(SK) == 16
    assert len(header8) == 8
    stream = b''
    counter = 0
    while len(stream) < len(body):
        block = bytes([counter & 0xff]) + header8 + b'\x00' * 7
        stream += fn_eacc(SK, block)
        counter += 1
    return bytes(value ^ stream[idx] for idx, value in enumerate(body))


def _mac_prefix(mac: bytes) -> bytes:
    """Return the three-byte Pixie packet prefix from a six-byte MAC."""
    assert len(mac) == 6
    return bytes([mac[5], mac[4], mac[3]])


def decrypt_notification_packet(SK: bytes, mac: bytes, pkt: bytes, verify_auth: bool = True) -> bytes:
    """Decrypt a raw 1911 notification packet and return its plaintext body.

    Raw notification shape:
      [0:3] nonce/counter
      [3:5] usually 0000, or ff7f for command responses
      [5:7] 2-byte auth/check
      [7:]  encrypted body

    The app passes this directly to BTDataHandle_decryption_data.
    """
    assert len(SK) == 16
    if len(pkt) < 7:
        raise ValueError(f"notification packet too short: {len(pkt)}")

    prefix = _mac_prefix(mac)
    header8 = prefix + pkt[:5]
    auth = pkt[5:7]
    body = _packet_xor_body(SK, header8, pkt[7:])
    if verify_auth:
        expected = _packet_auth(SK, header8, body)[:2]
        if auth != expected:
            raise ValueError(f"notification auth failed: got {auth.hex()} expected {expected.hex()}")
    return body


def encrypt_command_packet(SK: bytes, mac: bytes, plain_pkt: bytes) -> bytes:
    """Encrypt a full plaintext 1912 command packet.

    Plain command shape observed from the app:
      [0:3] nonce/counter, preserved
      [3:5] placeholder auth bytes, replaced
      [5:]  plaintext command body, encrypted
    """
    assert len(SK) == 16
    if len(plain_pkt) < 5:
        raise ValueError(f"command packet too short: {len(plain_pkt)}")

    prefix = _mac_prefix(mac)
    header8 = prefix + b'\x4d\x01' + plain_pkt[:3]
    body = plain_pkt[5:]
    auth = _packet_auth(SK, header8, body)[:2]
    encrypted_body = _packet_xor_body(SK, header8, body)
    return plain_pkt[:3] + auth + encrypted_body
