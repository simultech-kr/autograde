"""Internal view fragments rendered by the authenticated instructor shell.

This DTO is never accepted from HTTP callers. Views supply escaped markup and
fixed styles, while the controller owns navigation, identity and response headers.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class InstructorPageFragment:
    title: str
    body: str
    status: int = 200
    styles: str = ''
