#!/usr/bin/env python3
"""Static solver for the supplied Null Choir ELF + companion files."""
from pathlib import Path
import struct

MASK = (1 << 64) - 1
GOLD = 0x9e3779b97f4a7c15
SM_A = 0xbf58476d1ce4e5b9
SM_B = 0x94d049bb133111eb
REC_C = 0xd1b54a32d192ed03

ROOT = Path(__file__).resolve().parent
SCORE = (ROOT / "canticle.score").read_bytes()
TRACE = (ROOT / "echoes.trace").read_bytes()


def u64(b): return struct.unpack("<Q", b)[0]
def rol(x, n):
    n &= 63
    return ((x << n) | (x >> ((64 - n) & 63))) & MASK
def ror(x, n):
    n &= 63
    return ((x >> n) | (x << ((64 - n) & 63))) & MASK
def premix(x):
    x &= MASK
    x ^= x >> 30
    x = (x * SM_A) & MASK
    x ^= x >> 27
    return (x * SM_B) & MASK

def mix(x):
    x = premix(x)
    return (x ^ (x >> 31)) & MASK

def fnv1a32(b):
    h = 0x811c9dc5
    for c in b:
        h = ((h ^ c) * 0x1000193) & 0xffffffff
    return h

if SCORE[:4] != b"NCS2" or TRACE[:4] != b"ECH8":
    raise SystemExit("companion magic mismatch")
if len(SCORE) != 0x100 + 48 * 4096 or len(TRACE) % 16:
    raise SystemExit("companion size mismatch")

VM_STEPS = struct.unpack("<I", SCORE[4:8])[0]
if VM_STEPS != 4096:
    raise SystemExit(f"unexpected VM length {VM_STEPS}")
score_header = u64(SCORE[8:16])
const_a = 0x55cbc47c6a968ad5  # ELF .rodata + 0x20c4
const_b = 0xf8e87ff77c0a394f  # ELF .rodata + 0x20cc

# Trace-derived auxiliary registers at stack+0x190 in the ELF.
aux = [0] * 32
trace_key = (score_header >> 40) & 0xff
for off in range(0x20, len(TRACE), 16):
    ent = TRACE[off:off+16]
    if ent[:2] != b"EC" or ent[2] != trace_key or ent[3] > 31:
        continue
    if fnv1a32(ent[:12]) != struct.unpack("<I", ent[12:16])[0]:
        continue
    idx = ent[3]
    aux[idx] = mix((((idx + 1) * GOLD) & MASK) ^ score_header ^ 0xe0c412d8c2f7a366) ^ u64(ent[4:12])

# Reproduce the ELF's 32-step auxiliary-state seed chain.
a = 0
c = REC_C
for value in aux:
    a = mix(value ^ c ^ a)
    c = (c + REC_C) & MASK
rbp = mix(a ^ const_a ^ score_header ^ const_b ^ 0xe66cd00b5a17e123)
score_key0 = (rbp + GOLD) & MASK
# The ELF uses the remainder from rbp / 3 to choose one 0x10000-byte score bank.
score_bank = rbp % 3

def table_index(step):
    prev, prior = step & 63, step >> 6
    c = GOLD
    last3 = None
    for _ in range(4):
        nxt = (mix(prev ^ c ^ rbp) & 63) ^ prior
        prior, prev = prev, nxt
        c = (c + GOLD) & MASK
        last3 = prior
    return (last3 << 6) | prev

def decrypt_record(step):
    idx = table_index(step)
    off = 0x100 + score_bank * 0x10000 + idx * 16
    ct = SCORE[off:off+16]
    key_base = (score_key0 + step * GOLD) & MASK
    out = bytearray()
    for i, b in enumerate(ct):
        k = mix((((i + 1) * REC_C) & MASK) ^ key_base)
        out.append(b ^ ((k >> (8 * (i & 7))) & 0xff))
    return idx, bytes(out)

def switch_id(step, rec):
    return (rec[0] - 7 * step - ((rbp >> (8 * (step & 7))) & 0xffffffff)) & 31

def bitreverse64(x):
    x = (((x << 1) & 0xaaaaaaaaaaaaaaaa) | ((x >> 1) & 0x5555555555555555))
    x = (((x << 2) & 0xcccccccccccccccc) | ((x >> 2) & 0x3333333333333333))
    x = (((x << 4) & 0xf0f0f0f0f0f0f0f0) | ((x >> 4) & 0x0f0f0f0f0f0f0f0f))
    return int.from_bytes(x.to_bytes(8, "little"), "big")

