#!/usr/bin/env python3
"""Extract ordered DNS-label payloads from a classic Ethernet PCAP."""

import base64
import collections
import socket
import struct
import sys
from pathlib import Path


def read_dns_name(data, offset):
    labels = []
    seen = set()
    end = offset
    jumped = False

    while offset < len(data):
        if offset in seen:
            raise ValueError("DNS compression-pointer loop")
        seen.add(offset)
        length = data[offset]

        if length == 0:
            if not jumped:
                end = offset + 1
            return ".".join(labels), end

        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("Truncated DNS compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue

        offset += 1
        if offset + length > len(data):
            raise ValueError("Truncated DNS label")
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
        if not jumped:
            end = offset

    raise ValueError("Unterminated DNS name")


def dns_queries(pcap_path):
    with pcap_path.open("rb") as capture:
        global_header = capture.read(24)
        if len(global_header) != 24:
            raise ValueError("Invalid PCAP global header")

        magic = global_header[:4]
        if magic in (b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1"):
            endian = "<"
        elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2<M"):
            endian = ">"
        else:
            raise ValueError("Unsupported PCAP byte order")

        packet_number = 0
        while True:
            packet_header = capture.read(16)
            if not packet_header:
                break
            if len(packet_header) != 16:
                raise ValueError("Truncated PCAP packet header")

            _, _, captured_length, _ = struct.unpack(
                endian + "IIII", packet_header
            )
            packet = capture.read(captured_length)
            packet_number += 1

            if len(packet) < 14 or struct.unpack("!H", packet[12:14])[0] != 0x0800:
                continue

            ip_packet = packet[14:]
            if len(ip_packet) < 20 or ip_packet[9] != 17:
                continue

            ip_header_length = (ip_packet[0] & 0x0F) * 4
            if len(ip_packet) < ip_header_length + 8:
                continue

            source = socket.inet_ntoa(ip_packet[12:16])
            destination = socket.inet_ntoa(ip_packet[16:20])
            source_port, destination_port, udp_length, _ = struct.unpack(
                "!HHHH", ip_packet[ip_header_length : ip_header_length + 8]
            )
            if destination_port != 53:
                continue

            dns = ip_packet[ip_header_length + 8 : ip_header_length + udp_length]
            if len(dns) < 12:
                continue

            _, flags, question_count, _, _, _ = struct.unpack("!HHHHHH", dns[:12])
            if flags & 0x8000 or question_count == 0:
                continue

            offset = 12
            for _ in range(question_count):
                query_name, offset = read_dns_name(dns, offset)
                if offset + 4 > len(dns):
                    break
                query_type, _ = struct.unpack("!HH", dns[offset : offset + 4])
                offset += 4
                yield {
                    "packet": packet_number,
                    "source": source,
                    "destination": destination,
                    "query": query_name,
                    "type": query_type,
                }


def find_payloads(queries):
    groups = collections.defaultdict(list)
    for query in queries:
        labels = query["query"].split(".")
        if len(labels) >= 3:
            groups[".".join(labels[-2:])].append((labels[0], query))

    findings = []
    for domain, entries in groups.items():
        if len(entries) < 2:
            continue
        encoded = "".join(label for label, _ in entries)
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if "SecLeaf{" in decoded:
            findings.append((domain, entries, encoded, decoded))
    return findings


def main():
    pcap_path = Path(sys.argv[1] if len(sys.argv) > 1 else "suspicious_activity.pcap")
    queries = list(dns_queries(pcap_path))
    findings = find_payloads(queries)

    print(f"[+] Capture: {pcap_path.name}")
    print(f"[+] DNS queries inspected: {len(queries)}")

    if not findings:
        print("[-] No decodable SecLeaf payload found")
        return 1

    for domain, entries, encoded, decoded in findings:
        print(f"[+] Suspicious domain: {domain}")
        print("[+] Ordered DNS fragments:")
        for label, query in entries:
            print(
                f"    packet {query['packet']:>3}: "
                f"{query['source']} -> {query['destination']}  "
                f"{query['query']}  fragment={label}"
            )
        print(f"[+] Reconstructed Base64: {encoded}")
        print(f"[+] Decoded payload: {decoded}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
