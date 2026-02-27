"""
CKB address bech32/bech32m decoder.

Supports:
- Short format (prefix ckb/ckt, bech32): payload[0] == 0x01
- Full format (bech32m): payload[0] in {0x00, 0x02, 0x04}
"""

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3

_FORMAT_TYPE_HASH_TYPES = {
    0x00: "data",
    0x01: "type",
    0x02: "data1",
    0x04: "type2",
}

SECP256K1_BLAKE160_CODE_HASH = (
    "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
)


def _polymod(values):
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _verify_checksum(hrp, data, spec):
    const = BECH32M_CONST if spec == "bech32m" else BECH32_CONST
    return _polymod(_hrp_expand(hrp) + list(data)) == const


def _detect_spec(hrp, data):
    if _verify_checksum(hrp, data, "bech32m"):
        return "bech32m"
    if _verify_checksum(hrp, data, "bech32"):
        return "bech32"
    return None


def _bech32_decode(bech):
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return None, None, None
    if bech.lower() != bech and bech.upper() != bech:
        return None, None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return None, None, None
    hrp = bech[:pos]
    data = []
    for c in bech[pos + 1:]:
        d = CHARSET.find(c)
        if d == -1:
            return None, None, None
        data.append(d)
    spec = _detect_spec(hrp, data)
    if spec is None:
        return None, None, None
    return hrp, data[:-6], spec


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def decode_ckb_address(address):
    """
    Decode a CKB bech32/bech32m address.

    Returns a dict:
        {"code_hash": "0x...", "hash_type": "type"|"data"|"data1"|"type2", "args": "0x..."}

    Raises ValueError on invalid address.
    """
    hrp, data5, spec = _bech32_decode(address)
    if hrp is None:
        raise ValueError(f"Invalid bech32 address: {address!r}")
    if hrp not in ("ckb", "ckt"):
        raise ValueError(f"Unknown CKB address prefix: {hrp!r}")

    payload = _convertbits(data5, 5, 8, pad=False)
    if payload is None:
        raise ValueError("Bit-conversion failed")

    fmt = payload[0]

    # Short format: 0x01 prefix, bech32 encoding
    if fmt == 0x01 and spec == "bech32":
        args_bytes = bytes(payload[1:])
        return {
            "code_hash": SECP256K1_BLAKE160_CODE_HASH,
            "hash_type": "type",
            "args": "0x" + args_bytes.hex(),
        }

    # Full format (bech32m): 0x00, 0x02, 0x04
    if fmt in _FORMAT_TYPE_HASH_TYPES and spec == "bech32m":
        if len(payload) < 33:
            raise ValueError("Payload too short for full format address")
        code_hash_bytes = bytes(payload[1:33])
        args_bytes = bytes(payload[33:])
        hash_type = _FORMAT_TYPE_HASH_TYPES[fmt]
        return {
            "code_hash": "0x" + code_hash_bytes.hex(),
            "hash_type": hash_type,
            "args": "0x" + args_bytes.hex(),
        }

    raise ValueError(
        f"Unsupported CKB address format byte 0x{fmt:02x} with spec {spec!r}"
    )
