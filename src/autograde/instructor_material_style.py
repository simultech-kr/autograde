"""Information hierarchy for material monitoring and deliberate editing."""

STYLE = """
.responsive-instructor .shell .material-workspace .material-overview{padding:16px 20px;margin:0 0 16px;background:var(--surface-raised)}
.material-overview-heading{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px}
.material-overview-heading>.ui-action{margin-left:auto}
.material-state{font-size:16px;color:var(--text)}
.material-workspace .material-overview p{margin:8px 0 0;color:var(--muted);font-size:13px}
.material-workspace .material-share{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin:16px 0}
.material-workspace .material-share p{margin:0;flex:1 1 240px}
.responsive-instructor .shell .material-workspace .material-attention{padding:12px 20px 16px;margin:0 0 16px}
.material-attention-heading{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:4px 12px;margin-bottom:4px}
.material-attention-heading h3{font-size:16px;margin:0}
.material-attention-links{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.material-attention .hint{margin:8px 0 0}
.responsive-instructor .shell .material-workspace .material-students{padding:20px;margin:0 0 20px}
.material-students>h3{margin-bottom:12px}
.material-filter-fields{display:grid;grid-template-columns:minmax(180px,1.5fr) repeat(3,minmax(140px,1fr));gap:12px}
.material-filter-fields label{margin:0 0 6px;font-size:13px}
.material-filter-fields input,.material-filter-fields select{margin:0;min-width:0;width:100%}
.material-filter-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:12px 0 8px}
.material-filter-actions button{margin:0}
.material-filter-form+.hint{margin:8px 0 12px;font-size:12px}
.material-workspace details.material-work-area{margin:12px 0;border:1px solid var(--border);border-radius:6px;background:var(--surface)}
.material-workspace details.material-work-area>summary{min-height:48px;padding:12px 16px;font-weight:600;cursor:pointer}
.material-workspace details.material-work-area[open]>summary{border-bottom:1px solid var(--border)}
.material-work-content{padding:16px}
.material-work-content>:first-child{margin-top:0}
.material-work-content>:last-child{margin-bottom:0}
.responsive-instructor .shell .material-workspace .material-work-content>section{padding:0;border:0;box-shadow:none;margin:0 0 20px}
.responsive-instructor .shell .material-workspace .material-work-content>section:last-child{margin-bottom:0}
.responsive-instructor .shell .material-workspace .material-management{padding:20px;margin:0 0 20px}
.material-management>.hint{margin:8px 0 16px;max-width:88ch}
.material-upload-option{border-top:1px solid var(--border);padding-top:16px;margin-top:16px}
.material-upload-option h4{margin:0 0 8px;font-size:15px}
.material-workspace .material-remove{font-weight:400;font-size:12px;margin:8px 0 0}
.material-remove summary{font-size:13px;color:var(--link)}
.material-remove p{margin:8px 0;line-height:1.6}
.material-workspace .material-flow{padding:12px 0;margin:0 0 16px;color:var(--muted);font-size:14px}
.responsive-instructor .shell .material-workspace .material-step{margin:0 0 20px;padding:20px}
.material-workspace #material-history{margin-top:20px}
.material-workspace pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(min-width:1101px){
 .material-dashboard-summary{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;margin-bottom:16px}
 .responsive-instructor .shell .material-workspace .material-dashboard-summary>section{margin:0}
}
@media(max-width:1100px){
 .material-filter-fields{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:640px){
 .responsive-instructor .shell .material-workspace .material-overview,.responsive-instructor .shell .material-workspace .material-attention,.responsive-instructor .shell .material-workspace .material-students,.responsive-instructor .shell .material-workspace .material-management,.responsive-instructor .shell .material-workspace .material-step{padding:16px}
 .material-overview-heading>.ui-action{margin-left:0}
 .material-filter-fields{grid-template-columns:minmax(0,1fr)}
 .material-filter-actions>*{flex:1}
 .material-workspace details.material-work-area>summary{padding:12px}
 .material-work-content{padding:12px}
}
"""
