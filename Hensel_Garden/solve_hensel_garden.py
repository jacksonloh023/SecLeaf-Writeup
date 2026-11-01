#!/usr/bin/env python3
"""Solve Hensel Garden from the released data.json."""

import hashlib
import itertools
import json
import math
import sys
from pathlib import Path

from Crypto.Cipher import AES


def identify_prime_power(modulus):
    """Return (prime, exponent) for the supplied 2^k or 3^k modulus."""
    for prime in (2, 3):
        value = modulus
        exponent = 0
        while value % prime == 0:
            value //= prime
            exponent += 1
        if value == 1:
            return prime, exponent
    raise ValueError(f"unsupported modulus: {modulus}")


def hensel_square_roots(value, prime, exponent):
    """Lift every root of x^2 = value from mod p through mod p^exponent."""
    modulus = prime
    roots = [x for x in range(prime) if (x * x - value) % prime == 0]

    for _ in range(1, exponent):
        next_modulus = modulus * prime
        lifted = set()
        for root in roots:
            for digit in range(prime):
                candidate = root + digit * modulus
                if (candidate * candidate - value) % next_modulus == 0:
                    lifted.add(candidate)
        roots = sorted(lifted)
        modulus = next_modulus

    return roots


def crt_pair(a, modulus_a, b, modulus_b):
    """Chinese Remainder Theorem for two coprime moduli."""
    if math.gcd(modulus_a, modulus_b) != 1:
        raise ValueError("CRT moduli must be coprime")
    adjustment = ((b - a) * pow(modulus_a, -1, modulus_b)) % modulus_b
    return (a + modulus_a * adjustment) % (modulus_a * modulus_b)


def derive_key(seed):
    material = b"SecLeaf/hensel/v2/" + seed.to_bytes(16, "big")
    return hashlib.sha256(material).digest()


def decrypt_flag(sealed, seed):
    nonce = bytes.fromhex(sealed["nonce"])
    encrypted = bytes.fromhex(sealed["ct"])
    ciphertext, tag = encrypted[:-16], encrypted[-16:]
    cipher = AES.new(derive_key(seed), AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


def main():
    data_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data.json")
    data = json.loads(data_path.read_text())

    moduli = [int(value) for value in data["moduli"]]
    shifts = data["shifts"]
    squares = [int(value) for value in data["squares"]]
    seed_limit = 1 << data["bits"]

    print(f"[+] Input: {data_path.name}")
    print(f"[+] Seed bound: 0 <= seed < 2^{data['bits']}")

    residue_sets = []
    for index, (modulus, shift, square) in enumerate(
        zip(moduli, shifts, squares), start=1
    ):
        prime, exponent = identify_prime_power(modulus)
        roots = hensel_square_roots(square, prime, exponent)
        seed_residues = sorted({(root - shift) % modulus for root in roots})
        residue_sets.append(seed_residues)

        print(f"[+] Observation {index}: modulus = {prime}^{exponent}")
        print(f"    square roots ({len(roots)}): {roots}")
        print(f"    seed residues ({len(seed_residues)}): {seed_residues}")

    combined_modulus = math.prod(moduli)
    combinations = math.prod(len(values) for values in residue_sets)
    print(f"[+] CRT combinations: {combinations}")
    print(f"[+] Combined modulus: {combined_modulus}")
    print(f"[+] Combined modulus exceeds 2^{data['bits']}: {combined_modulus > seed_limit}")

    tested = 0
    for residues in itertools.product(*residue_sets):
        seed = crt_pair(residues[0], moduli[0], residues[1], moduli[1])
        if seed >= seed_limit:
            continue
        tested += 1
        try:
            flag = decrypt_flag(data["sealed"], seed)
        except ValueError:
            continue

        checks = [
            pow(seed + shift, 2, modulus) == square
            for shift, modulus, square in zip(shifts, moduli, squares)
        ]
        print(f"[+] In-range CRT candidates tested: {tested}")
        print(f"[+] Recovered seed: {seed}")
        print(f"[+] Seed hex: {seed:032x}")
        print(f"[+] Public observation checks: {checks}")
        print(f"[+] AES-GCM authentication: valid")
        print(f"[+] Flag: {flag}")
        return 0

    print(f"[-] No valid seed found after testing {tested} in-range candidates")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
