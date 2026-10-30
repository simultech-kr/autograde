"""Shared, flat instructor application chrome and action hierarchy.

Navigation stays a link; mutations stay authenticated forms. Presentation never
changes authorization, storage or browser persistence.
"""

_ICONS = {
    'home': '<path d="m3 10 9-7 9 7v10H3z"/><path d="M9 20v-7h6v7"/>',
    'students': '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5"/>',
    'assignments': '<rect x="5" y="4" width="14" height="17" rx="1"/><path d="M9 4V2h6v2M9 10h6M9 14h6M9 18h3"/>',
    'results': '<path d="M4 3v18h17M8 16v-5M13 16V7M18 16V4"/>',
    'classroom': '<rect x="3" y="4" width="18" height="13" rx="1"/><path d="M8 21h8M12 17v4"/>',
    'settings': '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    'folder': '<path d="M3 6h7l2 3h9v11H3z"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
}


def icon(name):
    return ('<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
            'focusable="false">' + _ICONS.get(name, _ICONS['grid']) + '</svg>')


def navigation_icon(url):
    segment = url.rstrip('/').split('/')[-1]
    name = {'students': 'students', 'registrations': 'students', 'assignments': 'assignments',
            'submissions': 'results', 'rubrics': 'results', 'classroom': 'classroom',
            'environments': 'classroom', 'operations': 'classroom', 'settings': 'settings',
            'content': 'folder', 'materials': 'folder', 'exam-papers': 'folder',
            'extensions': 'folder', 'instructor': 'home', 'student': 'students'}.get(segment, 'grid')
    return icon(name)


