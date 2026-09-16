"""Semantic colours shared by server-rendered student and instructor pages."""

THEME_CSS = """
:root{color-scheme:light;--page:#f8fafc;--surface:#ffffff;--text:#0f172a;
--muted:#475569;--border:#64748b;--link:#1d4ed8;--accent:#1d4ed8;--on-accent:#ffffff;
--soft:#eff6ff;--secondary:#e2e8f0;--error:#991b1b;--error-bg:#fef2f2;
--warning:#9a3412;--warning-bg:#fff7ed;--focus:#1d4ed8}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0f172a;--surface:#1e293b;--text:#f1f5f9;
--muted:#cbd5e1;--border:#94a3b8;--link:#93c5fd;--accent:#93c5fd;--on-accent:#0f172a;
--soft:#172554;--secondary:#334155;--error:#fca5a5;--error-bg:#450a0a;
--warning:#fdba74;--warning-bg:#431407;--focus:#fbbf24}}
body{background:var(--page);color:var(--text)}header,main,.card,section{background:var(--surface);color:var(--text)}
a{color:var(--link)}input,select,textarea{background:var(--surface);color:var(--text);border-color:var(--border)}
input:not([type=checkbox]){border-color:var(--border)}
input::placeholder,textarea::placeholder{color:var(--muted);opacity:1}
input[type=checkbox],input[type=radio]{accent-color:var(--accent)}
.assignment-choice input[type=radio]{accent-color:var(--accent)}
button,.button{background:var(--accent);color:var(--on-accent)}
.secondary,th{background:var(--secondary);color:var(--text)}
.hint,small,.audience{color:var(--muted)}.notice{background:var(--soft);color:var(--text);border-color:var(--accent)}
.warning{background:var(--warning-bg);color:var(--warning);border-color:var(--warning)}
.error{background:var(--error-bg);color:var(--error);border-color:var(--error)}
.danger{background:var(--error);color:var(--surface)}
header,.card,section,th,td,article,.steps li,.assignment-choice{border-color:var(--border)}
.assignment-choice:has(input:checked){background:var(--soft);border-color:var(--accent)}
.steps [aria-current]{border-color:var(--accent)}
:focus-visible,.assignment-choice:focus-within{outline:3px solid var(--focus);outline-offset:3px}
button:disabled,input:disabled,select:disabled,textarea:disabled{opacity:1;background:var(--secondary);color:var(--muted);cursor:not-allowed}
figure svg,.course-qr svg{background:#fff;padding:12px;color:#000}
.source-code{background:var(--page);color:var(--text);border:1px solid var(--border);padding:16px;overflow:auto;white-space:pre;tab-size:4}
.source-code code{font:14px/1.65 ui-monospace,monospace;unicode-bidi:plaintext}
@media(forced-colors:active){:focus-visible{outline:3px solid Highlight}button,.button,.assignment-choice{border:1px solid ButtonText}}
"""
