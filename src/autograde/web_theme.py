"""Shared design tokens and semantic styles for server-rendered pages.

Layout-specific styles remain with their views. Keep this layer limited to
shared controls and explicit page regions so a nested header or main element
does not unexpectedly acquire its own card background.
"""
from importlib.resources import files


THEME_CSS = files("autograde").joinpath("web_tokens.css").read_text(encoding="utf-8") + """
body {
  background: var(--page);
  color: var(--text);
  font-family: var(--font-sans);
  line-height: 1.6;
}
a { color: var(--link); text-underline-offset: .18em; }
input, select, textarea {
  background: var(--surface);
  color: var(--text);
  border-color: var(--control-border);
  border-radius: var(--radius-sm);
  max-width: 100%;
}
input:not([type=checkbox]):not([type=radio]) {
  border-color: var(--control-border);
  border-radius: var(--radius-sm);
}
input::placeholder, textarea::placeholder { color: var(--muted); opacity: 1; }
input[type=checkbox], input[type=radio] { accent-color: var(--accent); }
.assignment-choice input[type=radio] { accent-color: var(--accent); }
button, .button {
  background: var(--accent);
  color: var(--on-accent);
  border-radius: var(--radius-sm);
  min-height: 44px;
  line-height: 1.4;
  font-weight: 600;
  text-align: center;
  overflow-wrap: anywhere;
}
.secondary, th { background: var(--secondary); color: var(--text); }
.hint, small, .audience { color: var(--muted); }
.notice { background: var(--soft); color: var(--text); border-color: var(--accent); }
.warning { background: var(--warning-bg); color: var(--warning); border-color: var(--warning); }
.error { background: var(--error-bg); color: var(--error); border-color: var(--error); }
.danger { background: var(--error); color: var(--error-bg); }
.card, section, th, td, article, .steps li, .assignment-choice { border-color: var(--border); }
.assignment-choice:has(input:checked) { background: var(--soft); border-color: var(--accent); }
.steps [aria-current] { border-color: var(--accent); }
:focus-visible, .assignment-choice:focus-within {
  outline: 3px solid var(--focus);
  outline-offset: 3px;
}
button:disabled, input:disabled, select:disabled, textarea:disabled {
  opacity: 1;
  background: var(--secondary);
  color: var(--muted);
  cursor: not-allowed;
}
.source-code {
  background: var(--page);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-4);
  overflow: auto;
  white-space: pre;
  tab-size: 4;
}
.source-code code { font: 14px/1.65 var(--font-mono); unicode-bidi: plaintext; }
.skip-link {
  position: fixed;
  inset: var(--space-2) auto auto var(--space-2);
  z-index: 100;
  padding: var(--space-3) var(--space-4);
  background: var(--surface);
  border: 1px solid var(--control-border);
  border-radius: var(--radius-sm);
  transform: translateY(calc(-100% - 20px));
}
.skip-link:focus { transform: none; }

/* Explicit shell regions, not every semantic header/main on a page. */
.responsive-instructor > main {
  background: var(--surface);
  color: var(--text);
  border-radius: 0;
  box-shadow: none;
}
.instructor-header {
  background: var(--surface);
  color: var(--text);
  border-color: var(--border);
  padding: var(--space-5) max(var(--space-6), calc((100vw - var(--content-width)) / 2));
}
.instructor-header h1 { letter-spacing: -.025em; line-height: 1.25; }
.instructor-header .instructor-context { color: var(--muted); font-size: .875rem; margin-top: var(--space-2); }
.instructor-header + .shell { max-width: var(--content-width); }
.instructor-header + .shell > nav {
  align-self: start;
  padding: var(--space-6) var(--space-3);
}
.instructor-header + .shell > nav a {
  min-height: 44px;
  margin-bottom: var(--space-1);
  padding: var(--space-3) var(--space-4);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--muted);
  text-decoration: none;
  font-size: .9375rem;
}
.instructor-header + .shell > nav a:hover { background: var(--secondary); color: var(--text); }
.instructor-header + .shell > nav a[aria-current=page] {
  background: var(--soft);
  border-inline-start-color: var(--accent);
  color: var(--link);
  font-weight: 700;
}
.instructor-header + .shell > main { padding: var(--space-6); background: var(--page); }
.instructor-header + .shell > main > h2 { font-size: clamp(1.375rem, 2.5vw, 1.75rem); letter-spacing: -.025em; }
.instructor-header + .shell .card, .instructor-header + .shell section {
  background: var(--surface);
  color: var(--text);
  border: 0;
  border-top: 1px solid var(--border);
  border-radius: 0;
  padding: var(--space-6) 0;
  box-shadow: none;
}
.instructor-header + .shell .grid > *, .instructor-header + .shell .actions > * { min-width: 0; }
.instructor-header + .shell .draft-navigation a { min-height: 44px; border-radius: var(--radius-sm); }
.instructor-header + .shell .status-badge { border-radius: var(--radius-sm); }
.instructor-header + .shell .assignment-bulk-tools,
.instructor-header + .shell .draft-summary {
  background: var(--surface-raised);
  border: 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  border-radius: 0;
}
.instructor-header + .shell .account-notice {
  border-top: 1px solid var(--border);
  padding-top: var(--space-4);
  margin-top: var(--space-8);
  font-size: .8125rem;
}

/* The standalone result page appends its own layout CSS after the theme.
   Scope overrides to the instructor shell so those legacy radii stay flat. */
.responsive-instructor .results-toolbar select,
.responsive-instructor .results-toolbar button,
.responsive-instructor .results-toolbar a,
.responsive-instructor .participation-links a,
.responsive-instructor .result-status { border-radius: var(--radius-sm); }
.responsive-instructor .results-toolbar select,
.responsive-instructor .results-toolbar button { border-color: var(--control-border); }
.responsive-instructor .result-metrics {
  gap: 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.responsive-instructor .result-metrics > div {
  border: 0;
  border-inline-start: 1px solid var(--border);
  border-radius: 0;
  padding: var(--space-4);
}
.responsive-instructor .result-metrics > div:first-child { border-inline-start: 0; }
.responsive-instructor .assignment-participation {
  border: 0;
  border-top: 1px solid var(--border);
  border-radius: 0;
  padding: var(--space-4) 0;
}

main[data-audience=student] {
  width: calc(100% - var(--space-8));
  max-width: 640px;
  margin: var(--space-8) auto;
  padding: var(--space-8);
  border: 0;
  border-radius: 0;
  box-shadow: none;
  background: var(--surface);
  color: var(--text);
}
main[data-audience=student] > .student-brand {
  display: inline-block;
  min-height: 44px;
  padding: var(--space-2) 0;
  font-size: .875rem;
  font-weight: 700;
  text-decoration: none;
}
main[data-audience=student] h1 { margin-top: var(--space-3); letter-spacing: -.025em; }
main[data-audience=student] .assignment-choice {
  border-color: var(--control-border);
  border-radius: var(--radius-sm);
}
main[data-audience=student] .assignment-choice:has(input:checked) { border-color: var(--accent); }
@media(max-width:760px) {
  .instructor-header + .shell > nav { display: flex; flex-wrap: wrap; gap: var(--space-1); padding: var(--space-3); }
  .instructor-header + .shell > nav a { padding: var(--space-2) var(--space-3); margin: 0; }
  .instructor-header + .shell > main { padding: var(--space-4); }
  .instructor-header + .shell .card, .instructor-header + .shell section { padding: var(--space-4) 0; }
}
@media(max-width:640px) {
  .responsive-instructor .result-metrics > div:nth-child(odd) { border-inline-start: 0; }
  .responsive-instructor .result-metrics > div:nth-child(n+3) { border-top: 1px solid var(--border); }
  main[data-audience=student] {
    width: 100%;
    margin: 0;
    min-height: 100vh;
    padding: var(--space-5) var(--space-4);
    border: 0;
    border-radius: 0;
    box-shadow: none;
  }
}
@media(forced-colors:active) {
  :focus-visible { outline: 3px solid Highlight; }
  button, .button, .assignment-choice { border: 1px solid ButtonText; }
  .instructor-header + .shell > nav a[aria-current=page] { border: 2px solid Highlight; }
}
"""
