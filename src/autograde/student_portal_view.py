"""Student-facing display helpers, independent of authentication and storage."""
from datetime import datetime, timedelta, timezone
from html import escape

KST = timezone(timedelta(hours=9))


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def deadline(value):
    if not value:
        return '미지정'
    local = instant(value).astimezone(KST)
    weekday = '월화수목금토일'[local.weekday()]
    return f'{local.year}년 {local.month}월 {local.day}일({weekday}) {local:%H:%M} · 한국 시간'


def urgency(value, now):
    if not value:
        return ''
    remaining = instant(value) - instant(now)
    if timedelta(0) < remaining <= timedelta(days=1):
        return '<small class="warning deadline-badge">마감 임박 · 24시간 이내</small>'
    if timedelta(days=1) < remaining <= timedelta(days=7):
        return '<small class="notice deadline-badge">7일 이내 마감</small>'
    return ''


STYLE = """
*{box-sizing:border-box}[hidden]{display:none!important}
main{width:100%;max-width:640px;overflow-wrap:anywhere}
h1{font-size:1.65rem;line-height:1.3}p{margin:12px 0}
.progress-hint{font-size:.9rem;color:var(--muted)}
.assignment-choice{margin:10px 0;padding:12px;scroll-margin-bottom:180px}
.assignment-choice input[type=radio]{scroll-margin-bottom:180px}
.assignment-choice strong{font-size:1rem}.assignment-choice small{font-size:.85rem}
.deadline-badge{width:fit-content;padding:2px 6px;border-radius:4px}
.claim-action{position:sticky;bottom:0;z-index:1;background:var(--surface);border-top:1px solid var(--border);
padding:12px 0 max(12px,env(safe-area-inset-bottom));box-shadow:0 -6px 10px var(--surface)}
.claim-action p{margin:0 0 8px;font-size:.9rem;max-height:3.2em;overflow:auto}
.claim-action button{margin:0}button{min-height:44px}
.assignment-links a{display:inline-block;min-height:44px;padding:8px 0}
summary{display:list-item;min-height:44px;padding:8px 0}
.error:not(:empty){padding:12px;border-left:4px solid var(--error)}
.code-box{background:var(--soft);border:1px solid var(--border);border-radius:10px;padding:16px;margin:16px 0}
#claim-code{font:700 1.45rem/1.5 ui-monospace,monospace;letter-spacing:.03em}
#claim-code code{font:inherit}#api-address{font-size:1rem}.code-box button{margin-top:12px}
.claim-expiry{font-weight:600}.ide-help li{margin:8px 0}.ide-help{padding-left:22px}
.claim-copy-status{min-height:1.6em;font-size:.9rem}details{margin:12px 0}
summary{cursor:pointer;font-weight:600}.secondary{border:1px solid var(--border)}
@media(max-width:640px){main{margin:0;min-height:100vh;padding:20px 16px;border-radius:0}}
@media(max-height:450px){.claim-action{position:static}}
"""


def claim_body(course_label, assignment, code, api_url, expires_at, course):
    e = escape
    return (
        f'<p>{e(course_label)} · <strong>{e(assignment.title)}</strong></p>'
        '<p class="progress-hint">다음 단계: 실습 PC의 Autograde 확장에 코드를 입력하세요.</p>'
        f'<div class="code-box"><div id="claim-code"><code>{e(code)}</code></div>'
        '<button type="button" data-copy-target="claim-code" data-copy-label="수령 코드" hidden>수령 코드 복사</button></div>'
        f'<p class="claim-expiry" data-claim-expires="{e(expires_at, quote=True)}">만료: {e(deadline(expires_at))}</p>'
        f'<p><small>만료 시각: {e(deadline(expires_at))}. 남은 시간은 기기 시계 기준이며, 서버에서 만료·사용 여부를 최종 확인합니다. 이미 사용한 코드는 다시 사용할 수 없습니다.</small></p>'
        '<p class="claim-copy-status" data-copy-status role="status" aria-live="polite"></p>'
        '<p>휴대전화에서 보고 있다면 실습 PC에 코드를 직접 입력하세요. 코드를 다른 사람과 공유하지 마세요.</p>'
        '<h2>확장 연결 안내</h2>'
        f'<p>확장 API 서버 주소 (웹 접속 주소와 다릅니다)<br><code id="api-address">{e(api_url)}</code></p>'
        '<button type="button" class="secondary" data-copy-target="api-address" data-copy-label="서버 주소" hidden>서버 주소 복사</button>'
        '<details open><summary>VS Code</summary><ol class="ide-help">'
        '<li>Autograde 확장을 설치하고 실습 파일을 저장할 수업 폴더를 여세요.</li>'
        '<li>Autograde 화면의 서버 설정에 위 API 서버 주소를 입력하세요.</li>'
        '<li>수령 코드를 입력하고 로그인하세요. 내려받은 과제 폴더가 열리면 README를 확인하세요.</li>'
        '</ol></details><details><summary>Visual Studio 2022 / 2026</summary><ol class="ide-help">'
        '<li>Autograde 확장을 설치하고 Autograde 과제 창을 여세요.</li>'
        '<li>설정에서 위 API 서버 주소를 적용한 뒤, 수령 코드를 입력하고 코드로 로그인하세요.</li>'
        '<li>과제 다운로드 · 열기를 누르고 저장할 위치를 선택하세요. IDE 열기가 실패하면 내려받은 폴더를 직접 여세요.</li>'
        '</ol></details><noscript><p>코드를 선택해서 직접 복사하세요. 만료 시각 이후에는 새 코드가 필요합니다.</p></noscript>'
        '<p><small>공용 PC 보호를 위해 웹 로그인은 코드 발급 후 종료됩니다. 새 코드는 학번과 전용 비밀번호로 다시 인증해야 합니다.</small></p>'
        f'<p><a href="/courses/{e(course, quote=True)}">다시 인증하여 새 코드 받기</a></p>'
    )
