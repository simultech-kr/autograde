import re

import pytest

from autograde.web_theme import THEME_CSS
from autograde.platform_portal import CoursePortal


def luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(v * weight for v, weight in zip(channels, (.2126, .7152, .0722)))


@pytest.mark.parametrize('mode', [0, 1], ids=['light', 'dark'])
def test_semantic_text_pairs_have_readable_contrast(mode):
    palettes = re.findall(r':root\{([^}]+)\}', THEME_CSS)
    colours = dict(re.findall(r'--([a-z-]+):(#[0-9a-f]{6})', palettes[mode]))
    for fg, bg in [('text', 'surface'), ('text', 'page'), ('muted', 'surface'),
                   ('link', 'surface'), ('on-accent', 'accent'), ('text', 'secondary'),
                   ('muted', 'secondary'), ('text', 'soft'), ('error', 'error-bg'),
                   ('warning', 'warning-bg')]:
        a, b = sorted((luminance(colours[fg]), luminance(colours[bg])))
        assert (b + .05) / (a + .05) >= 4.5, (mode, fg, bg)
    for fg in ['focus', 'border']:
        a, b = sorted((luminance(colours[fg]), luminance(colours['surface'])))
        assert (b + .05) / (a + .05) >= 3


def test_student_html_uses_shared_theme_after_legacy_colours():
    body = CoursePortal._page('Test', '<input>').body
    assert THEME_CSS in body
    assert body.index('prefers-color-scheme:dark') > body.index('background:white')
    assert 'input,select,textarea{background:var(--surface);color:var(--text)' in body
    assert 'forced-colors:active' in body
