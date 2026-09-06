from __future__ import annotations

import pytest

from autograde.platform_qr import assignment_claim_qr_svg


def test_assignment_claim_qr_is_inline_and_contains_no_remote_asset() -> None:
    svg = assignment_claim_qr_svg(
        "https://grade.example.edu/assignment-claim/ba_123"
    )

    assert svg.startswith("<svg")
    assert "<script" not in svg
    assert "<image" not in svg
    assert "http://" not in svg


@pytest.mark.parametrize(
    "value",
    (
        "https://user:secret@grade.example.edu/assignment-claim/ba_123",
        "https://grade.example.edu/assignment-claim/ba_123?token=secret",
        "https://grade.example.edu/assignment-claim/ba_123#secret",
        "https://grade.example.edu/assignment-claim",
        "https://grade.example.edu/assignment-claim-else/ba_123",
        "https://grade.example.edu/assignment-claim/ba_123/extra",
        "https://grade.example.edu/other",
        "javascript:alert(1)",
    ),
)
def test_assignment_claim_qr_rejects_non_public_or_secret_bearing_urls(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        assignment_claim_qr_svg(value)
