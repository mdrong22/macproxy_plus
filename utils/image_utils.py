"""
image_utils.py — fixed version for macproxy_plus
Fixes:
  - PNG / RGBA images crashing GIF conversion (transparency not handled)
  - CDN image URLs without file extensions not being detected
  - Multiple images on a page not all being converted
  - WebP / AVIF served by CDNs silently failing

Drop this file in utils/ to replace the original.
No other files need to change.
Compatible with Python 3.9+
"""

import hashlib
import io
import os
import re
from typing import Optional
from urllib.parse import urlparse

import requests
from PIL import Image

CACHE_DIR = "image_cache"

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".webp", ".tiff", ".tif", ".ico",
    ".avif", ".heic", ".heif", ".jfif",
}

_CDN_PATTERNS = [
    # query-string format hints
    re.compile(r"[?&](?:format|fmt|type)=(?:jpe?g|png|webp|gif|avif|bmp)", re.I),
    # size hints common on image CDNs
    re.compile(r"[?&](?:w|h|width|height|size|resize|crop|thumb)=\d+", re.I),
    # path segments
    re.compile(
        r"/(?:images?|img|photos?|media|uploads?|thumbs?|thumbnails?|"
        r"static/images?|assets/images?)/",
        re.I
    ),
    # known CDN hostnames
    re.compile(r"pbs\.twimg\.com/media/", re.I),
    re.compile(r"cdn\.bsky\.app/img/", re.I),
    re.compile(r"\.imgix\.net/", re.I),
    re.compile(r"res\.cloudinary\.com/", re.I),
    re.compile(r"images\.unsplash\.com/", re.I),
    re.compile(r"i\.imgur\.com/", re.I),
    re.compile(r"i\.redd\.it/", re.I),
]


def is_image_url(url: str) -> bool:
    """Return True if the URL almost certainly points to an image."""
    path = urlparse(url).path.lower()

    # Extension check (most common case)
    if os.path.splitext(path)[1] in _IMAGE_EXTENSIONS:
        return True

    # CDN / query-string pattern check
    url_lower = url.lower()
    for pattern in _CDN_PATTERNS:
        if pattern.search(url_lower):
            return True

    return False


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _flatten_alpha(img: Image.Image) -> Image.Image:
    """
    Composite any alpha/transparency onto a white background.
    This MUST happen before converting to GIF, which has no true alpha.
    Without this, RGBA PNGs produce garbage or raise an exception.
    """
    # Palette images can carry transparency info — convert to RGBA first
    if img.mode == "P":
        img = img.convert("RGBA")

    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        # img.split()[3] is the alpha channel used as paste mask
        bg.paste(img, mask=img.split()[3])
        return bg

    if img.mode == "LA":
        bg = Image.new("L", img.size, 255)
        bg.paste(img, mask=img.split()[1])
        return bg.convert("RGB")

    # Ensure we end up in a quantisable mode
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")

    return img


def fetch_and_cache_image(
    url: str,
    content: Optional[bytes] = None,
    resize: bool = True,
    max_width: int = 512,
    max_height: int = 342,
    convert: bool = True,
    convert_to: str = "gif",
    dithering: str = "FLOYDSTEINBERG",
) -> Optional[str]:
    """
    Fetch an image (or use supplied bytes), convert it, cache it on disk,
    and return the path to the cached file.
    Returns None on any failure.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    # ── Fetch if no content supplied ─────────────────────────────────────────
    if content is None:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/91.0.4472.114 Safari/537.36"
                ),
                # Listing preferred types first discourages many CDNs from
                # silently serving WebP/AVIF when Pillow may lack a decoder
                "Accept": (
                    "image/gif, image/jpeg, image/png, "
                    "image/*;q=0.8, */*;q=0.5"
                ),
            }
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f"[image_utils] HTTP {resp.status_code} fetching {url}")
                return None
            content = resp.content
        except Exception as e:
            print(f"[image_utils] fetch error {url}: {e}")
            return None

    if not content:
        return None

    # ── Cache key ────────────────────────────────────────────────────────────
    cache_key = hashlib.md5(url.encode("utf-8")).hexdigest()
    out_ext = convert_to.lower().lstrip(".")
    cached_path = os.path.join(CACHE_DIR, f"{cache_key}.{out_ext}")

    if os.path.exists(cached_path):
        return cached_path

    # ── Open with Pillow ─────────────────────────────────────────────────────
    try:
        img = Image.open(io.BytesIO(content))

        # Animated GIF or multi-frame WebP — take only frame 0
        if hasattr(img, "is_animated") and img.is_animated:
            img.seek(0)

        img.load()  # force full decode so errors surface now
    except Exception as e:
        print(f"[image_utils] Pillow cannot open {url}: {e}")
        return None

    # ── Process ──────────────────────────────────────────────────────────────
    try:
        # 1. Flatten alpha/transparency onto white FIRST — critical for PNG
        img = _flatten_alpha(img)

        # 2. Resize to fit Mac Plus screen dimensions
        if resize:
            img.thumbnail((max_width, max_height), Image.LANCZOS)

        # 3. Convert to output format
        if convert and out_ext == "gif":
            dither_flag = (
                Image.Dither.FLOYDSTEINBERG
                if dithering.upper() == "FLOYDSTEINBERG"
                else Image.Dither.NONE
            )
            img = img.quantize(colors=256, dither=dither_flag)
        elif img.mode not in ("RGB", "L", "P"):
            img = img.convert("RGB")

        # 4. Save to cache
        buf = io.BytesIO()
        img.save(buf, format=out_ext.upper() if out_ext != "jpg" else "JPEG")
        with open(cached_path, "wb") as f:
            f.write(buf.getvalue())

        print(f"[image_utils] converted and cached: {url}")
        return cached_path

    except Exception as e:
        print(f"[image_utils] conversion error {url}: {e}")
        return None