"""Scoped student-management presentation; shared controls live in the shell."""

STYLE = """
.roster-intro{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin:0 0 24px}
.roster-intro p{margin:0;max-width:60ch}.roster-primary-actions{margin:0;gap:8px}
.roster-metrics{margin:0 0 24px}.roster-metrics dd span{font-size:.875rem;color:var(--muted);font-weight:400;margin-left:6px}
.roster-metrics small{display:block;font-size:.8125rem;line-height:1.6;font-weight:400;color:var(--muted);margin:8px 0 0}.roster-metrics [data-tone=warning] dd{color:var(--warning)}
.roster-list-panel{padding:0!important;overflow:hidden}.roster-list-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:20px 24px 0}
.roster-list-heading h3{margin:0;font-size:1.0625rem}.roster-list-heading p{margin:0;font-size:.8125rem;color:var(--muted)}
.roster-search{padding:0 24px;margin:16px 0 20px}.roster-search label{font-weight:500;font-size:.8125rem}.roster-search>div:first-child{flex:2 1 240px}.roster-search>div:nth-child(2){flex:1 1 160px}.roster-search button{margin:0}.roster-search .ui-action{flex:none}
.responsive-instructor .roster-search{border:0;border-radius:0;background:transparent;padding:0 24px;margin:16px 0 20px}
.responsive-instructor .student-roster-product .results-region{border:0;border-radius:0;padding:0}
.student-roster-product .results-region{margin:0}.student-roster-product caption{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.student-roster-product .result-table th,.student-roster-product .result-table td{padding:16px 24px}
.student-roster-product .result-table thead th{padding-block:12px;font-size:.75rem;font-weight:600;background:var(--surface-raised)}
.student-roster-product .result-table tbody tr:last-child>*{border-bottom:0}
.student-roster-product .result-table tbody tr:hover>*{background:var(--surface-raised)}
.student-roster-product strong{font-size:.9375rem;font-variant-numeric:tabular-nums}
.roster-student-name,.roster-enrollment,.roster-member{display:block}.roster-student-name{color:var(--muted);font-size:.8125rem;font-weight:400;margin-top:3px}.roster-enrollment{font-size:.875rem}.roster-member{font-size:.8125rem;margin-top:3px}
.roster-state-details{margin:6px 0 0!important}.roster-state-details summary{padding:0!important;min-height:36px!important;display:list-item!important;align-content:center;color:var(--muted);font-size:.8125rem;font-weight:400}
.roster-state-details p{font-size:.8125rem;margin:8px 0;line-height:1.65}.roster-state-details dl{font-size:.8125rem;margin:12px 0}.roster-state-details dl>div{display:grid;grid-template-columns:5rem minmax(0,1fr);gap:8px;margin:4px 0}.roster-state-details dt{color:var(--muted)}.roster-state-details dd{margin:0}
.student-roster-product .ui-action.roster-row-action{margin:0;min-height:44px;padding:8px 12px;font-size:.8125rem;max-width:100%;white-space:normal;text-align:left;gap:8px}
.roster-row-note{display:block;font-size:.8125rem;line-height:1.6;padding:8px 0;max-width:24ch}
.roster-pagination{display:flex;justify-content:flex-end;gap:8px;padding:16px 24px;border-top:1px solid var(--border)}
.roster-policy{font-size:.8125rem;line-height:1.6;margin:12px 0 28px}.roster-legacy{border:1px solid var(--border);background:var(--surface);border-radius:var(--radius-md);padding:0;margin:12px 0}
.responsive-instructor .shell main>details.roster-legacy{padding:0;margin:12px 0}
.roster-legacy>summary{padding:16px 20px!important;font-weight:600;font-size:.875rem}.roster-legacy>summary small{font-weight:400;display:inline-block;margin-left:8px}
.roster-legacy .roster-legacy-content{padding:0 20px 20px}.roster-legacy-content>p{font-size:.875rem;color:var(--muted);max-width:80ch}.roster-legacy-content form{max-width:740px}.roster-reset{margin:24px 0 0;font-size:.8125rem}.readonly-view .roster-write-only{display:none}
.registration-top{margin-bottom:24px}.registration-top>p{color:var(--muted);max-width:80ch}.registration-top .ui-toolbar{margin:16px 0 0}.registration-steps{display:flex;gap:24px;flex-wrap:wrap;margin:20px 0 0;padding:0;list-style:none;color:var(--muted);font-size:.875rem}.registration-steps strong{color:var(--text);margin-right:6px}
.registration-layout{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;align-items:start}.registration-layout .ui-panel{margin:0}.registration-panel-heading{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:16px}.registration-panel-heading h3{margin:0;font-size:1.0625rem}
.responsive-instructor .shell main .registration-layout .ui-panel{margin:0}
.registration-layout form p{font-size:.8125rem;color:var(--muted);line-height:1.65}.registration-layout label{font-size:.875rem;font-weight:500}.registration-layout details summary{font-size:.875rem}.registration-layout form button{width:100%;margin-top:20px}.registration-format{font-size:.8125rem;color:var(--muted);margin:12px 0}
.registration-request-list{margin-top:24px}.registration-request-list .result-table td,.registration-request-list .result-table th{padding:12px 16px}.registration-request-list .result-table caption{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.registration-review{max-width:760px}.registration-review dl{display:grid;grid-template-columns:minmax(120px,1fr) 2fr;gap:12px 16px}.registration-review dt{color:var(--muted)}.registration-review dd{margin:0;font-weight:600}.registration-complete{max-width:760px}.registration-complete p{line-height:1.7}
@media(min-width:1101px){.student-roster-product thead th:nth-child(1){width:19%}.student-roster-product thead th:nth-child(2){width:21%}.student-roster-product thead th:nth-child(3){width:34%}.student-roster-product thead th:nth-child(4){width:26%}}
@media(max-width:1100px){.student-roster-product .result-table tbody{padding:16px;gap:12px}.student-roster-product .result-table td,.student-roster-product .result-table th{padding:10px 0}.student-roster-product .result-table tbody tr:last-child>:not(:last-child){border-bottom:1px solid var(--border)}.registration-layout{grid-template-columns:1fr}}
@media(max-width:640px){.roster-intro{align-items:stretch}.roster-primary-actions{width:100%}.roster-primary-actions .ui-action{flex:1}.roster-list-heading{padding:16px 16px 0}.responsive-instructor .roster-search{padding:0 16px;gap:12px}.roster-search>div{flex:1 1 100%!important}.roster-search button{width:auto!important;flex:1}.roster-search .ui-action{flex:1}.roster-legacy>summary small{display:block;margin:4px 0 0}.registration-steps{gap:8px 16px}.roster-state-details dl>div{grid-template-columns:4rem minmax(0,1fr)}}
"""
