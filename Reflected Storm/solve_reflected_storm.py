#!/usr/bin/env python3
"""Identify and verify a DNS-amplification victim and its reflectors."""

from __future__ import annotations

import argparse
import collections
import socket
import struct
from pathlib import Path

DNS_TYPES = {1: "A", 16: "TXT", 255: "ANY"}
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
    transaction_id, flags, qdcount, ancount, _, _ = struct.unpack_from("!HHHHHH", payload)
    offset = 12
    question_name = ""
    question_type = 0
    for index in range(qdcount):
        name, offset = parse_dns_name(payload, offset)
        qtype, _ = struct.unpack_from("!HH", payload, offset)
        offset += 4
        if index == 0:
            question_name, question_type = name, qtype

    answer_types = []
    for _ in range(ancount):
        _, offset = parse_dns_name(payload, offset)
        rtype, _, _, rdlength = struct.unpack_from("!HHIH", payload, offset)
        offset += 10 + rdlength
        if offset > len(payload):
            raise ValueError("truncated DNS answer")
        answer_types.append(rtype)

    return {
        "id": transaction_id,
        "is_response": bool(flags & 0x8000),
        "rcode": flags & 0x0F,
        "question_name": question_name,
        "question_type": question_type,
        "answer_types": answer_types,
    }


def parse_ipv4_udp_packets(path: Path):
    data = path.read_bytes()
    if len(data) < 24:
        raise ValueError("truncated PCAP global header")
    endian = pcap_endian(data)
    position = 24
    packet_number = 0

    while position + 16 <= len(data):
        _, _, included_length, _ = struct.unpack_from(endian + "IIII", data, position)
        position += 16
        frame = data[position : position + included_length]
        position += included_length
        packet_number += 1

        if len(frame) < 20 or frame[0] >> 4 != 4:
            continue
        header_length = (frame[0] & 0x0F) * 4
        if header_length < 20 or len(frame) < header_length + 8 or frame[9] != 17:
            continue
        source = socket.inet_ntoa(frame[12:16])
        destination = socket.inet_ntoa(frame[16:20])
        source_port, destination_port, udp_length, _ = struct.unpack_from(
            "!HHHH", frame, header_length
        )
        yield (
            packet_number,
            source,
            source_port,
            destination,
            destination_port,
            udp_length,
            frame[header_length + 8 :],
        )


def type_name(value: int) -> str:
    return DNS_TYPES.get(value, f"TYPE{value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", nargs="?", default="reflected-storm.pcap")
    args = parser.parse_args()
    capture = Path(args.capture)

    packets = list(parse_ipv4_udp_packets(capture))
    dns_packets = []
    for packet in packets:
        try:
            dns = parse_dns(packet[6])
        except ValueError:
            continue
        dns_packets.append((*packet[:6], dns))

    amplified = [
        packet for packet in dns_packets
        if packet[2] == 53 and packet[5] >= 500 and packet[6]["is_response"]
    ]
    if not amplified:
        raise SystemExit("No large DNS response stream was found")
    victim, response_count = collections.Counter(packet[3] for packet in amplified).most_common(1)[0]
    victim_responses = [packet for packet in amplified if packet[3] == victim]
    reflectors = collections.Counter(packet[1] for packet in victim_responses)

    queries = {
        (packet[1], packet[2], packet[3], packet[6]["id"]): packet
        for packet in dns_packets
        if not packet[6]["is_response"] and packet[1] == victim and packet[4] == 53
    }
    matched_pairs = []
    for response in victim_responses:
        key = (response[3], response[4], response[1], response[6]["id"])
        if key in queries:
            matched_pairs.append((queries[key], response))
    if not matched_pairs:
        raise SystemExit("No matching DNS request/response pairs found")

    sample_query, sample_response = matched_pairs[0]
    all_any = all(query[6]["question_type"] == 255 for query, _ in matched_pairs)
    all_txt = all(16 in response[6]["answer_types"] for _, response in matched_pairs)
    all_noerror = all(response[6]["rcode"] == 0 for _, response in matched_pairs)

    print(f"[+] Capture: {capture}")
    print(f"[+] IPv4/UDP packets parsed: {len(packets)}")
    print(f"[+] Amplified DNS responses (UDP >= 500 bytes): {response_count}")
    print(f"[+] Victim: {victim}")
    print(f"[+] Reflectors ({len(reflectors)}):")
    for reflector, count in sorted(reflectors.items()):
        print(f"    {reflector}: {count} amplified responses")
    print(f"[+] Matched DNS request/response pairs: {len(matched_pairs)}")
    print(
        "[+] DNS semantics: "
        f"queries={type_name(sample_query[6]['question_type'])} (all={all_any}), "
        f"answers include TXT (all={all_txt}), "
        f"response rcode={DNS_RCODES.get(sample_response[6]['rcode'], sample_response[6]['rcode'])} "
        f"(all={all_noerror})"
    )
    print(
        f"[+] Example trigger: packet {sample_query[0]}  "
        f"{sample_query[1]}:{sample_query[2]} -> {sample_query[3]}:53  "
        f"{type_name(sample_query[6]['question_type'])}? {sample_query[6]['question_name']}"
    )
    print(
        f"[+] Example reflection: packet {sample_response[0]}  "
        f"{sample_response[1]}:53 -> {sample_response[3]}:{sample_response[4]}  "
        f"UDP length={sample_response[5]}, answer={type_name(sample_response[6]['answer_types'][0])}"
    )
    print(f"[+] Flag: FLOWTRIQ{{{victim}_{len(reflectors)}_reflectors}}")


if __name__ == "__main__":
    main()
