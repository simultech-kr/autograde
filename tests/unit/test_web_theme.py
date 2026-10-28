import re
from importlib.resources import files

import pytest

from autograde.web_theme import THEME_CSS
from autograde.platform_portal import CoursePortal
from autograde.instructor_responsive import RESPONSIVE_CSS
from autograde.student_portal_view import STYLE as STUDENT_STYLE


def luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(v * weight for v, weight in zip(channels, (.2126, .7152, .0722)))


@pytest.mark.parametrize('mode', [0, 1], ids=['light', 'dark'])
def test_semantic_text_pairs_have_readable_contrast(mode):
    palettes = re.findall(r':root\s*\{([^}]+)\}', THEME_CSS)
    colours = dict(re.findall(r'--([a-z-]+):\s*(#[0-9a-f]{6})', palettes[mode]))
    for fg, bg in [('text', 'surface'), ('text', 'page'), ('muted', 'surface'),
                   ('link', 'surface'), ('on-accent', 'accent'), ('text', 'secondary'),
                   ('muted', 'secondary'), ('text', 'soft'), ('error', 'error-bg'),
                   ('warning', 'warning-bg'), ('success', 'success-bg'),
                   ('link', 'soft'), ('text', 'surface-raised'), ('muted', 'surface-raised')]:
        a, b = sorted((luminance(colours[fg]), luminance(colours[bg])))
        assert (b + .05) / (a + .05) >= 4.5, (mode, fg, bg)
    # Decorative separators may be subtle. Interactive control boundaries and
    # keyboard focus must remain clearly visible against their actual surfaces.
    for fg in ['focus', 'control-border']:
        for bg in ['surface', 'page']:
            a, b = sorted((luminance(colours[fg]), luminance(colours[bg])))
            assert (b + .05) / (a + .05) >= 3, (mode, fg, bg)


def test_python_pages_read_the_canonical_token_resource():
    tokens = files('autograde').joinpath('web_tokens.css').read_text(encoding='utf-8')
    assert THEME_CSS.startswith(tokens)
    assert len(re.findall(r':root\s*\{', THEME_CSS)) == 2
    defined = set(re.findall(r'(--[\w-]+)\s*:', THEME_CSS))
    referenced = set(re.findall(r'var\((--[\w-]+)', THEME_CSS))
    assert referenced <= defined


def test_explicit_light_theme_outranks_os_dark_preference():
    # Both rules match <html data-theme="light"> on a dark OS. The explicit
    # theme must have greater specificity than the later media-query :root.
    tokens = files('autograde').joinpath('web_tokens.css').read_text(encoding='utf-8')
    light_selectors = tokens.split('{', 1)[0]
    assert ':root[data-theme="light"]' in light_selectors
    media = tokens.split('@media (prefers-color-scheme: dark)', 1)[1]
    assert re.match(r'\s*\{\s*:root\s*\{', media)


def test_student_html_uses_shared_theme_after_legacy_colours():
    body = CoursePortal._page('Test', '<input>').body
    assert THEME_CSS in body
    compact = re.sub(r'\s+', '', body)
    assert compact.index('prefers-color-scheme:dark') > compact.index('background:white')
    assert 'input,select,textarea{background:var(--surface);color:var(--text)' in compact
    assert 'forced-colors:active' in body


def test_shell_regions_do_not_recolour_every_nested_semantic_element():
    compact = re.sub(r'\s+', '', THEME_CSS)
    assert 'header,main,.card,section{' not in compact
    assert '.responsive-instructor>main{background:var(--surface)' in compact
    assert 'main[data-audience=student]{' in compact
    assert '.danger{background:var(--error);color:var(--error-bg);}' in compact
    assert 'outline:3pxsolidvar(--focus)' in compact
    assert '.skip-link:focus{transform:none;}' in compact
    assert '@media(max-width:640px)' in compact


def test_flat_theme_removes_nested_card_decoration_without_hiding_focus():
    compact = re.sub(r'\s+', '', THEME_CSS)
    section_rule = compact.split('.instructor-header+.shell.card,.instructor-header+.shellsection{', 1)[1].split('}', 1)[0]
    assert 'border:0;border-top:1pxsolidvar(--border)' in section_rule
    assert 'border-radius:0' in section_rule
    assert 'box-shadow:none' in section_rule
    assert 'padding:var(--space-6)0' in section_rule
    student_rule = compact.split('main[data-audience=student]{', 1)[1].split('}', 1)[0]
    assert 'border:0;border-radius:0;box-shadow:none' in student_rule
    assert 'outline:3pxsolidvar(--focus)' in compact
    assert 'min-height:44px' in compact


def test_flat_overrides_cover_later_legacy_result_styles():
    compact = re.sub(r'\s+', '', THEME_CSS)
    # These selectors need higher specificity than instructor_results.STYLE,
    # which is appended after THEME_CSS on the standalone results page.
    assert '.responsive-instructor.results-toolbarselect,' in compact
    assert '.responsive-instructor.result-status{border-radius:var(--radius-sm);}' in compact
    metrics_rule = compact.split('.responsive-instructor.result-metrics>div{', 1)[1].split('}', 1)[0]
    assert 'border:0;border-inline-start:1pxsolidvar(--border);border-radius:0' in metrics_rule
    assert '.responsive-instructor.result-metrics>div:nth-child(odd){border-inline-start:0;}' in compact
    assert '.responsive-instructor.result-metrics>div:nth-child(n+3){border-top:1pxsolidvar(--border);}' in compact


def test_student_claim_and_mobile_results_use_flat_details():
    student = re.sub(r'\s+', '', STUDENT_STYLE)
    responsive = re.sub(r'\s+', '', RESPONSIVE_CSS)
    assert 'box-shadow:none' in student
    assert 'box-shadow:0' not in student
    assert 'border-inline-start:3pxsolidvar(--accent)' in student
    assert 'border-radius:var(--radius-sm)' in student
    assert 'border-radius:10px' not in student + responsive
    assert 'padding:8px12px;box-shadow:none' in responsive
    # The table's mobile label/value relationship and short-height sticky
    # fallback are unchanged by the visual flattening.
    assert '.result-table.cell-label{display:block' in responsive
    assert '@media(max-height:450px){.claim-action{position:static}}' in student
