"""One packaged token source for the server and the browser build."""
from importlib.resources import files
from pathlib import Path
import re

import pytest

from autograde.web_theme import THEME_CSS


ROOT = Path(__file__).resolve().parents[2]
TOKENS = ROOT / 'src/autograde/web_tokens.css'
GLOBAL = ROOT / 'web/src/styles/globals.css'


def test_both_renderers_consume_the_same_packaged_tokens():
    packaged = files('autograde').joinpath('web_tokens.css').read_text(encoding='utf-8')
    assert packaged == TOKENS.read_text(encoding='utf-8')
    assert THEME_CSS.startswith(packaged)
    stylesheet = GLOBAL.read_text(encoding='utf-8')
    imported = re.findall(r'@import\s+"([^"]+)"\s*;', stylesheet)
    assert len(imported) == 1
    assert (GLOBAL.parent / imported[0]).resolve() == TOKENS
    # CSS is part of the wheel, not a runtime dependency on the repository tree.
    packaging = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    assert '[tool.setuptools.package-data]' in packaging
    assert re.search(r'autograde\s*=\s*\["web_tokens\.css"\]', packaging)


def test_component_styles_do_not_redefine_shared_design_tokens():
    names = set(re.findall(r'(--[a-z-]+)\s*:', TOKENS.read_text(encoding='utf-8')))
    assert {'--border', '--control-border', '--success', '--success-bg'} <= names
    for stylesheet in (ROOT / 'web/src').rglob('*.css'):
        declarations = set(re.findall(r'(--[a-z-]+)\s*:', stylesheet.read_text(encoding='utf-8')))
        assert not names.intersection(declarations), str(stylesheet)


def test_explicit_student_theme_uses_the_shared_system_palette():
    css = TOKENS.read_text(encoding='utf-8')
    assert '[data-theme="light"],\n:root {' in css
    system_dark = re.findall(r':root\s*\{([^}]+)\}', css)[1]
    explicit_dark = re.search(r'\[data-theme="dark"\]\s*\{([^}]+)\}', css).group(1)
    declarations = lambda block: dict(re.findall(r'(--[a-z-]+)\s*:\s*([^;]+);', block))
    assert declarations(explicit_dark) == declarations(system_dark)


@pytest.mark.parametrize('token', ['--page', '--surface', '--surface-raised', '--text', '--muted',
                                  '--border', '--control-border', '--accent', '--on-accent',
                                  '--error', '--error-bg', '--warning', '--warning-bg',
                                  '--success', '--success-bg', '--focus', '--link'])
def test_each_colour_role_is_defined_for_both_themes(token):
    palettes = re.findall(r':root\s*\{([^}]+)\}', TOKENS.read_text(encoding='utf-8'))
    assert len(palettes) == 2
    for palette in palettes:
        assert re.search(re.escape(token) + r'\s*:\s*#[0-9a-f]{6}\s*;', palette)


def test_browser_base_has_accessible_controls_and_user_preferences():
    css = GLOBAL.read_text(encoding='utf-8')
    assert 'var(--control-border)' in css
    assert 'min-height: 44px' in css
    assert ':focus-visible' in css
    assert 'prefers-reduced-motion: reduce' in css
    assert 'forced-colors: active' in css
    assert 'overflow-x: hidden' not in css  # Do not hide layout bugs or content.


def test_flat_design_has_no_elevation_in_either_theme():
    css = TOKENS.read_text(encoding='utf-8')
    palettes = re.findall(r':root\s*\{([^}]+)\}', css)
    for palette in palettes:
        tokens = dict(re.findall(r'(--[a-z-]+)\s*:\s*([^;]+);', palette))
        assert tokens['--shadow-sm'].strip() == 'none'
        # Flat surfaces can use a distinct canvas without gradients or shadows.
        assert tokens['--page'].strip() != tokens['--surface'].strip()
    radii = re.findall(r'--radius-\w+\s*:\s*(\d+)px\s*;', css)
    assert len(radii) == 3
    assert all(0 <= int(radius) <= 4 for radius in radii)
    for stylesheet in (ROOT / 'web/src').rglob('*.css'):
        source = stylesheet.read_text(encoding='utf-8')
        assert 'gradient(' not in source, str(stylesheet)
        shadows = re.findall(r'box-shadow\s*:\s*([^;]+);', source)
        assert all(shadow.strip() in {'none', 'var(--shadow-sm)'} for shadow in shadows)


def _contrast(first, second):
    def luminance(colour):
        channels = [int(colour[offset:offset + 2], 16) / 255 for offset in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                  for value in channels]
        return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


@pytest.mark.parametrize(('foreground', 'background'), [
    ('--text', '--surface'), ('--muted', '--surface'),
    ('--text', '--page'), ('--muted', '--page'),
    ('--muted', '--surface-raised'), ('--link', '--surface'), ('--link', '--soft'),
    ('--on-accent', '--accent'), ('--success', '--success-bg'),
    ('--warning', '--warning-bg'), ('--error', '--error-bg'),
])
def test_dashboard_text_colours_remain_readable_in_both_themes(foreground, background):
    for palette in re.findall(r':root\s*\{([^}]+)\}', TOKENS.read_text(encoding='utf-8')):
        colours = dict(re.findall(r'(--[a-z-]+)\s*:\s*(#[0-9a-f]{6})\s*;', palette))
        assert _contrast(colours[foreground], colours[background]) >= 4.5
