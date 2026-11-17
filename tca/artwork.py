"""Album pictures and the washed color cards drawn from them."""

from __future__ import annotations

import logging
from io import BytesIO
from urllib.parse import urlsplit, urljoin

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

logger = logging.getLogger("tca")

_ART_HOSTS = {"i.scdn.co", "mosaic.scdn.co"}
_ART_LIMIT = 2_000_000
_CARD_RADIUS = 18
_ART_RADIUS = 14


def fetch_album_art(url: str, *, _hop: int = 0) -> bytes | None:
    """Download one Spotify album picture. Other hosts are ignored."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ART_HOSTS:
        return None
    try:
        response = requests.get(url, timeout=10, allow_redirects=False)
    except requests.RequestException:
        logger.info("Spotify did not return an album picture.")
        return None
    if response.status_code in {301, 302, 303, 307, 308} and _hop < 2:
        location = response.headers.get("Location", "")
        if location:
            return fetch_album_art(urljoin(url, location), _hop=_hop + 1)
    if response.status_code != 200:
        logger.info("Spotify album picture was not available (%s).", response.status_code)
        return None
    body = response.content
    if not body or len(body) > _ART_LIMIT:
        return None
    return body


def card_images(data: bytes | None, *, width: int, height: int, art_size: int) -> tuple[bytes, bytes | None]:
    """A rounded color wash for the card, and a rounded album picture when one exists."""
    art = _open(data)
    wash = _backdrop(art, width, height)
    thumb = None if art is None else _thumb(art, art_size)
    return _png(wash), None if thumb is None else _png(thumb)


def _open(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        image = Image.open(BytesIO(data))
        image.load()
        return image.convert("RGB")
    except Exception:
        logger.info("Could not read an album picture.")
        return None


def _backdrop(art: Image.Image | None, width: int, height: int) -> Image.Image:
    if art is None:
        base = Image.new("RGB", (width, height), (18, 11, 26))
        draw = ImageDraw.Draw(base)
        draw.ellipse((-width * 0.15, -height, width * 0.75, height * 1.4), fill=(74, 42, 110))
        draw.ellipse((width * 0.35, -height * 0.8, width * 1.2, height), fill=(42, 24, 68))
        draw.ellipse((width * 0.15, height * 0.15, width * 1.1, height * 2.2), fill=(26, 16, 40))
        base = base.filter(ImageFilter.GaussianBlur(10))
    else:
        soft = art.resize((48, 28), Image.Resampling.BOX).filter(ImageFilter.GaussianBlur(6))
        base = soft.resize((width, height), Image.Resampling.BICUBIC)
        base = ImageEnhance.Color(base).enhance(1.35)
    scrim = Image.new("L", (width, 1))
    for x in range(width):
        amount = 0.38 + 0.36 * (x / max(width - 1, 1))
        scrim.putpixel((x, 0), int(amount * 255))
    scrim = scrim.resize((width, height))
    dark = Image.new("RGB", (width, height), (8, 6, 12))
    base = Image.composite(dark, base, scrim)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=_CARD_RADIUS, fill=255)
    rounded = base.convert("RGBA")
    rounded.putalpha(mask)
    return rounded


def _thumb(art: Image.Image, size: int) -> Image.Image:
    thumb = art.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=_ART_RADIUS, fill=255)
    thumb.putalpha(mask)
    return thumb


def _png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