def apply_op(state, op, d, s, imm, rot):
    """Exact forward semantics of the 24 dispatched VM instructions."""
    if op == 0: state[d] ^= (imm + state[s]) & MASK
    elif op == 1: state[d] = (state[d] + rol(imm ^ state[s], rot)) & MASK
    elif op == 2: state[d] ^= rol((imm + state[s]) & MASK, rot)
    elif op == 3:
        old_d, old_s = state[d], state[s]
        state[d] = old_s
        state[s] = old_d ^ mix((imm + old_s) & MASK)
    elif op == 4: state[d], state[s] = state[s], state[d]
    elif op == 5: state[d] = rol(state[d], rot)
    elif op == 6: state[d] ^= imm
    elif op == 7: state[d] = (state[d] + imm) & MASK
    elif op == 8: state[d] = int.from_bytes(state[d].to_bytes(8, "little"), "big")
    elif op == 9: state[d] ^= imm ^ state[s]
    elif op == 10: state[d] = (state[d] - rol(imm ^ state[s], rot)) & MASK
    elif op == 11:
        old = state[:]
        q = rot % 6
        for i in range(6): state[(q + i) % 6] = old[i]
    elif op == 12: state[d] = (~state[d] ^ imm) & MASK
    elif op == 13: state[d] = bitreverse64(state[d])
    elif op == 14: state[d] = rol(imm ^ state[d], rot)
    elif op == 15: state[d] = ((imm ^ state[d]) + rol(state[s], rot)) & MASK
    elif op == 16: state[d] ^= mix(imm ^ state[s])
    elif op == 17: state[d] = (state[d] + mix((imm + state[s]) & MASK)) & MASK
    elif op == 18: state[d] = rol((imm + state[d]) & MASK, rot)
    elif op == 19: state[d] = mix((imm + state[s]) & MASK) ^ rol(state[d], rot)
    elif op == 20: state[d] ^= imm & state[s]
    elif op == 21: state[d] = (state[d] + (rol(imm, rot) ^ state[s])) & MASK
    elif op == 22: state[d] = (~rol(state[d] ^ mix(imm), rot)) & MASK
    elif op == 23: state[d] ^= rol(mix(imm ^ state[s]), rot)
    elif op < 32: pass
    else: raise ValueError(op)
    state[d] &= MASK

def unapply_op(state, op, d, s, imm, rot):
    """Inverse of apply_op; score instructions are reversed by the caller."""
    if op == 0: state[d] ^= (imm + state[s]) & MASK
    elif op == 1: state[d] = (state[d] - rol(imm ^ state[s], rot)) & MASK
    elif op == 2: state[d] ^= rol((imm + state[s]) & MASK, rot)
    elif op == 3:
        new_d, new_s = state[d], state[s]
        state[s] = new_d
        state[d] = new_s ^ mix((imm + new_d) & MASK)
    elif op == 4: state[d], state[s] = state[s], state[d]
    elif op == 5: state[d] = ror(state[d], rot)
    elif op == 6: state[d] ^= imm
    elif op == 7: state[d] = (state[d] - imm) & MASK
    elif op == 8: state[d] = int.from_bytes(state[d].to_bytes(8, "little"), "big")
    elif op == 9: state[d] ^= imm ^ state[s]
    elif op == 10: state[d] = (state[d] + rol(imm ^ state[s], rot)) & MASK
    elif op == 11:
        new = state[:]
        q = rot % 6
        for i in range(6): state[i] = new[(q + i) % 6]
    elif op == 12: state[d] = (~state[d] ^ imm) & MASK
    elif op == 13: state[d] = bitreverse64(state[d])
    elif op == 14: state[d] = ror(state[d], rot) ^ imm
    elif op == 15: state[d] = ((state[d] - rol(state[s], rot)) & MASK) ^ imm
    elif op == 16: state[d] ^= mix(imm ^ state[s])
    elif op == 17: state[d] = (state[d] - mix((imm + state[s]) & MASK)) & MASK
    elif op == 18: state[d] = (ror(state[d], rot) - imm) & MASK
    elif op == 19: state[d] = ror(state[d] ^ mix((imm + state[s]) & MASK), rot)
    elif op == 20: state[d] ^= imm & state[s]
    elif op == 21: state[d] = (state[d] - (rol(imm, rot) ^ state[s])) & MASK
    elif op == 22: state[d] = ror((~state[d]) & MASK, rot) ^ mix(imm)
    elif op == 23: state[d] ^= rol(mix(imm ^ state[s]), rot)
    elif op < 32: pass
    else: raise ValueError(op)
    state[d] &= MASK

