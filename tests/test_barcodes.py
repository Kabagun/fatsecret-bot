from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from fatsecret_bot.barcodes import (
    MAX_BARCODE_IMAGE_PIXELS,
    BarcodeDecodeError,
    decode_barcode_image,
    normalize_barcode,
)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(output, format="PNG")
    return output.getvalue()


def _ean13_png(code: str) -> bytes:
    left = {
        "0": "0001101", "1": "0011001", "2": "0010011", "3": "0111101", "4": "0100011",
        "5": "0110001", "6": "0101111", "7": "0111011", "8": "0110111", "9": "0001011",
    }
    middle = {
        "0": "0100111", "1": "0110011", "2": "0011011", "3": "0100001", "4": "0011101",
        "5": "0111001", "6": "0000101", "7": "0010001", "8": "0001001", "9": "0010111",
    }
    right = {digit: "".join("1" if bit == "0" else "0" for bit in pattern) for digit, pattern in left.items()}
    parity = {
        "0": "LLLLLL", "1": "LLGLGG", "2": "LLGGLG", "3": "LLGGGL", "4": "LGLLGG",
        "5": "LGGLLG", "6": "LGGGLL", "7": "LGLGLG", "8": "LGLGGL", "9": "LGGLGL",
    }
    bits = "101"
    bits += "".join(
        (left if kind == "L" else middle)[digit]
        for kind, digit in zip(parity[code[0]], code[1:7], strict=True)
    )
    bits += "01010" + "".join(right[digit] for digit in code[7:]) + "101"
    module = 4
    quiet = 12
    image = Image.new("RGB", ((len(bits) + quiet * 2) * module, 140), "white")
    pixels = image.load()
    for index, bit in enumerate(bits):
        if bit == "0":
            continue
        for x in range((quiet + index) * module, (quiet + index + 1) * module):
            for y in range(10, 125):
                pixels[x, y] = (0, 0, 0)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_normalize_barcode_validates_checksum_and_maps_type() -> None:
    assert normalize_barcode("4006381333931").barcode_type == "EAN_13"
    assert normalize_barcode("036000291452").barcode_type == "UPC_A"

    with pytest.raises(BarcodeDecodeError, match="Контрольная цифра"):
        normalize_barcode("4006381333932")


def test_decode_barcode_image_returns_one_normalized_code() -> None:
    decoded = decode_barcode_image(_ean13_png("4006381333931"))

    assert decoded.code == "4006381333931"
    assert decoded.barcode_type == "EAN_13"


def test_decode_barcode_image_rejects_multiple_codes(monkeypatch) -> None:
    monkeypatch.setattr(
        "fatsecret_bot.barcodes.zxingcpp.read_barcodes",
        lambda image: [
            SimpleNamespace(text="4006381333931", format="EAN13"),
            SimpleNamespace(text="036000291452", format="UPCA"),
        ],
    )

    with pytest.raises(BarcodeDecodeError, match="несколько"):
        decode_barcode_image(_png_bytes())


def test_decode_barcode_image_rejects_too_many_pixels(monkeypatch) -> None:
    class OversizedImage:
        width = MAX_BARCODE_IMAGE_PIXELS + 1
        height = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("fatsecret_bot.barcodes.Image.open", lambda _stream: OversizedImage())

    with pytest.raises(BarcodeDecodeError, match="слишком большое"):
        decode_barcode_image(b"image")
