"""
CKB address bech32/bech32m decoder.

Supports:
- Short format (bech32): payload[0] == 0x01, secp256k1_blake160
- Full format (bech32m):  payload[0] in {0x00, 0x02, 0x04}
"""

# bech32 / bech32m constants
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3

SECP256K1_BLAKE160_CODE_HASH = (
    "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
)


def _bech32_polymod(values):
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp, data):
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + list(data))
    if const == BECH32_CONST:
        return "bech32"
    if const == BECH32M_CONST:
        return "bech32m"
    return None


def _bech32_decode(bech):
    """Returns (hrp, data_bytes, encoding) or raises ValueError."""
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1:
        raise ValueError("Invalid bech32 string: no separator")
    hrp = bech[:pos]
    data_str = bech[pos + 1 :]
    if len(data_str) < 6:
        raise ValueError("Bech32 string too short")
    for c in data_str:
        if c not in CHARSET:
            raise ValueError(f"Invalid bech32 character: {c!r}")
    decoded = [CHARSET.index(c) for c in data_str]
    encoding = _bech32_verify_checksum(hrp, decoded)
    if encoding is None:
        raise ValueError("Invalid bech32 checksum")
    # Convert 5-bit groups to 8-bit bytes (strip checksum)
    data5 = decoded[:-6]
    acc = 0
    bits = 0
    result = []
    for val in data5:
        acc = (acc << 5) | val
        bits += 5
        while bits >= 8:
            bits -= 8
            result.append((acc >> bits) & 0xFF)
    if bits >= 5 or (acc & ((1 << bits) - 1)):
        raise ValueError("Invalid padding in bech32 data")
    return hrp, bytes(result), encoding


def decode_ckb_address(address: str) -> dict:
    """
    Decode a CKB address string into a lock script dict:
      {"code_hash": "0x...", "hash_type": "type"|"data"|"data1", "args": "0x..."}

    Supports:
    - Short format (bech32, prefix ckb/ckt, payload[0]==0x01)
    - Full format  (bech32m, prefix ckb/ckt, payload[0] in {0x00,0x02,0x04})

    Raises ValueError on invalid input.
    """
    hrp, payload, encoding = _bech32_decode(address)
    if hrp not in ("ckb", "ckt"):
        raise ValueError(f"Unknown CKB address prefix: {hrp!r}")
    if len(payload) < 1:
        raise ValueError("Empty address payload")

    fmt = payload[0]

    # ── Short format (bech32, fmt == 0x01) ─────────────────────────────────
    if fmt == 0x01:
        # payload: [0x01, code_hash_index(1), args...]
        if len(payload) < 2:
            raise ValueError("Short format payload too short")
        code_hash_index = payload[1]
        if code_hash_index != 0x00:
            raise ValueError(
                f"Unsupported short-format code_hash index: {code_hash_index:#x}"
            )
        args = payload[2:]
        return {
            "code_hash": SECP256K1_BLAKE160_CODE_HASH,
            "hash_type": "type",
            "args": "0x" + args.hex(),
        }

    # ── Full format 0x00 (bech32m, new format) ─────────────────────────────
    # payload: [0x00][code_hash(32)][hash_type(1)][args...]
    if fmt == 0x00:
        if len(payload) < 34:
            raise ValueError("Full format (0x00) payload too short")
        code_hash = payload[1:33]
        hash_type_byte = payload[33]
        args = payload[34:]
        hash_type_map = {0x00: "data", 0x01: "type", 0x02: "data1"}
        hash_type = hash_type_map.get(hash_type_byte, "type")
        return {
            "code_hash": "0x" + code_hash.hex(),
            "hash_type": hash_type,
            "args": "0x" + args.hex(),
        }

    # ── Full format 0x02 / 0x04 (bech32, older format) ─────────────────────
    # payload: [fmt_byte(1)][code_hash(32)][args...]
    # 0x02 = full with data, 0x04 = full with type
    if fmt in (0x02, 0x04):
        if len(payload) < 33:
            raise ValueError(f"Full format ({fmt:#x}) payload too short")
        code_hash = payload[1:33]
        args = payload[33:]
        hash_type = "data" if fmt == 0x02 else "type"
        return {
            "code_hash": "0x" + code_hash.hex(),
            "hash_type": hash_type,
            "args": "0x" + args.hex(),
        }

    raise ValueError(f"Unsupported address format byte: {fmt:#x}")
