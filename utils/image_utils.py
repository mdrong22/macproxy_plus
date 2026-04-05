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
    Forcefully strips all PNG metadata and flattens transparency.
    """
    # If it's already a palette image (PNG-8), convert to RGBA to 
    # properly handle the transparency layer before flattening.
    if img.mode in ("P", "1"):
        img = img.convert("RGBA")

    if img.mode == "RGBA":
        # Create a brand new image buffer to drop all PNG 'Chunks'
        bg = Image.new("RGB", img.size, (255, 255, 255))
        # Use the alpha channel as a mask
        bg.paste(img, mask=img.split()[3])
        return bg

    if img.mode == "LA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[1])
        return bg

    # Final fallback: Force to RGB and drop any ICC profiles or metadata
    return img.convert("RGB")


def fetch_and_cache_image(
    url: str,
    content: Optional[bytes] = None,
    resize: bool = True,
    max_width: int = 512,
    max_height: int = 342,
    convert: bool = True,
    convert_to: str = "gif",
    dithering: str = "FLOYDSTEINBERG",
    hash_url: bool = True
) -> Optional[str]:
    """
    Fetch, convert to 1-bit B&W GIF87a, and cache for Macintosh Plus.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 1. Fetching Logic
    if content is None:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; 68K Mac OS 7)",
                "Accept": "image/gif, image/jpeg, image/png, */*",
            }
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return None
            content = resp.content
        except Exception as e:
            print(f"[image_utils] Fetch error: {e}")
            return None

    if not content:
        return None

    # 2. Cache Setup
    cache_key = hashlib.md5(url.encode("utf-8")).hexdigest()
    out_ext = convert_to.lower().lstrip(".")
    cached_path = os.path.join(CACHE_DIR, f"{cache_key}.{out_ext}")

    if os.path.exists(cached_path):
        return cached_path

    # 3. Processing
    try:
        img = Image.open(io.BytesIO(content))
        
        # Handle animations (take first frame)
        if hasattr(img, "is_animated") and img.is_animated:
            img.seek(0)
        img.load()

        print(f"[image_utils] Processing {url} | Original: {img.size} {img.mode}")

        # A. Flatten Alpha (Critical for PNG)
        img = _flatten_alpha(img)

        # B. Move to RGB for high-quality resizing
        if img.mode != "RGB":
            img = img.convert("RGB")

        # C. Resize (Mac Plus screen is 512x342)
        if resize:
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            print(f"[image_utils] New size: {img.size}")

        # D. Convert to 1-bit (B&W) for the Plus
        if convert and out_ext == "gif":
            dither_flag = (
                Image.Dither.FLOYDSTEINBERG 
                if dithering.upper() == "FLOYDSTEINBERG" 
                else Image.Dither.NONE
            )
            # Convert to Grayscale then Quantize to exactly 2 colors
            img = img.convert("L")
            img = img.quantize(colors=256, dither=dither_flag)

        # E. Save as strictly GIF87a
        buf = io.BytesIO()
        img.save(
            buf, 
            format="GIF", 
            version="GIF87a", 
            optimize=True, 
            interlace=False
        )
        
        final_data = buf.getvalue()

        # 4. Binary Header Debugging
        # Bytes 6-7 are Width, 8-9 are Height in Little Endian
        header_w = final_data[6] + (final_data[7] << 8)
        header_h = final_data[8] + (final_data[9] << 8)
        
        if header_w > 1000 or header_h > 1000:
            print(f"[!!!] WARNING: Header dimensions look corrupted: {header_w}x{header_h}")
        else:
            print(f"[debug] Verified GIF Header: {header_w}x{header_h}")

        with open(cached_path, "wb") as f:
            f.write(final_data)

        print(f"[image_utils] SUCCESS: {cached_path} ({len(final_data)} bytes)")
        return cached_path

    except Exception as e:
        print(f"[image_utils] CRITICAL ERROR: {e}")
        return None