STYLE = """
.responsive-instructor{font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.responsive-instructor .instructor-header{min-height:80px;padding:16px max(24px,calc((100vw - 1600px)/2 + 24px));gap:20px;border-bottom:1px solid var(--border)}
.instructor-brand{display:flex;align-items:center;gap:12px;color:var(--text);text-decoration:none}
.instructor-brand-mark{display:grid;place-items:center;flex:none;width:36px;height:36px;border-radius:4px;background:var(--accent);color:var(--on-accent);font-size:19px;font-weight:750}
.instructor-brand h1{font-size:18px;letter-spacing:-.025em;margin:0;line-height:1.3}
.instructor-brand small{font-size:12px;font-weight:500;color:var(--muted);display:block;margin-top:3px}
.instructor-header .course-switch{flex:0 1 430px}
.instructor-header .course-switch label{font-size:12px;color:var(--muted);font-weight:500;margin:0 0 4px}
.instructor-header .course-switch .actions{flex-wrap:nowrap;gap:8px}
.instructor-header .course-switch select{width:100%;flex:1;max-width:100%;font-size:13px;min-height:40px;padding:8px 10px}
.instructor-header .course-switch button{font-size:13px;min-height:40px;padding:8px 12px;background:var(--surface);border:1px solid var(--control-border);color:var(--text)}
.responsive-instructor .instructor-header+.shell{max-width:1600px;grid-template-columns:232px minmax(0,1fr);min-height:calc(100vh - 80px)}
.portal-menu{margin:0;background:var(--surface);border-right:1px solid var(--border);min-width:0}
.portal-menu>summary{display:none}
.portal-menu nav.portal-navigation{padding:24px 12px;display:block}
.portal-menu .portal-nav-group{margin-bottom:24px}
.portal-menu .portal-nav-label{font-size:11px;letter-spacing:.05em;font-weight:650;margin:0 12px 8px;color:var(--muted)}
.portal-menu nav a{display:flex;align-items:center;gap:10px;min-height:44px;border:1px solid transparent;border-radius:4px;margin:2px 0;padding:10px 12px;color:var(--muted);text-decoration:none;font-size:13px;font-weight:500;line-height:1.5}
.portal-menu nav a:hover{color:var(--text);background:var(--surface-raised)}
.portal-menu nav a[aria-current=page]{background:var(--soft);color:var(--link);border-color:transparent;font-weight:700}
.ui-icon{width:18px;height:18px;flex:none;vertical-align:middle}
.portal-account{border-top:1px solid var(--border);padding-top:12px;margin-top:24px}
.responsive-instructor .instructor-header+.shell>main{padding:28px 32px;min-width:0;background:var(--page)}
.page-heading{padding:0 0 20px;margin:0 0 20px;border-bottom:1px solid var(--border)}
.page-heading h2{font-size:26px;font-weight:700;letter-spacing:-.035em;line-height:1.3;margin:8px 0 0}
.page-breadcrumb{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:0;padding:0}
.page-breadcrumb a{color:var(--muted);text-decoration:none;min-height:24px;display:inline-flex;align-items:center}
.page-breadcrumb a:hover{text-decoration:underline;color:var(--link)}
.ui-muted{color:var(--muted);font-size:13px}.ui-toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin:20px 0}
.ui-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.responsive-instructor .ui-action,.responsive-instructor .actions>a,.responsive-instructor .catalog-actions>a{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:9px 14px;border:1px solid var(--border);border-radius:4px;background:var(--surface);color:var(--text);text-decoration:none;font-size:13px;line-height:1.5;font-weight:600;margin:0}
.responsive-instructor .ui-action.primary,.responsive-instructor .actions>a.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.responsive-instructor .ui-action.quiet{border-color:transparent;background:transparent;color:var(--link)}
.responsive-instructor .ui-action:hover,.responsive-instructor .actions>a:hover,.responsive-instructor .catalog-actions>a:hover{border-color:var(--accent);background:var(--soft);color:var(--link)}
.responsive-instructor .ui-action.primary:hover{background:var(--accent);color:var(--on-accent)}
.responsive-instructor .button, .responsive-instructor button{font-size:14px;border:1px solid transparent;min-height:44px;font-weight:600}
.responsive-instructor button.secondary{background:var(--surface);border-color:var(--border);color:var(--text)}
.responsive-instructor input:not([type=checkbox]):not([type=radio]),.responsive-instructor select,.responsive-instructor textarea{font-size:14px;padding:10px 12px}
.responsive-instructor label{font-size:13px;font-weight:600;color:var(--text)}
.responsive-instructor .shell main .ui-panel,.responsive-instructor .shell main>section{padding:24px;background:var(--surface);border:1px solid var(--border);border-radius:4px;margin:0 0 20px}
.responsive-instructor .shell main>section>h3{font-size:17px;letter-spacing:-.02em;margin-bottom:16px}
.responsive-instructor .shell main>details{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:0 20px;margin:20px 0}
.responsive-instructor .shell main>details>summary{font-size:14px;font-weight:600;padding:16px 0}
.responsive-instructor .shell main>details>section{padding:16px 0;border-top:1px solid var(--border)}
.ui-badge{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;border:1px solid transparent;border-radius:3px;background:var(--surface-raised);color:var(--muted);font-size:12px;font-weight:600;line-height:1.5;max-width:100%;overflow-wrap:anywhere}
.ui-badge[data-tone=info]{background:var(--soft);color:var(--link)}
.ui-badge[data-tone=success]{background:var(--success-bg);color:var(--success)}
.ui-badge[data-tone=warning]{background:var(--warning-bg);color:var(--warning)}
.ui-badge[data-tone=error]{background:var(--error-bg);color:var(--error)}
.ui-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,160px),1fr));gap:12px;margin:20px 0}
.ui-metrics>div{border:1px solid var(--border);border-radius:4px;padding:18px 20px;background:var(--surface)}
.ui-metrics dt{font-size:12px;color:var(--muted)}.ui-metrics dd{margin:6px 0 0;font-size:28px;font-weight:700;line-height:1.25;font-variant-numeric:tabular-nums}
.ui-empty{padding:32px;border:1px solid var(--border);background:var(--surface);border-radius:4px;text-align:center;color:var(--muted)}
.ui-empty strong{display:block;color:var(--text);font-size:17px;margin-bottom:8px}
.ui-tabs{display:flex;flex-wrap:wrap;gap:4px;padding:0;border-bottom:1px solid var(--border);margin:20px 0}
.ui-tabs a{display:inline-flex;align-items:center;min-height:44px;padding:10px 16px;border-bottom:2px solid transparent;color:var(--muted);text-decoration:none;font-size:13px;font-weight:600}
.ui-tabs a[aria-current=page]{color:var(--link);border-color:var(--accent)}
.ui-tabs a:hover{background:var(--soft);color:var(--link)}
.catalog-title{color:var(--text);font-weight:650;text-decoration:none}.catalog-title:hover{text-decoration:underline;color:var(--link)}
.operations-alerts{list-style:none;padding:0;margin:0}.operations-alerts li{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 0;border-bottom:1px solid var(--border)}
.operations-alerts li:last-child{border-bottom:0}.operations-alerts p{margin:4px 0 0}.operations-alerts .ui-action{flex:none}
.operations-health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:20px;margin:0}
.operations-health-grid dt{font-size:12px;color:var(--muted);margin-bottom:6px}.operations-health-grid dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums}
.responsive-instructor .results-region{border:1px solid var(--border);background:var(--surface);border-radius:4px;padding:0 16px}
.responsive-instructor .result-table{font-size:13px}
.responsive-instructor .result-table caption{padding:16px 0;font-size:12px}
.responsive-instructor .result-table th,.responsive-instructor .result-table td{padding:14px 12px}
.responsive-instructor .result-table thead th{font-size:12px;font-weight:600;background:var(--surface-raised)}
.responsive-instructor .result-table tbody tr:last-child>*{border-bottom:0}
.responsive-instructor .result-table tbody tr:hover{background:var(--surface-raised)}
.responsive-instructor .result-table tbody th{background:transparent}
.responsive-instructor .result-table .ui-action{font-size:12px;padding:8px 10px}
.responsive-instructor .result-table details{margin:8px 0 0;font-size:12px;color:var(--muted)}
.responsive-instructor .result-table summary{min-height:36px;padding:6px 0;color:var(--link);font-weight:500}
.responsive-instructor .notice{border:1px solid var(--border);border-left:3px solid var(--accent);padding:14px 16px;font-size:13px;border-radius:3px}
.responsive-instructor .notice.warning{border-left-color:var(--warning)}.responsive-instructor .notice.error{border-left-color:var(--error)}
.responsive-instructor .portal-metrics{gap:12px;border:0}.responsive-instructor .portal-metrics>div{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:20px}
.responsive-instructor .portal-tools,.responsive-instructor .catalog-tools{background:var(--surface);padding:16px 20px;border:1px solid var(--border);border-radius:4px;margin:20px 0}
.responsive-instructor .portal-tools label,.responsive-instructor .catalog-tools label{margin-top:0}
.responsive-instructor .portal-course-card,.responsive-instructor .portal-workflow-card{border-radius:4px;padding:24px}
.responsive-instructor .portal-section-heading>a{font-size:13px;font-weight:600;text-decoration:none;padding:8px 12px;border:1px solid var(--border);border-radius:4px;background:var(--surface)}
.responsive-instructor .draft-navigation{gap:0;padding:6px;border:1px solid var(--border);border-radius:4px;background:var(--surface)}
.responsive-instructor .draft-navigation li{flex:1;min-width:140px}.responsive-instructor .draft-navigation a{border:0;padding:10px 12px;font-size:13px;text-align:center}
.responsive-instructor .draft-navigation a:hover{background:var(--soft)}
.responsive-instructor .draft-summary{padding:16px;border:1px solid var(--border);border-radius:4px;background:var(--surface);font-size:13px}
.draft-overview{border:1px solid var(--border);background:var(--surface);border-radius:4px;padding:20px;margin-bottom:16px}
.draft-overview p{margin:4px 0}.draft-overview .ui-toolbar{margin:0 0 12px}.draft-overview .ui-actions{margin-top:12px}
.draft-overview details{font-size:13px;color:var(--muted);margin:8px 0 0}.draft-overview summary{font-weight:500;padding:4px 0;min-height:32px}
.responsive-instructor .account-notice{font-size:12px;color:var(--muted)}
@media(min-width:1101px){.portal-menu nav{position:sticky;top:0;max-height:100vh;overflow-y:auto}}
@media(max-width:1100px){.responsive-instructor .results-region{padding:0 12px 12px}.responsive-instructor .result-table th,.responsive-instructor .result-table td{padding:10px 0}}
@media(max-width:1100px){
 .responsive-instructor .instructor-header{padding:16px 20px}.responsive-instructor .instructor-header+.shell{display:block}
 .portal-menu{border-right:0;border-bottom:1px solid var(--border)}.portal-menu>summary{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;font-weight:600;font-size:13px;min-height:44px;list-style:none}.portal-menu>summary::-webkit-details-marker{display:none}
 .portal-menu>summary:after{content:'+';font-size:20px;font-weight:400}.portal-menu[open]>summary:after{content:'−'}
 .portal-menu nav.portal-navigation{padding:12px 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}.portal-menu .portal-nav-group{margin:0}.portal-menu .portal-account{margin:0}
 .responsive-instructor .instructor-header+.shell>main{padding:24px}.page-heading h2{font-size:24px}
}
@media(max-width:600px){
 .responsive-instructor .instructor-header{padding:16px;gap:16px}.instructor-brand h1{font-size:17px}.instructor-header .course-switch{flex-basis:100%}
 .responsive-instructor .instructor-header+.shell>main{padding:20px 16px}.page-heading{margin-bottom:16px;padding-bottom:16px}.page-heading h2{font-size:22px}
 .responsive-instructor .shell main .ui-panel,.responsive-instructor .shell main>section{padding:16px}
 .ui-metrics{gap:8px}.ui-metrics>div{padding:14px}.ui-metrics dd{font-size:24px}
 .ui-toolbar{align-items:stretch}.ui-toolbar .ui-actions{width:100%}.ui-toolbar .ui-action{flex:1}
 .responsive-instructor .portal-tools,.responsive-instructor .catalog-tools{padding:16px}
 .responsive-instructor .result-table th,.responsive-instructor .result-table td{grid-template-columns:minmax(0,5.5rem) minmax(0,1fr)}
 .draft-overview{padding:16px}.draft-navigation{flex-direction:column}
 .operations-alerts li{align-items:flex-start;flex-direction:column;gap:8px}
}
@media(prefers-reduced-motion:reduce){.responsive-instructor *{scroll-behavior:auto!important}}
@media(forced-colors:active){.ui-action,.ui-badge,.instructor-brand-mark,.portal-menu nav a[aria-current=page]{border:1px solid CanvasText}}
"""
