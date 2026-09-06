"""Local QR rendering for public assignment-claim URLs.

The service must never send claim URLs to a third-party QR image API.  Segno is
pure Python and produces inert inline SVG, so the instructor dashboard can
display a QR code without a remote request or a writable temporary file.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import segno


def assignment_claim_qr_svg(claim_url: str) -> str:
    """Return an inline QR SVG after enforcing a public, secret-free URL."""

    if not isinstance(claim_url, str):
        raise TypeError("claim_url must be a string")
    parsed = urlsplit(claim_url)
    path_parts = parsed.path.split("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 3
        or path_parts[:2] != ["", "assignment-claim"]
        or not path_parts[2]
    ):
        raise ValueError("claim_url must be a secret-free assignment claim URL")
    return segno.make_qr(claim_url, error="m").svg_inline(
        scale=4,
        border=4,
        dark="#111827",
        light="#ffffff",
        omitsize=False,
    )
