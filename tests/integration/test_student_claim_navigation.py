"""Claim completion tells each login mode the truth and keeps logout usable."""
from html.parser import HTMLParser

from test_course_portal import portal, request, cookie, csrf, login, WEB
from test_student_registration import signup, activate_member


class LinksAndForms(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = set()
        self.forms = []
        self.feed(body.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.links.add(attrs.get('href'))
        if tag == 'form':
            self.forms.append(attrs)


def test_common_member_claim_has_truthful_session_notice_and_real_logout(portal):
    web, _, _, facade, _ = portal
    facade.members.store.upload_eligibility('come3105', b'student_key\n20269999\n')
    status, headers, _ = signup(web)
    assert status == 303
    activate_member(web)
    token = cookie(headers)
    status, _, body = request(web, '/courses/come3105/portal', cookie=token)
    assert status == 200
    assert '공통 회원 로그인은 수령 코드 발급 후에도 유지됩니다' in body.decode()
    assert {'method': 'post', 'action': '/student/logout'} in LinksAndForms(body).forms
    status, _, body = request(web, '/courses/come3105/assignments', cookie=token)
    assert status == 200
    assert '/courses/come3105/activate-account' not in LinksAndForms(body).links
    assert '/student/account' in LinksAndForms(body).links
    status, _, body = request(web, '/courses/come3105/claims', method='POST', origin=WEB, cookie=token,
        data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    assert status == 200
    assert '공통 회원 로그인은 코드 발급 후에도 유지됩니다' in body.decode()
    assert '로그인은 코드 발급 후 종료' not in body.decode()
    assert '다시 인증하여 새 코드 받기' not in body.decode()
    elements = LinksAndForms(body)
    assert {'/student/account', '/courses/come3105/portal'} <= elements.links
    assert {'method': 'post', 'action': '/student/logout'} in elements.forms
    assert '/student/logout' not in elements.links  # Logout is never a GET link.

    # A visible logout control must revoke the same common session. Neither a
    # foreign origin nor a forged CSRF token may consume it first.
    logout = {'csrf': csrf(body)}
    assert request(web, '/student/logout', method='POST', origin='https://other.invalid', cookie=token, data=logout)[0] == 403
    assert request(web, '/student/logout', method='POST', origin=WEB, cookie=token, data={'csrf': 'invalid'})[0] == 403
    assert request(web, '/student/account', cookie=token)[0] == 200
    for target in ('/student/account', '/courses/come3105/portal'):
        assert request(web, target, cookie=token)[0] == 200
    assert request(web, '/student/logout', method='POST', origin=WEB, cookie=token, data=logout)[0] == 303
    assert request(web, '/student/account', cookie=token)[0] == 403
    assert b'name="assignment_id"' not in request(web, '/courses/come3105', cookie=token)[2]


def test_legacy_claim_is_already_logged_out_and_only_offers_reauthentication(portal):
    web, _, _, _, _ = portal
    status, headers, body = login(web, 'come3105')
    assert status == 200
    assert '/courses/come3105/activate-account' in LinksAndForms(body).links
    token = cookie(headers)
    status, _, home = request(web, '/courses/come3105/portal', cookie=token)
    assert status == 200
    assert '수업별 웹 로그인이 종료됩니다' in home.decode()
    assert '공통 회원 로그인은 수령 코드 발급 후에도 유지됩니다' not in home.decode()
    status, _, body = request(web, '/courses/come3105/claims', method='POST', origin=WEB, cookie=token,
        data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    assert status == 200
    assert '수업별 웹 로그인은 코드 발급 후 종료되었습니다' in body.decode()
    elements = LinksAndForms(body)
    assert '/courses/come3105' in elements.links
    assert not any(form.get('action') == '/student/logout' for form in elements.forms)
    assert b'name="password"' in request(web, '/courses/come3105/portal', cookie=token)[2]
