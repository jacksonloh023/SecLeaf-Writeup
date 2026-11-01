#!/usr/bin/env python3
"""Recover the ECDSA private scalar from an affine nonce window."""

import hashlib
import json
import sys
from pathlib import Path

from Crypto.Cipher import AES

# secp256k1 field prime, group order, and generator.
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    55066263022277343669578718895168534326250603453777594175500187360389116729240,
    32670510020758816978083085130507043184471273380659243275938904335757337482424,
)


def digest(message):
    return int.from_bytes(hashlib.sha256(message.encode()).digest(), "big") % N


def point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left

    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % P == 0:
        return None

    if left == right:
        slope = (3 * x1 * x1) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow((x2 - x1) % P, -1, P) % P

    x3 = (slope * slope - x1 - x2) % P
    y3 = (slope * (x1 - x3) - y1) % P
    return x3, y3


def scalar_multiply(scalar, point=G):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        scalar >>= 1
    return result


def linear_subtract(left, right):
    """Subtract (a*d+b) linear forms represented as (a,b)."""
    return ((left[0] - right[0]) % N, (left[1] - right[1]) % N)


def linear_product(left, right):
    """Multiply linear forms, returning quadratic coefficients d^2,d,1."""
    return (
        left[0] * right[0] % N,
        (left[0] * right[1] + left[1] * right[0]) % N,
        left[1] * right[1] % N,
    )


def quadratic_subtract(left, right):
    return tuple((a - b) % N for a, b in zip(left, right))


def quadratic_value(poly, value):
    return (poly[0] * value * value + poly[1] * value + poly[2]) % N


def candidate_from_window(forms):
    """Recover d from five k_i = alpha_i*d+beta_i affine nonce forms."""
    differences = [
        linear_subtract(forms[index + 1], forms[index]) for index in range(4)
    ]

    # Affine recursion implies D1^2=D0*D2 and D2^2=D1*D3.
    first = quadratic_subtract(
        linear_product(differences[1], differences[1]),
        linear_product(differences[0], differences[2]),
    )
    second = quadratic_subtract(
        linear_product(differences[2], differences[2]),
        linear_product(differences[1], differences[3]),
    )

    # Eliminate d^2: second[0]*first - first[0]*second is linear.
    coefficient = (second[0] * first[1] - first[0] * second[1]) % N
    constant = (second[0] * first[2] - first[0] * second[2]) % N
    if coefficient == 0:
        return None

    private_scalar = (-constant * pow(coefficient, -1, N)) % N
    return private_scalar, first, second


def derive_key(private_scalar):
    material = (
        b"SecLeaf/nonce-nursery/v2/" + private_scalar.to_bytes(32, "big")
    )
    return hashlib.sha256(material).digest()


def decrypt_flag(sealed, private_scalar):
    nonce = bytes.fromhex(sealed["nonce"])
    encrypted = bytes.fromhex(sealed["ct"])
    ciphertext, tag = encrypted[:-16], encrypted[-16:]
    cipher = AES.new(derive_key(private_scalar), AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


def main():
    data_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data.json")
    data = json.loads(data_path.read_text())
    public_key = tuple(int(value, 16) for value in data["public_key"])

    records = []
    forms = []
    for record in data["records"]:
        r = int(record["r"], 16)
        s = int(record["s"], 16)
        h = digest(record["message"])
        inverse_s = pow(s, -1, N)
        alpha = r * inverse_s % N
        beta = h * inverse_s % N
        records.append((r, s, h))
        forms.append((alpha, beta))

    print(f"[+] Input: {data_path.name}")
    print(f"[+] Curve: {data['curve']}")
    print(f"[+] Signatures: {len(records)}")
    print("[+] Testing every consecutive length-5 window")

    valid_candidates = []
    for start in range(len(records) - 4):
        result = candidate_from_window(forms[start : start + 5])
        if result is None:
            print(f"    window {start:02d}-{start + 4:02d}: degenerate")
            continue

        private_scalar, first_poly, second_poly = result
        polynomial_check = (
            quadratic_value(first_poly, private_scalar) == 0
            and quadratic_value(second_poly, private_scalar) == 0
        )

        window_forms = forms[start : start + 5]
        nonces = [
            (alpha * private_scalar + beta) % N
            for alpha, beta in window_forms
        ]
        difference = (nonces[1] - nonces[0]) % N
        if difference == 0:
            recurrence_check = False
            multiplier = increment = 0
        else:
            multiplier = (
                (nonces[2] - nonces[1]) * pow(difference, -1, N)
            ) % N
            increment = (nonces[1] - multiplier * nonces[0]) % N
            recurrence_check = all(
                nonces[index + 1]
                == (multiplier * nonces[index] + increment) % N
                for index in range(4)
            )

        public_key_check = scalar_multiply(private_scalar) == public_key
        valid = polynomial_check and recurrence_check and public_key_check
        print(
            f"    window {start:02d}-{start + 4:02d}: "
            f"polynomials={polynomial_check} "
            f"recurrence={recurrence_check} public_key={public_key_check}"
        )
        if valid:
            valid_candidates.append(
                (start, private_scalar, multiplier, increment, nonces, public_key_check)
            )

    if len(valid_candidates) != 1:
        print(f"[-] Expected one valid affine nonce window; found {len(valid_candidates)}")
        return 1

    start, private_scalar, multiplier, increment, nonces, public_key_check = valid_candidates[0]
    all_nonces = [
        (alpha * private_scalar + beta) % N for alpha, beta in forms
    ]
    all_r_checks = []
    for nonce, record in zip(all_nonces, records):
        nonce_point = scalar_multiply(nonce)
        all_r_checks.append(
            nonce_point is not None and nonce_point[0] % N == record[0]
        )
    flag = decrypt_flag(data["sealed"], private_scalar)

    print(f"[+] Valid affine windows: {len(valid_candidates)}")
    print(f"[+] Vulnerable window: {start:02d}-{start + 4:02d}")
    print(f"[+] Private scalar: {private_scalar}")
    print(f"[+] Private scalar hex: {private_scalar:064x}")
    print(f"[+] Affine multiplier A: {multiplier}")
    print(f"[+] Affine increment B: {increment}")
    print(f"[+] Window nonces:")
    for index, nonce in enumerate(nonces, start=start):
        print(f"    k[{index:02d}] = {nonce}")
    print(f"[+] Public-key check: {public_key_check}")
    print(f"[+] All 11 nonce/r checks: {all(all_r_checks)}")
    print(f"[+] AES-GCM authentication: valid")
    print(f"[+] Flag: {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
