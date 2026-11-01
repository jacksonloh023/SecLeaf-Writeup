#!/usr/bin/env python3
"""Demodulate the Bell 202 AFSK/UART burst hidden in signal.wav."""

import array
import math
import statistics
import sys
import wave
from pathlib import Path

BAUD = 1200
MARK_FREQUENCY = 1200  # Logical 1
SPACE_FREQUENCY = 2200  # Logical 0


def rms(samples):
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def find_burst(samples, sample_rate):
    block_size = sample_rate // 100  # 10 ms
    levels = []
    for offset in range(0, len(samples) - block_size + 1, block_size):
        levels.append(rms(samples[offset : offset + block_size]))

    noise_level = statistics.median(levels)
    threshold = noise_level * 5
    active = [index for index, level in enumerate(levels) if level > threshold]
    if not active:
        raise ValueError("No high-energy burst found")

    first_block = active[0]
    last_block = active[-1]
    start = first_block * block_size
    end = min(len(samples), (last_block + 1) * block_size)

    # In this challenge the burst continues to the end of the file. Preserve
    # the final partial block rather than truncating valid symbols.
    if len(samples) - end < block_size:
        end = len(samples)

    return start, end, noise_level, threshold


def tone_energy(samples, frequency, sample_rate):
    mean = sum(samples) / len(samples)
    cosine = 0.0
    sine = 0.0
    step = 2 * math.pi * frequency / sample_rate
    for index, sample in enumerate(samples):
        centered = sample - mean
        angle = step * index
        cosine += centered * math.cos(angle)
        sine += centered * math.sin(angle)
    return cosine * cosine + sine * sine


def demodulate_symbols(samples, sample_rate, baud):
    samples_per_symbol = sample_rate / baud
    symbol_count = round(len(samples) / samples_per_symbol)
    bits = []
    confidences = []

    for symbol in range(symbol_count):
        start = round(symbol * samples_per_symbol)
        end = round((symbol + 1) * samples_per_symbol)
        if end > len(samples):
            break
        block = samples[start:end]
        mark = tone_energy(block, MARK_FREQUENCY, sample_rate)
        space = tone_energy(block, SPACE_FREQUENCY, sample_rate)
        bits.append(1 if mark >= space else 0)
        confidences.append(abs(mark - space) / (mark + space + 1e-30))

    return bits, confidences, samples_per_symbol


def decode_uart(bits):
    candidates = []
    for offset in range(10):
        decoded = []
        valid_frames = 0
        printable = 0

        for index in range(offset, len(bits) - 9, 10):
            frame = bits[index : index + 10]
            start_ok = frame[0] == 0
            stop_ok = frame[9] == 1
            value = sum(frame[1 + bit] << bit for bit in range(8))
            valid = start_ok and stop_ok
            valid_frames += int(valid)
            printable += int(valid and (32 <= value < 127 or value in (9, 10, 13)))
            decoded.append(value if valid else None)

        candidates.append((valid_frames, printable, len(decoded), offset, decoded))

    return max(candidates, key=lambda item: (item[0], item[1]))


def main():
    wav_path = Path(sys.argv[1] if len(sys.argv) > 1 else "signal.wav")
    with wave.open(str(wav_path), "rb") as wav_file:
        if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
            raise ValueError("Expected a 16-bit mono PCM WAV")
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        samples = array.array("h", wav_file.readframes(frame_count))

    start, end, noise_level, threshold = find_burst(samples, sample_rate)
    burst = samples[start:end]
    bits, confidences, samples_per_symbol = demodulate_symbols(
        burst, sample_rate, BAUD
    )
    valid, printable, frames, offset, decoded = decode_uart(bits)
    payload = bytes(value for value in decoded if value is not None)
    text = payload.decode("ascii")

    print(f"[+] Input: {wav_path.name}")
    print(f"[+] Format: PCM 16-bit mono at {sample_rate} Hz")
    print(f"[+] Total duration: {len(samples) / sample_rate:.6f} s")
    print(f"[+] Estimated noise RMS: {noise_level:.2f}")
    print(f"[+] Burst threshold: {threshold:.2f}")
    print(f"[+] Burst start: {start / sample_rate:.6f} s")
    print(f"[+] Burst samples: {len(burst)}")
    print(f"[+] Burst duration: {len(burst) / sample_rate:.6f} s")
    print(f"[+] Modulation: Bell 202 AFSK, {BAUD} baud")
    print(f"[+] Mark/1: {MARK_FREQUENCY} Hz; Space/0: {SPACE_FREQUENCY} Hz")
    print(f"[+] Samples per symbol: {samples_per_symbol:.4f}")
    print(f"[+] Symbols recovered: {len(bits)}")
    print(f"[+] Mean tone confidence: {statistics.mean(confidences):.4f}")
    print(f"[+] UART format: 8N1, LSB first")
    print(f"[+] Best UART bit offset: {offset}")
    print(f"[+] Valid UART frames: {valid}/{frames}")
    print(f"[+] Printable/control bytes: {printable}/{frames}")
    print(f"[+] Payload hex: {payload.hex()}")
    print(f"[+] Payload repr: {text!r}")
    print(f"[+] Secret message: {text.rstrip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
