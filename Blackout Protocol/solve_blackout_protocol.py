#!/usr/bin/env python3
"""Recover and validate the Blackout Protocol accepted 48-byte input.

The supplied ELF is stripped PIE.  This solver verifies all supplied artifact
hashes and formats, uses GDB only on a disposable copy to obtain the verifier's
artifact-derived 384-byte target state/S-box, then exactly inverts its 52-round
byte permutation.  It never changes an input artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

MASK = (1 << 64) - 1
PHI = 0x9E3779B97F4A7C15
M1 = 0xBF58476D1CE4E5B9
M2 = 0x94D049BB133111EB
PACKET_C = 0xD1B54A32D192ED03
CHECK_C = 0x8BF41BD2368A339D
HASHES = {
    "blackout_protocol": "8b03bd3928ed0c362c193c88533dcbf3e8d05cc61146e1f7dc8ffa61773bb529",
    "incident.journal": "8e4346f69be0a9dcd8a90406a8560bfda219701637424357f6eb3f702864e857",
    "retained.mem": "848661122b5c8c3ad729085fe0626f10d1945f1a8f9aab582a80f76e7c149d28",
    "transmission.pcap": "fe5a71d9509942fe2b04c344ae17ae1f3a028fd14bc90628ddac324c15f0e392",
}


def rol8(x: int, n: int) -> int:
    n &= 7
    return (((x << n) | (x >> (8 - n))) & 0xFF) if n else x


def mix(x: int) -> int:
    x &= MASK
    x ^= x >> 30
    x = (x * M1) & MASK
    x ^= x >> 27
    x = (x * M2) & MASK
    x ^= x >> 31
    return x & MASK


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def pcap_udp_payloads(data: bytes) -> list[bytes]:
    assert data[:24] == bytes.fromhex("d4c3b2a1020004000000000000000000ffff000001000000")
    off, payloads = 24, []
    while off < len(data):
        _, _, caplen, _ = struct.unpack_from("<IIII", data, off)
        off += 16
        frame = data[off:off + caplen]
        off += caplen
        if len(frame) < 42 or frame[12:14] != b"\x08\x00":
            continue
        ip = 14
        ihl = (frame[ip] & 15) * 4
        if frame[ip + 9] != 17:
            continue
        udp = ip + ihl
        ulen = struct.unpack_from(">H", frame, udp + 4)[0]
        payloads.append(frame[udp + 8:udp + ulen])
    return payloads


def audit_artifacts(root: Path) -> bytes:
    for name, expected in HASHES.items():
        got = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if got != expected:
            raise SystemExit(f"refusing altered artifact {name}: {got}")

    mem = (root / "retained.mem").read_bytes()
    journal = (root / "incident.journal").read_bytes()
    payloads = pcap_udp_payloads((root / "transmission.pcap").read_bytes())
    if len(mem) != 0x4000 or mem[0x180:0x184] != b"M16B":
        raise SystemExit("retained.mem format check failed")
    if journal[:4] != b"JRN4" or (len(journal) - 0x20) % 24:
        raise SystemExit("incident.journal format check failed")
    if len(payloads) != 290 or any(len(x) != 24 or x[:4] != b"BKP5" for x in payloads):
        raise SystemExit("transmission.pcap BKP5 frame check failed")

    marker = mem[0x184]
    records = [journal[i:i + 24] for i in range(0x20, len(journal), 24)]
    active = [r for r in records if r[:2] == b"JR" and r[2] == marker and r[3] < 48 and fnv1a32(r[:12]) == struct.unpack_from("<I", r, 12)[0]]
    if len(active) != 48 or sorted(r[3] for r in active) != list(range(48)):
        raise SystemExit("journal selection/checksum validation failed")
    if sum(p[4] == marker for p in payloads) != 240:
        raise SystemExit("BKP57 marker count changed")
    print(f"artifact checks: 290 UDP BKP5 frames; 240 BKP{marker:02x} candidates; 48 authenticated JR records")
    return mem


def debugger_state(root: Path) -> tuple[int, bytes, bytes]:
    """Get the post-artifact target/S-box from a disposable binary process."""
    with tempfile.TemporaryDirectory(prefix="blackout-solve-") as td:
        work = Path(td)
        for name in HASHES:
            shutil.copy2(root / name, work / name)
        exe = work / "blackout_protocol"
        exe.chmod(exe.stat().st_mode | 0o100)
        (work / "input.txt").write_text("A" * 48 + "\n", encoding="ascii")
        gdb = r'''set pagination off
set confirm off
set disable-randomization on
starti < input.txt
set $base = 0x555555554000
break *($base + 0x193b)
continue
printf "--- at 0x193b ---\n"
info registers r15 r13
x/256bx $r13
break *($base + 0x1b3a)
continue
printf "--- at 0x1b3a ---\n"
x/64bx $rbx
x/384bx $rbp
quit
'''
        cmd = work / "extract_state.gdb"
        cmd.write_text(gdb)
        result = subprocess.run(["gdb", "-q", "-batch", "-x", str(cmd), str(exe)], cwd=work, text=True, capture_output=True, check=True)
        text = result.stdout
        lines = text.splitlines()
        a = lines.index("--- at 0x193b ---")
        b = lines.index("Breakpoint 2 at 0x555555555b3a")
        seed = int(re.search(r"r15\s+0x([0-9a-f]+)", "\n".join(lines[a:b])).group(1), 16)
        first = []
        for line in lines[a:b]:
            if re.match(r"0x[0-9a-f]+:", line):
                first.extend(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2})", line.split(":", 1)[1]))
        sbox = bytes(first[-256:])
        c = lines.index("--- at 0x1b3a ---")
        second = []
        for line in lines[c + 1:]:
            if re.match(r"0x[0-9a-f]+:", line):
                second.extend(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2})", line.split(":", 1)[1]))
        target = bytes(second[64:64 + 384])  # first x/64 is transformed probe input
        if len(sbox) != 256 or len(target) != 384 or len(set(sbox)) != 256:
            raise SystemExit("failed to extract verifier state")
        return seed, sbox, target


def prf(seed: int, target: bytes, round_no: int, pos: int) -> int:
    x = (((round_no + 1) * PHI + seed) & MASK) ^ (((pos + 1) * PACKET_C) & MASK) ^ target[(17 * round_no + pos) % 384]
    return mix(x) >> ((pos & 7) * 8)


def forward(buf: bytes, seed: int, target: bytes, sbox: bytes) -> bytes:
    b = list(buf)
    for r in range(52):
        b = [sbox[x ^ (prf(seed, target, r, i) & 0xFF)] for i, x in enumerate(b)]
        q = [0] * 48
        for i, x in enumerate(b):
            q[(r * 7 + 11 + 17 * i) % 48] = x
        b = q
        state = b[0]
        for i in range(1, 48):
            state = (rol8(state, ((r + i) % 7) + 1) + b[i]) & 0xFF
            b[i] = state
        state = b[47]
        for i in range(46, -1, -1):
            state = rol8(state, ((r + i) % 5) + 1) ^ b[i]
            b[i] = state
        if r % 7 == 6:
            b[0] ^= prf(seed, target, r, 47) & 0xFF
            state = b[0]
            for i in range(1, 48):
                state = rol8(state, ((r + i) % 7) + 1) ^ b[i]
                b[i] = state
        elif r % 13 == 12:
            for i in range(48):
                b[i] = (b[i] + rol8(b[(i + 5) % 48], ((r + i) % 6) + 1)) & 0xFF
    return bytes(b)


def required_final(seed: int, target: bytes, mem: bytes) -> bytes:
    out = bytearray()
    for i in range(48):
        x = (((i + 1) * PHI) & MASK) ^ (seed ^ target[30 + 7 * i]) ^ CHECK_C
        out.append(mem[0x600 + ((i * 0x89 + 0x3B) & 0x1FF)] ^ ((mix(x) >> (8 * (i & 7))) & 0xFF))
    return bytes(out)


def invert(goal: bytes, seed: int, target: bytes, sbox: bytes) -> bytes:
    inverse_sbox = [0] * 256
    for i, value in enumerate(sbox):
        inverse_sbox[value] = i
    b = list(goal)
    for r in range(51, -1, -1):
        if r % 7 == 6:
            for i in range(47, 0, -1):
                b[i] ^= rol8(b[i - 1], ((r + i) % 7) + 1)
            b[0] ^= prf(seed, target, r, 47) & 0xFF
        elif r % 13 == 12:
            for i in range(47, -1, -1):
                b[i] = (b[i] - rol8(b[(i + 5) % 48], ((r + i) % 6) + 1)) & 0xFF
        for i in range(47):
            b[i] ^= rol8(b[i + 1], ((r + i) % 5) + 1)
        for i in range(47, 0, -1):
            b[i] = (b[i] - rol8(b[i - 1], ((r + i) % 7) + 1)) & 0xFF
        q = b[:]
        b = [q[(r * 7 + 11 + 17 * i) % 48] for i in range(48)]
        b = [inverse_sbox[x] ^ (prf(seed, target, r, i) & 0xFF) for i, x in enumerate(b)]
    return bytes(b)


def validate(root: Path, answer: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="blackout-validate-") as td:
        work = Path(td)
        for name in HASHES:
            shutil.copy2(root / name, work / name)
        exe = work / "blackout_protocol"
        exe.chmod(exe.stat().st_mode | 0o100)
        result = subprocess.run([str(exe), answer.decode("ascii")], cwd=work, text=True, capture_output=True, check=True)
        return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_dir", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.artifact_dir).resolve()
    mem = audit_artifacts(root)
    seed, sbox, target = debugger_state(root)
    answer = invert(required_final(seed, target, mem), seed, target, sbox)
    if forward(answer, seed, target, sbox) != required_final(seed, target, mem):
        raise SystemExit("round-trip inversion failed")
    result = validate(root, answer)
    if result != "Authenticated.":
        raise SystemExit(f"binary validation failed: {result!r}")
    print("accepted input:", answer.decode("ascii"))
    print("binary validation:", result)

if __name__ == "__main__":
    main()