def apply_boundary(state, step):
    """The ELF's six-register mixer executed after every 64th score record."""
    rdi = rbp ^ step
    for i in range(6):
        delta = mix(state[i] ^ mix(((i * GOLD) & MASK) ^ rdi))
        state[(i + 2) % 6] = (state[(i + 2) % 6] + delta) & MASK

def unapply_boundary(state, step):
    rdi = rbp ^ step
    for i in range(5, -1, -1):
        delta = mix(state[i] ^ mix(((i * GOLD) & MASK) ^ rdi))
        state[(i + 2) % 6] = (state[(i + 2) % 6] - delta) & MASK

def inv_self_add_rol_xor(y, imm, rot):
    """Solve y = x + rol(x ^ imm, rot) (mod 2**64) by bit propagation."""
    r = rot & 63
    if r == 0:
        # y = 2*x + imm: the same two-root case as a self-referential op 21.
        t = (y - imm) & MASK
        return [] if t & 1 else [t >> 1, (t >> 1) | (1 << 63)]
    roots = []
    for guess in range(1 << r):
        xb = [0] * 64
        for k in range(r): xb[64 - r + k] = (guess >> k) & 1
        carry = 0
        okay = True
        for i in range(64):
            z = xb[(i - r) & 63] ^ ((imm >> ((i - r) & 63)) & 1)
            want = ((y >> i) & 1) ^ z ^ carry
            if i < 64 - r:
                xb[i] = want
            elif xb[i] != want:
                okay = False
                break
            carry = (xb[i] & z) | (xb[i] & carry) | (z & carry)
        if okay:
            roots.append(sum(bit << i for i, bit in enumerate(xb)))
    return roots

def initial_forward(input_bytes):
    words = list(struct.unpack("<6Q", input_bytes))
    return [rol(word ^ mix(rbp ^ (((i + 1) * GOLD) & MASK)), 14 + 7 * i)
            for i, word in enumerate(words)]

def initial_inverse(state):
    words = [ror(encoded, 14 + 7 * i) ^ mix(rbp ^ (((i + 1) * GOLD) & MASK))
             for i, encoded in enumerate(state)]
    return struct.pack("<6Q", *words)

def target_state():
    out = bytearray()
    for i in range(48):
        key = mix(rbp ^ (((i + 1) * REC_C) & MASK) ^ 0x8bf41bd2368a339d)
        out.append(SCORE[0x20 + i] ^ ((key >> (8 * (i & 7))) & 0xff))
    return list(struct.unpack("<6Q", out))

def load_records():
    records = []
    for step in range(VM_STEPS):
        idx, rec = decrypt_record(step)
        if not (rec[1] <= 5 and rec[2] <= 5 and 1 <= rec[4] <= 63 and rec[5] == (step & 0xff) and rec[6:8] == b"\x7a\xa7"):
            raise ValueError(f"bad record at step {step}: {rec.hex()}")
        records.append((switch_id(step, rec), rec[1], rec[2], u64(rec[8:]), rec[4], step, idx))
    return records

def execute(input_bytes):
    state = initial_forward(input_bytes)
    for op, d, s, imm, rot, step, _ in load_records():
        apply_op(state, op, d, s, imm, rot)
        if (step & 63) == 63:
            apply_boundary(state, step)
    return state

def solve_candidate():
    state = target_state()
    records = load_records()
    for op, d, s, imm, rot, step, _ in reversed(records):
        if (step & 63) == 63:
            unapply_boundary(state, step)
        unapply_op(state, op, d, s, imm, rot)
    candidate = initial_inverse(state)
    if execute(candidate) != target_state():
        raise AssertionError("reversed VM did not round-trip")
    return candidate

if __name__ == "__main__":
    records = load_records()
    candidate = solve_candidate()
    print(f"score_header=0x{score_header:016x} rbp=0x{rbp:016x} score_bank={score_bank} trace_key=0x{trace_key:02x}")
    print("validated_vm_records=" + str(len(records)))
    print("candidate_hex=" + candidate.hex())
    print("candidate_ascii=" + repr(candidate))
