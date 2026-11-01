#!/usr/bin/env python3
"""Extract and verify DNS-label exfiltration from the LeafSync capture."""

from __future__ import annotations

import argparse
import base64
import socket
import struct
from pathlib import Path

PARENT_DOMAIN = "sync-cache.training.local"
DNS_RCODES = {0: "NOERROR", 3: "NXDOMAIN"}


def pcap_endian(data: bytes) -> str:
    mapping = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }
    try:
        return mapping[data[:4]]
    except KeyError as error:
        raise ValueError("unsupported or invalid PCAP magic") from error


def parse_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    end_offset: int | None = None
    visited = set()
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        if length == 0:
            offset += 1
            return ".".join(labels), end_offset if end_offset is not None else offset
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if pointer in visited:
                raise ValueError("DNS compression pointer loop")
            visited.add(pointer)
            if end_offset is None:
                end_offset = offset + 2
            offset = pointer
            continue
        if length & 0xC0 or offset + 1 + length > len(data):
            raise ValueError("invalid DNS label")
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii"))
        offset += length


def parse_dns(payload: bytes) -> dict:
    if len(payload) < 12:
        raise ValueError("truncated DNS message")
    transaction_id, flags, qdcount, _, _, _ = struct.unpack_from("!HHHHHH", payload)
    if qdcount != 1:
        raise ValueError("expected one DNS question")
    question_name, offset = parse_dns_name(payload, 12)
    question_type, _ = struct.unpack_from("!HH", payload, offset)
    return {
        "id": transaction_id,
        "is_response": bool(flags & 0x8000),
        "rcode": flags & 0x0F,
        "question_name": question_name,
        "question_type": question_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", nargs="?", default="leafsync.pcap")
    args = parser.parse_args()
    capture = Path(args.capture)
    raw = capture.read_bytes()
    if len(raw) < 24:
        raise ValueError("truncated PCAP global header")

    endian = pcap_endian(raw)
    position = 24
    packet_number = 0
    queries = []
    responses = {}
    http_payloads = []

    while position + 16 <= len(raw):
        _, _, included_length, _ = struct.unpack_from(endian + "IIII", raw, position)
        position += 16
        frame = raw[position : position + included_length]
        position += included_length
        packet_number += 1

        if len(frame) < 34 or struct.unpack_from("!H", frame, 12)[0] != 0x0800:
            continue
        ip_offset = 14
        ip_header_length = (frame[ip_offset] & 0x0F) * 4
        if ip_header_length < 20 or len(frame) < ip_offset + ip_header_length:
            continue

        source = socket.inet_ntoa(frame[ip_offset + 12 : ip_offset + 16])
        destination = socket.inet_ntoa(frame[ip_offset + 16 : ip_offset + 20])
        protocol = frame[ip_offset + 9]
        transport_offset = ip_offset + ip_header_length

        if protocol == 6 and len(frame) >= transport_offset + 20:
            tcp_header_length = ((frame[transport_offset + 12] >> 4) & 0x0F) * 4
            if tcp_header_length >= 20 and len(frame) >= transport_offset + tcp_header_length:
                payload = frame[transport_offset + tcp_header_length :]
                if payload:
                    http_payloads.append((packet_number, payload))
            continue

        if protocol != 17 or len(frame) < transport_offset + 8:
            continue
        source_port, destination_port, _, _ = struct.unpack_from("!HHHH", frame, transport_offset)
        if source_port != 53 and destination_port != 53:
            continue

        try:
            dns = parse_dns(frame[transport_offset + 8 :])
        except ValueError:
            continue
        if not dns["question_name"].endswith("." + PARENT_DOMAIN):
            continue

        record = (packet_number, source, source_port, destination, destination_port, dns)
        if dns["is_response"]:
            responses[(destination, destination_port, source, dns["id"])] = record
        elif destination_port == 53:
            labels = dns["question_name"].split(".")
            if len(labels) >= 3 and labels[0].isdigit():
                queries.append((*record, int(labels[0]), labels[1]))

    if not queries:
        raise SystemExit("No numbered DNS labels found for the expected parent domain")

    ordered = sorted(queries, key=lambda row: row[6])
    sequence = [row[6] for row in ordered]
    if sequence != list(range(1, len(ordered) + 1)):
        raise SystemExit(f"unexpected sequence values: {sequence}")
    encoded = "".join(row[7] for row in ordered)
    decoded = base64.b64decode(encoded, validate=True).decode("utf-8")

    matched_responses = []
    for query in ordered:
        key = (query[1], query[2], query[3], query[5]["id"])
        if key in responses:
            matched_responses.append(responses[key])
    rcode_values = [response[5]["rcode"] for response in matched_responses]

    http_text = b"\n".join(payload for _, payload in http_payloads)
    request_packet = next(
        (number for number, payload in http_payloads if b"GET /tools/leafsync-helper.sh HTTP/1.1" in payload),
        None,
    )
    response_packet = next(
        (number for number, payload in http_payloads if b"Content-Disposition: attachment; filename=leafsync-helper.sh" in payload),
        None,
    )

    print(f"[+] Capture: {capture}")
    print(
        f"[+] HTTP helper request/response text found: "
        f"packets {request_packet}/{response_packet}"
    )
    print("[+] Download path: /tools/leafsync-helper.sh")
    print(f"[+] LeafSync helper content observed: {b'LeafSync helper started' in http_text}")
    print(f"[+] Suspicious parent domain: {PARENT_DOMAIN}")
    print(f"[+] Exfiltrating workstation: {ordered[0][1]}")
    print(f"[+] Ordered DNS fragments ({len(ordered)}):")
    for packet, source, _, _, _, _, sequence_number, fragment in ordered:
        print(f"    packet {packet}: {source}  {sequence_number:02d}.{fragment}")
    print(
        f"[+] Matching DNS responses: {len(matched_responses)}/{len(ordered)}, "
        f"rcode={DNS_RCODES.get(rcode_values[0], rcode_values[0])} "
        f"(all={all(value == 3 for value in rcode_values)})"
    )
    print(f"[+] Reconstructed Base64: {encoded}")
    print(f"[+] Decoded data: {decoded}")


if __name__ == "__main__":
    main()
