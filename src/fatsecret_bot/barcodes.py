from __future__ import annotations

import io
import re
from dataclasses import dataclass

import zxingcpp
from PIL import Image, UnidentifiedImageError

MAX_BARCODE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_BARCODE_IMAGE_PIXELS = 24_000_000
_DIGITS_RE = re.compile(r"^\d+$")
_SERVER_TYPES = {
    "EAN8": "EAN_8",
    "EAN13": "EAN_13",
    "UPCA": "UPC_A",
    "UPCE": "UPC_E",
}


class BarcodeDecodeError(ValueError):
    """A user-supplied image does not contain one unambiguous retail barcode."""


@dataclass(frozen=True)
class DecodedBarcode:
    """Normalized code and the enum expected by FatSecret's create-food form."""

    code: str
    barcode_type: str


def _valid_gtin_checksum(code: str) -> bool:
    if len(code) not in {8, 12, 13, 14} or not _DIGITS_RE.fullmatch(code):
        return False
    digits = [int(char) for char in code]
    weighted = sum(
        digit * (3 if (len(digits) - index) % 2 == 0 else 1)
        for index, digit in enumerate(digits[:-1])
    )
    return (10 - weighted % 10) % 10 == digits[-1]


def normalize_barcode(code: str, barcode_type: str | None = None) -> DecodedBarcode:
    """Validate a retail barcode and map its format to FatSecret's enum."""
    normalized = "".join(code.strip().split())
    if not _DIGITS_RE.fullmatch(normalized):
        raise BarcodeDecodeError("Штрих-код должен состоять только из цифр.")
    inferred = {
        8: "EAN_8",
        12: "UPC_A",
        13: "EAN_13",
        14: "Other",
    }.get(len(normalized))
    server_type = _SERVER_TYPES.get((barcode_type or "").replace("-", "").replace("_", "").upper())
    server_type = server_type or inferred
    if server_type is None:
        raise BarcodeDecodeError("Поддерживаются товарные штрих-коды EAN-8, UPC-A, EAN-13 и GTIN-14.")
    if server_type != "UPC_E" and not _valid_gtin_checksum(normalized):
        raise BarcodeDecodeError("Контрольная цифра штрих-кода не сходится. Пришли более четкое фото.")
    return DecodedBarcode(code=normalized, barcode_type=server_type)


def decode_barcode_image(image_bytes: bytes) -> DecodedBarcode:
    """Decode exactly one retail barcode from an in-memory Telegram photo."""
    if not image_bytes or len(image_bytes) > MAX_BARCODE_IMAGE_BYTES:
        raise BarcodeDecodeError("Фото пустое или слишком большое.")
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_BARCODE_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.width * image.height > MAX_BARCODE_IMAGE_PIXELS:
                raise BarcodeDecodeError("Фото штрих-кода слишком большое.")
            image.load()
            results = zxingcpp.read_barcodes(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise BarcodeDecodeError("Не удалось прочитать изображение.") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit

    decoded: dict[str, DecodedBarcode] = {}
    for result in results:
        try:
            item = normalize_barcode(str(result.text), str(result.format))
        except BarcodeDecodeError:
            continue
        decoded[item.code] = item
    if not decoded:
        raise BarcodeDecodeError("Не вижу товарный штрих-код. Сфотографируй его крупно, ровно и при хорошем свете.")
    if len(decoded) > 1:
        raise BarcodeDecodeError("На фото несколько штрих-кодов. Пришли фото только одного кода.")
    return next(iter(decoded.values()))
