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
main{width:100%;max-width:640px;overflow-wrap:anywhere;background:var(--surface);color:var(--text);border-radius:0}
main.portal-wide{max-width:1080px}
.student-brand{font-size:.9rem;text-decoration:none;font-weight:650}
.student-nav{display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--border);padding:12px 0;margin-bottom:24px}
.student-nav a{display:inline-flex;align-items:center;min-height:44px;padding:8px 12px;text-decoration:none;color:var(--muted)}
.student-nav a[aria-current=page]{color:var(--link);background:var(--soft);border-bottom:2px solid var(--accent);font-weight:650}
.portal-intro{max-width:70ch;color:var(--muted)}
.portal-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px;margin:24px 0}
.portal-card{border:1px solid var(--border);padding:20px;background:var(--surface);min-width:0}
.portal-card h2{margin:0 0 8px;font-size:1.15rem}.portal-card p{color:var(--muted)}
.portal-card a,.portal-action{display:inline-flex;align-items:center;min-height:44px;font-weight:650}
.portal-kpis{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}.portal-kpis div{flex:1 1 180px;padding:16px;border:1px solid var(--border)}
.portal-kpis dt{color:var(--muted);font-size:.9rem}.portal-kpis dd{font-size:1.45rem;font-weight:700;margin:4px 0 0}
.portal-section{margin:28px 0}.portal-section h2{font-size:1.2rem}
.portal-result{border:1px solid var(--border);padding:16px;margin:12px 0}
.portal-result h3{margin:0;font-size:1.05rem}.portal-result dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,180px),1fr));gap:12px}
.portal-result dt{font-size:.85rem;color:var(--muted)}.portal-result dd{margin:4px 0 0;font-weight:600}
.portal-result code{font-size:.85rem}.portal-result .portal-score{font-size:1.25rem}
.portal-help{padding:16px;border-left:3px solid var(--accent);background:var(--soft)}
.portal-empty{padding:16px;border:1px solid var(--border)}
.portal-footer{padding-top:20px;border-top:1px solid var(--border);margin-top:24px;color:var(--muted);font-size:.85rem}
h1{font-size:1.65rem;line-height:1.3}p{margin:12px 0}
select{width:100%;max-width:100%;min-height:44px;padding:10px;font:inherit;border:1px solid var(--border);background:var(--surface);color:var(--text)}
.progress-hint{font-size:.9rem;color:var(--muted)}
.assignment-choice{margin:10px 0;padding:12px;scroll-margin-bottom:180px}
.assignment-choice input[type=radio]{scroll-margin-bottom:180px}
.assignment-choice strong{font-size:1rem}.assignment-choice small{font-size:.85rem}
.deadline-badge{width:fit-content;padding:2px 6px;border-radius:var(--radius-sm)}
.claim-action{position:sticky;bottom:0;z-index:1;background:var(--surface);border-top:1px solid var(--border);
padding:12px 0 max(12px,env(safe-area-inset-bottom));box-shadow:none}
.claim-action p{margin:0 0 8px;font-size:.9rem;max-height:3.2em;overflow:auto}
.claim-action button{margin:0}button{min-height:44px}
.assignment-links a{display:inline-block;min-height:44px;padding:8px 0}
summary{display:list-item;min-height:44px;padding:8px 0}
.error:not(:empty){padding:12px;border-left:4px solid var(--error)}
.code-box{background:var(--soft);border:0;border-inline-start:3px solid var(--accent);border-radius:var(--radius-sm);padding:16px;margin:16px 0}
#claim-code{font:700 1.45rem/1.5 ui-monospace,monospace;letter-spacing:.03em}
#claim-code code{font:inherit}#api-address{font-size:1rem}.code-box button{margin-top:12px}
.claim-expiry{font-weight:600}.ide-help li{margin:8px 0}.ide-help{padding-left:22px}
.claim-copy-status{min-height:1.6em;font-size:.9rem}details{margin:12px 0}
summary{cursor:pointer;font-weight:600}.secondary{border:1px solid var(--border)}
@media(max-width:640px){main{margin:0;min-height:100vh;padding:20px 16px;border-radius:0}}
@media(max-height:450px){.claim-action{position:static}}
"""


def navigation(course, current, *, materials=False, exams=False):
    base = '/courses/' + escape(course, quote=True)
    links = [('portal', '내 수업', base + '/portal'),
             ('assignments', '과제 받기', base + '/assignments'),
             ('results', '제출 현황', base + '/results')]
    if materials:
        links.append(('materials', '내 자료', base + '/materials'))
    if exams:
        links.append(('exam-papers', '시험지', base + '/exam-papers'))
    links.append(('courses', '교과목 선택', '/student'))
    return '<nav class="student-nav" aria-label="학생 메뉴">' + ''.join(
        f'<a href="{href}"' + (' aria-current="page"' if key == current else '') + f'>{label}</a>'
        for key, label, href in links) + '</nav>'


def role_landing(*, instructors=True):
    body = ('<p class="portal-intro">학생은 과제와 자료를 받고 제출 현황을 확인합니다. '
            '교수자는 수업을 구성하고 실습 진행과 평가를 관리합니다.</p><div class="portal-grid">'
            '<section class="portal-card"><h2>학생 포털</h2><p>학번·비밀번호로 가입 → 교과목 선택 → 수강 명단 일치 → 과제·자료 확인</p>'
            '<a href="/student">학생 포털 시작</a></section>')
    if instructors:
        body += ('<section class="portal-card"><h2>교수자 포털</h2><p>수업과 학생 관리, 과제 등록, 실시간 교실, 제출·평가 확인</p>'
                 '<a href="/instructor">교수자 포털 시작</a></section>')
    return body + '</div><p class="portal-help">실습 코드는 Autograde IDE에서 작성하고 제출합니다. 웹에서 발급한 수령 코드로 IDE를 연결하세요.</p>'


def course_chooser(courses):
    body = ('<div class="portal-grid"><section class="portal-card"><h2>학생 회원</h2>'
            '<p>학번·비밀번호로 가입하고 수업을 선택하세요. 교수자의 명단과 학번이 일치하면 이용할 수 있습니다.</p>'
            '<nav class="assignment-links"><a href="/student/register">회원가입</a><a href="/student/login">로그인</a>'
            '<a href="/student/account">내 수강 교과목</a></nav></section></div>'
            '<h2>기존 학생 · 수업 로그인</h2><p class="portal-intro">교수자에게 전용 비밀번호를 받은 학생은 수업을 선택하세요. '
            '수업 로그인 후 회원 계정을 만들면 공통 비밀번호로 여러 수업을 이용할 수 있습니다.</p>')
    if not courses:
        return body + '<p class="portal-empty" role="status">현재 운영 중인 교과목이 없습니다. 교수자에게 수업 등록 여부를 확인하세요.</p>'
    body += '<div class="portal-grid">'
    for course, label in courses:
        body += (f'<article class="portal-card"><h2>{escape(label)}</h2><p>과제 수령 · 제출 현황 · 배포 자료</p>'
                 f'<a href="/courses/{escape(course, quote=True)}/portal">이 수업으로 이동</a></article>')
    return body + '</div>'


def claim_body(course_label, assignment, code, api_url, expires_at, course, *, member_csrf=None):
    e = escape
    if member_csrf is not None:
        session_navigation = (
            '<p class="portal-help">공통 회원 로그인은 코드 발급 후에도 유지됩니다. '
            '로그인한 시점부터 최대 10분 동안 이용할 수 있으며, 공용 PC를 떠나기 전에 학생 웹에서도 로그아웃하세요.</p>'
            f'<nav class="assignment-links"><a href="/courses/{e(course, quote=True)}/portal">내 수업으로 돌아가기</a>'
            '<a href="/student/account">내 수강 교과목</a></nav>'
            '<form method="post" action="/student/logout">'
            f'<input type="hidden" name="csrf" value="{e(member_csrf, quote=True)}">'
            '<button class="secondary">학생 웹 로그아웃</button></form>')
    else:
        session_navigation = (
            '<p><small>공용 PC 보호를 위해 수업별 웹 로그인은 코드 발급 후 종료되었습니다. '
            '새 코드는 학번과 전용 비밀번호로 다시 인증해야 합니다.</small></p>'
            f'<p><a href="/courses/{e(course, quote=True)}">다시 인증하여 새 코드 받기</a></p>')
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
        + session_navigation
    )
