#!/usr/bin/env python3
"""Verify the Shadow Chronometry image and geocode its identified landmark."""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

LANDMARK = "Obelisco de Buenos Aires, Argentina"


def main() -> None:
    image_path = Path(sys.argv[1] if len(sys.argv) > 1 else "shadow_chronometry.jpg")
    image = image_path.read_bytes()
    exif_present = b"Exif\x00\x00" in image

    params = urllib.parse.urlencode(
        {"q": LANDMARK, "format": "json", "limit": 1, "addressdetails": 1}
    )
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "SecLeaf-CTF-writeup/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))[0]

    latitude = float(result["lat"])
    longitude = float(result["lon"])
    flag = f"SecLeaf{{{latitude:.4f},{longitude:.4f}}}"

    print(f"[+] Image: {image_path}")
    print(f"[+] SHA256: {hashlib.sha256(image).hexdigest()}")
    print(f"[+] JPEG EXIF segment present: {exif_present}")
    print(f"[+] GPS metadata present: {False if not exif_present else 'not parsed'}")
    print(f"[+] Landmark identified visually: {LANDMARK}")
    print("[+] Geocoder: OpenStreetMap/Nominatim")
    print(f"[+] Result: {result['display_name']}")
    print(f"[+] Coordinates: latitude={latitude:.7f}, longitude={longitude:.7f}")
    print(f"[+] Rounded to four decimals: {latitude:.4f}, {longitude:.4f}")
    print(f"[+] Flag: {flag}")


if __name__ == "__main__":
    main()
