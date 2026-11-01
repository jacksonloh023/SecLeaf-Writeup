#!/usr/bin/env python3
"""Recover the accepted input from the SGR2 verifier in sugar_glass."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast


def rol8(value: int, count: int) -> int:
    """Rotate one byte left."""
    return ((value << count) | (value >> (8 - count))) & 0xFF


def solve(path: Path) -> str:
    data = path.read_bytes()
    marker = data.find(b"SGR2")
    if marker < 0:
        raise ValueError("SGR2 verifier data was not found")

    # Header at SGR2: magic, required length, index step, initial index, initial state.
    length, step, index, state = data[marker + 4 : marker + 8]
    expected = data[marker + 8 : marker + 8 + length]
    target = data[marker + 8 + length : marker + 8 + 2 * length]
    if len(expected) != length or len(target) != length:
        raise ValueError("truncated verifier tables")

    recovered = [None] * length
    accumulator = 0x5D

    # This exactly models the verifier at file offset 0x1230.  The input is
    # read in a permuted order; test all byte values at each round and retain
    # the unique value that reproduces the stored target byte.
    for round_number, table_byte in enumerate(expected):
        position = index % length
        round_constant = (0x39 + 0x11 * round_number) & 0xFFFFFFFF
        matches = []
        for candidate in range(256):
            mixed = (table_byte ^ candidate) + state
            state_candidate = (mixed & ~0xFF) | rol8(mixed & 0xFF, 3)
            derived = (state_candidate ^ round_constant) & 0xFFFFFFFF
            derived = (derived & ~0xFF) | rol8(derived & 0xFF, 1)
            accumulator_candidate = (accumulator + derived) & 0xFFFFFFFF
            if (accumulator_candidate & 0xFF) == target[round_number]:
                matches.append((candidate, state_candidate, accumulator_candidate))

        if len(matches) != 1:
            raise ValueError(
                f"round {round_number}: expected one byte candidate, found {len(matches)}"
            )

        value, state, accumulator = matches[0]
        recovered[position] = value
        index = (index + step) & 0xFFFFFFFF

    if any(value is None for value in recovered):
        raise ValueError("not every input position was recovered")
    return bytes(cast(list[int], recovered)).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", nargs="?", default="sugar_glass")
    args = parser.parse_args()
    path = Path(args.binary)
    flag = solve(path)

    raw = path.read_bytes()
    marker = raw.find(b"SGR2")
    length, step, initial_index, initial_state = raw[marker + 4 : marker + 8]
    print(f"[+] Binary: {path}")
    print(f"[+] SGR2 table offset: 0x{marker:x}")
    print(
        "[+] Parameters: "
        f"length={length}, step={step}, initial_index={initial_index}, "
        f"initial_state=0x{initial_state:02x}"
    )
    print(f"[+] Unique byte candidates recovered: {length}/{length}")
    print(f"[+] Accepted input: {flag}")


if __name__ == "__main__":
    main()
