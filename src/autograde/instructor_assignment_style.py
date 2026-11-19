"""Compact assignment catalog spacing, scoped away from other workflows."""

STYLE = """
.assignment-catalog .catalog-heading{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;margin:0 0 8px;gap:8px;border-bottom:1px solid var(--border);padding-bottom:8px}
.assignment-catalog .catalog-heading .ui-tabs{margin:0;border:0;gap:0;flex-wrap:nowrap}
.assignment-catalog .catalog-heading .ui-tabs a{padding:8px;white-space:nowrap}
.assignment-catalog .catalog-heading>.ui-action{flex:none}
.responsive-instructor .assignment-catalog .catalog-tools{display:grid;grid-template-columns:minmax(140px,1fr) minmax(150px,.6fr) auto;margin:4px 0;padding:4px 0 8px;gap:8px 12px;align-items:end;background:transparent;border:0}
.responsive-instructor .assignment-catalog .catalog-trash-tools{grid-template-columns:minmax(140px,1fr) auto}
.assignment-catalog .catalog-tools>div{min-width:0}
.assignment-catalog .catalog-tools label{margin:0 0 4px}
.assignment-catalog .catalog-search-actions{display:flex;align-items:center;gap:4px}
.assignment-catalog .catalog-search-actions button{margin:0;white-space:nowrap}
.assignment-catalog .catalog-search-actions .ui-action{padding-inline:10px}
.assignment-catalog .catalog-active-filters{display:flex;flex-wrap:wrap;gap:4px 8px;margin:0 0 4px}
.assignment-catalog .catalog-filter-chip{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:6px 10px;border:1px solid var(--border);border-radius:6px;color:var(--text);background:var(--surface);font-size:13px;text-decoration:none;max-width:100%;overflow-wrap:anywhere}
.assignment-catalog .catalog-filter-chip:hover{border-color:var(--accent);color:var(--link);background:var(--soft)}
.assignment-catalog .catalog-result-summary{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:4px 12px;min-height:44px;margin:0;font-size:13px;color:var(--muted)}
.assignment-catalog .catalog-result-count{margin:0}
.assignment-catalog .assignment-selection-tools{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px}
.assignment-catalog .assignment-selection-tools label{display:inline-flex;align-items:center;gap:6px;min-height:44px;margin:0;font-weight:500}
.assignment-catalog [data-assignment-selection-status]{font-variant-numeric:tabular-nums}
.assignment-catalog .assignment-bulk-panel{margin:0 0 8px;padding:8px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface)}
.assignment-catalog .assignment-bulk-panel[data-enhanced=true]{position:sticky;top:8px;z-index:2;background:var(--soft)}
.assignment-catalog .assignment-bulk-panel[data-enhanced=true]>summary{display:none}
.assignment-catalog .assignment-bulk-toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.assignment-catalog .assignment-bulk-toolbar button{margin:0}
.responsive-instructor .assignment-catalog .assignment-delete-action{color:var(--error);border:1px solid var(--error);background:var(--surface)}
.assignment-catalog .assignment-delete-action:hover:not(:disabled){background:var(--error-bg)}
.assignment-catalog .assignment-bulk-panel p{margin:4px 0;font-size:13px}
.assignment-catalog .assignment-bulk-help{margin:8px 0;font-size:13px;color:var(--muted)}
.assignment-catalog .assignment-bulk-help summary{min-height:44px;padding:8px 0}
.assignment-catalog .results-region{margin:0 0 8px}
.responsive-instructor .assignment-catalog .result-table caption{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.assignment-catalog .assignment-selection{display:inline-flex;margin:0 8px 0 0;min-height:44px}
.assignment-catalog .catalog-actions{margin:12px 0}
@media(max-width:760px){
 .responsive-instructor .assignment-catalog .catalog-tools{grid-template-columns:minmax(0,1fr) auto}
 .assignment-catalog .catalog-search-field{grid-column:1/-1}
 .assignment-catalog .catalog-state-field{grid-column:1}
 .assignment-catalog .catalog-search-actions{grid-column:2;justify-content:flex-end}
 .assignment-catalog .catalog-heading>.ui-action{padding-inline:10px}
 .assignment-catalog .catalog-heading .ui-tabs a{padding-inline:6px}
 .assignment-catalog .catalog-trash-tools .catalog-search-field{grid-column:1}
 .assignment-catalog .catalog-tools button{width:auto}
}
"""
