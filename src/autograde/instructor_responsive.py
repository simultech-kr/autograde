"""Shared responsive presentation for read-only instructor result views."""
from html import escape


def result_table(headers, rows, caption):
    """Cell contents are trusted HTML, already escaped by the calling view.

    Keep one semantic table in the DOM: cards are a CSS layout, not duplicated
    student data or a separate mobile-only representation.
    """
    head = ''.join(f'<th scope="col" role="columnheader">{escape(label)}</th>' for label in headers)
    body = []
    for row in rows:
        if len(row) != len(headers):
            raise ValueError('result table column count mismatch')
        cells = []
        for index, (label, content) in enumerate(zip(headers, row)):
            tag = 'th' if index == 0 else 'td'
            attributes = 'scope="row" role="rowheader"' if index == 0 else 'role="cell"'
            cells.append(f'<{tag} {attributes}><span class="cell-label" aria-hidden="true">'
                         f'{escape(label)}</span><div class="cell-value">{content}</div></{tag}>')
        body.append('<tr role="row">' + ''.join(cells) + '</tr>')
    return ('<div class="results-region"><table class="result-table" role="table">'
            f'<caption>{escape(caption)}</caption><thead role="rowgroup"><tr role="row">{head}</tr></thead>'
            '<tbody role="rowgroup">' + ''.join(body) + '</tbody></table></div>')


RESPONSIVE_CSS = """
.responsive-instructor,.responsive-instructor *{box-sizing:border-box}
.responsive-instructor{overflow-wrap:anywhere}
.responsive-instructor main{min-width:0;width:100%}
.responsive-instructor h1{font-size:clamp(1.4rem,4vw,2rem)}
.responsive-instructor h2{overflow-wrap:anywhere}
.responsive-instructor a{overflow-wrap:anywhere}
.responsive-instructor summary{cursor:pointer;min-height:44px;padding:8px 0}
.responsive-instructor figure{margin:16px 0}
.responsive-instructor figure svg{max-width:100%;height:auto}
.responsive-instructor .page-actions{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
.responsive-instructor .page-actions a,.result-table a{display:inline-block;padding:8px 0;min-height:44px}
.results-region{width:100%;min-width:0;margin:16px 0}
.result-table{display:table;width:100%;max-width:100%;table-layout:fixed;border-collapse:collapse;overflow:visible}
.result-table caption{text-align:left;font-weight:600;padding:8px 0;color:var(--text)}
.result-table th,.result-table td{padding:10px;vertical-align:top;text-align:left;white-space:normal;overflow-wrap:anywhere;border-bottom:1px solid var(--border)}
.result-table tbody th{background:var(--surface);font-weight:600}
.result-table .cell-value{min-width:0;max-width:100%}
.result-table .cell-label{display:none}
.result-table details{margin:8px 0}
.result-table ol{padding-left:20px}
.result-table code{overflow-wrap:anywhere}
.responsive-instructor .source-code{max-width:100%;overflow:auto;white-space:pre}
.responsive-instructor .source-code code{overflow-wrap:normal}
.responsive-instructor .source-files{padding-left:20px}
.responsive-instructor .source-files a{display:inline-block;padding:6px 0;min-height:44px}
@media(min-width:1101px){
 #student-results+.results-region th:nth-child(1){width:9%}
 #student-results+.results-region th:nth-child(2){width:17%}
 #student-results+.results-region th:nth-child(3){width:6%}
 #student-results+.results-region th:nth-child(4){width:9%}
 #student-results+.results-region th:nth-child(5){width:13%}
 #student-results+.results-region th:nth-child(6){width:12%}
 #student-results+.results-region th:nth-child(7),#student-results+.results-region th:nth-child(8){width:5%}
 #student-results+.results-region th:nth-child(9){width:18%}
 #student-results+.results-region th:nth-child(10){width:6%}
}
#student-results+.results-region td:nth-child(3) .cell-value{font-weight:700}
@media(max-width:1100px){
 .result-table,.result-table tbody{display:block}
 .result-table caption{display:block}
 .result-table thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
 .result-table tbody{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:16px}
 .result-table tbody tr{display:block;min-width:0;border:1px solid var(--border);border-radius:10px;background:var(--surface);padding:8px 12px}
 .result-table th,.result-table td{display:grid;grid-template-columns:minmax(0,7rem) minmax(0,1fr);gap:12px;padding:10px 0;font-weight:400}
 .result-table tbody th{font-weight:600}
 .result-table .cell-label{display:block;color:var(--muted);font-size:.9rem}
 .result-table tr>:last-child{border-bottom:0}
}
@media(max-width:640px){
 .responsive-instructor main{margin:0 auto;padding:16px;border-radius:0}
 .responsive-instructor header{padding:16px}
 .responsive-instructor .grid{grid-template-columns:minmax(0,1fr)}
 .result-table tbody{grid-template-columns:minmax(0,1fr)}
 .result-table th,.result-table td{grid-template-columns:minmax(0,5.5rem) minmax(0,1fr);gap:8px}
}
"""
