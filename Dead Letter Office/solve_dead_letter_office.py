#!/usr/bin/env python3
"""Recover the accepted letter from Dead Letter Office challenge files."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

MASK32 = 0xFFFFFFFF
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193


def rol8(value: int, count: int) -> int:
    return ((value << count) | (value >> (8 - count))) & 0xFF


def rol_low_byte(value: int, count: int) -> int:
    return ((value & ~0xFF) | rol8(value & 0xFF, count)) & MASK32


def fnv1a32(data: bytes) -> int:
    value = FNV_OFFSET
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & MASK32
    return value


def decode_key(binary: bytes) -> tuple[int, list[int], int, int, int]:
    marker = binary.find(b"DLO2")
    if marker < 0 or marker + 15 > len(binary):
        raise ValueError("DLO2 verifier constants were not found")

    encoded_key = binary[marker + 4 : marker + 12]
    key = [
        byte ^ ((index * 31 - 0x5B) & 0xFF)
        for index, byte in enumerate(encoded_key)
    ]
    slot_increment = binary[marker + 12]
    slot_start = binary[marker + 13]
    inner_rounds = binary[marker + 14]
    return marker, key, slot_increment, slot_start, inner_rounds


def pair_matches(
    outer_round: int,
    first: int,
    second: int,
    previous_word: int,
    target_word: int,
    key: list[int],
    inner_rounds: int,
) -> bool:
    """Model one two-character verification round from dead_letter_office."""
    state_a = (first ^ previous_word) & MASK32
    state_b = (second ^ (previous_word >> 8)) & 0xFF
    rolling_a = state_a
    rolling_b = state_b
    round_base = (outer_round * 13) & MASK32
    rotation_b = (outer_round % 7) + 1
    key_rotation = 5
    value = state_b

    for inner_round in range(inner_rounds):
        mixed = ((key[(outer_round + inner_round) & 7] ^ round_base) + value) & MASK32
        value = rol_low_byte(mixed, (inner_round % 7) + 1)
        value ^= rol8(key[key_rotation & 7], rotation_b)
        value ^= rolling_a
        value &= MASK32
        key_rotation = (key_rotation + 3) & MASK32
        rolling_a = rolling_b
        if inner_round != inner_rounds - 1:
            rolling_b = value

    previous_rotated = ((previous_word << 3) | (previous_word >> 13)) & MASK32
    post_mix = (
        ((previous_rotated ^ ((outer_round + 1) * 0x9E37)) & 0xFFFF)
        ^ target_word
    ) & MASK32

    # The assembly compares the low byte of the penultimate inner result and
    # the low byte of the final inner result with the target-derived value.
    low_check = ((rolling_b ^ post_mix) & 0xFF) == 0
    high_check = (((post_mix >> 8) ^ (value & 0xFF)) & MASK32) == 0
    return low_check and high_check


def solve(grid_path: Path, binary_path: Path) -> tuple[str, int, int, int]:
    grid = grid_path.read_bytes()
    if len(grid) < 0x80 or grid[:4] != b"HPL2":
        raise ValueError("invalid HPL2 postcard grid")

    _, length, pair_count, recorded_hash, initial_word = struct.unpack_from("<4sHHIH", grid)
    if length != pair_count * 2:
        raise ValueError("grid length and pair count disagree")
    payload = grid[0x80:]
    if len(payload) != pair_count * 4:
        raise ValueError("grid payload has an unexpected size")
    if fnv1a32(payload) != recorded_hash:
        raise ValueError("FNV-1a integrity check failed")

    marker, key, increment, selector, inner_rounds = decode_key(binary_path.read_bytes())
    recovered: list[int] = []
    previous_word = initial_word

    for outer_round in range(pair_count):
        slot = selector % pair_count
        target_word = struct.unpack_from("<H", payload, slot * 4)[0]
        matches = []
        for first in range(256):
            for second in range(256):
                if pair_matches(
                    outer_round,
                    first,
                    second,
                    previous_word,
                    target_word,
                    key,
                    inner_rounds,
                ):
                    matches.append((first, second))

        if len(matches) != 1:
            raise ValueError(
                f"round {outer_round}: expected one character pair, found {len(matches)}"
            )
        recovered.extend(matches[0])
        previous_word = target_word
        selector += increment

    return bytes(recovered).decode("ascii"), marker, recorded_hash, inner_rounds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("grid", nargs="?", default="postcard.grid")
    parser.add_argument("binary", nargs="?", default="dead_letter_office")
    args = parser.parse_args()

    grid_path = Path(args.grid)
    binary_path = Path(args.binary)
    flag, marker, integrity_hash, inner_rounds = solve(grid_path, binary_path)
    _, length, pair_count, _, initial_word = struct.unpack_from("<4sHHIH", grid_path.read_bytes())

    print(f"[+] Grid: {grid_path}")
    print(f"[+] DLO2 constants offset: 0x{marker:x}")
    print(
        f"[+] Header: length={length}, pairs={pair_count}, "
        f"seed=0x{initial_word:04x}"
    )
    print(f"[+] FNV-1a check: 0x{integrity_hash:08x} (valid)")
    print(f"[+] Inner verifier rounds: {inner_rounds}")
    print(f"[+] Unique character pairs recovered: {pair_count}/{pair_count}")
    print(f"[+] Accepted input: {flag}")


if __name__ == "__main__":
    main()
