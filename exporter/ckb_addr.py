"""
CKB address decoder using official segwit_addr implementation.

Supports both mainnet (ckb) and testnet (ckt) addresses:
- Short format (bech32, payload[0] == 0x01): secp256k1-blake160
- Full format (bech32m, payload[0] == 0x00): arbitrary script
- Deprecated full format (bech32m, payload[0] in {0x02, 0x04}): legacy

ref: https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0021-ckb-address-format/0021-ckb-address-format.md
"""

import segwit_addr as sa

FORMAT_TYPE_FULL = 0x00
FORMAT_TYPE_SHORT = 0x01
FORMAT_TYPE_FULL_DATA = 0x02
FORMAT_TYPE_FULL_TYPE = 0x04

SECP256K1_BLAKE160_CODE_HASH = "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"

# hash_type byte mapping for full format
_HASH_TYPE_MAP = {
    0x00: "data",
    0x01: "type",
    0x02: "data1",
}


def decode_ckb_address(address: str) -> dict:
    """
    Decode a CKB bech32/bech32m address (mainnet or testnet).

    Returns a dict:
        {"code_hash": "0x...", "hash_type": "type"|"data"|"data1", "args": "0x..."}

    Raises ValueError on invalid address.
    """
    hrpgot, data, spec = sa.bech32_decode(address)

    if hrpgot is None or data is None:
        raise ValueError(f"Invalid bech32 address: {address!r}")
    if hrpgot not in ("ckb", "ckt"):
        raise ValueError(f"Unknown CKB address prefix: {hrpgot!r}")

    decoded = sa.convertbits(data, 5, 8, False)
    if decoded is None:
        raise ValueError("Bit-conversion failed")

    payload = bytes(decoded)
    format_type = payload[0]

    # Full format (bech32m): payload[0] == 0x00
    if format_type == FORMAT_TYPE_FULL:
        if spec != sa.Encoding.BECH32M:
            raise ValueError("Full format address must use bech32m encoding")
        if len(payload) < 34:
            raise ValueError("Payload too short for full format address")
        ptr = 1
        code_hash = "0x" + payload[ptr: ptr + 32].hex()
        ptr += 32
        hash_type_byte = payload[ptr]
        ptr += 1
        args = "0x" + payload[ptr:].hex()
        if hash_type_byte not in _HASH_TYPE_MAP:
            raise ValueError(f"Unknown hash_type byte 0x{hash_type_byte:02x} in full format address")
        hash_type = _HASH_TYPE_MAP[hash_type_byte]
        return {
            "code_hash": code_hash,
            "hash_type": hash_type,
            "args": args,
        }

    # Short format (bech32): payload[0] == 0x01
    elif format_type == FORMAT_TYPE_SHORT:
        if spec != sa.Encoding.BECH32:
            raise ValueError("Short format address must use bech32 encoding")
        # payload[1] is code_index (0x00 = secp256k1-blake160-sighash)
        # payload[2:] is the 20-byte lock args
        args = "0x" + payload[2:].hex()
        return {
            "code_hash": SECP256K1_BLAKE160_CODE_HASH,
            "hash_type": "type",
            "args": args,
        }

    # Deprecated full format (bech32m): payload[0] in {0x02, 0x04}
    elif format_type in (FORMAT_TYPE_FULL_DATA, FORMAT_TYPE_FULL_TYPE):
        if spec != sa.Encoding.BECH32M:
            raise ValueError("Deprecated full format address must use bech32m encoding")
        if len(payload) < 33:
            raise ValueError("Payload too short for deprecated full format address")
        ptr = 1
        code_hash = "0x" + payload[ptr: ptr + 32].hex()
        ptr += 32
        args = "0x" + payload[ptr:].hex()
        hash_type = "data" if format_type == FORMAT_TYPE_FULL_DATA else "type"
        return {
            "code_hash": code_hash,
            "hash_type": hash_type,
            "args": args,
        }

    raise ValueError(f"Unsupported CKB address format byte 0x{format_type:02x}")
