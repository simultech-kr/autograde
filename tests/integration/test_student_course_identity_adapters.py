"""Course-backed common login preserves score and optional-consent boundaries."""
from dataclasses import replace
import json

import pytest

from autograde.platform_state import PlatformAccessDenied
from autograde.student_privacy import POLICY_VERSION
from test_course_portal import portal
from test_student_browser_sessions import CookieBrowser
from test_student_gradebook_http import prepare as prepare_gradebook


COURSE = 'come3105'
KEY = '20260001'


def json_response(browser, path, *, data=None):
    status, headers, body = browser.request(path, data=data)
    assert headers['Content-Type'].startswith('application/json'), body
    assert headers['Cache-Control'] == 'no-store'
    return status, json.loads(body)


def course_browser(portal):
    browser = CookieBrowser(portal[0])
    status, boot = json_response(browser, '/student/api/bootstrap')
    assert status == 200
    status, logged = json_response(browser, '/student/api/login', data={
        'csrf': boot['result']['csrf'], 'student_key': KEY, 'password': '000001'})
    assert status == 200, logged
    assert logged['result']['auth_method'] == 'course'
    return browser


def proof(portal):
    return portal[2][COURSE].state.get_student_password_credential(student_key=KEY, course_key=COURSE)


def private_counts(portal):
    with portal[2][COURSE].state._connection() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                     for name in ('student_accounts', 'student_privacy_consents', 'student_privacy_events'))


def test_course_proofs_overview_never_expands_to_other_enrollments_or_creates_records(portal):
    privacy = portal[3].privacy
    result = privacy.overview_for_credentials(KEY, (proof(portal),), allowed_courses={'come2201', COURSE})
    assert [row['course_key'] for row in result['consents']] == [COURSE]
    assert result['student_key'] == KEY
    assert result['consents'][0]['decision'] == 'unanswered'
    assert result['consents'][0]['course_name']
    assert 'password' not in json.dumps(result)
    assert privacy.overview_for_credentials(KEY, (proof(portal),), allowed_courses=set())['consents'] == []
    assert private_counts(portal) == (0, 0, 0)


@pytest.mark.parametrize('change', ['password', 'enrollment', 'student', 'course', 'lock'])
def test_course_privacy_projection_rechecks_proof_inside_its_snapshot(portal, change):
    old = proof(portal)
    state = portal[2][COURSE].state
    with state._write() as db:
        if change == 'password':
            db.execute("UPDATE platform_student_passwords SET password_hash='replaced' WHERE enrollment_id=?", (old.enrollment_id,))
        elif change == 'enrollment':
            db.execute('UPDATE platform_enrollments SET active=0 WHERE id=?', (old.enrollment_id,))
        elif change == 'student':
            db.execute('UPDATE platform_students SET active=0 WHERE id=?', (old.student_id,))
        elif change == 'course':
            db.execute("UPDATE admin_courses SET status='preparation' WHERE course_key=?", (COURSE,))
        else:
            db.execute("UPDATE platform_student_passwords SET locked_until='2099-01-01T00:00:00Z' WHERE enrollment_id=?", (old.enrollment_id,))
    with pytest.raises(PlatformAccessDenied):
        portal[3].privacy.overview_for_credentials(KEY, (old,), allowed_courses={COURSE})
    assert private_counts(portal) == (0, 0, 0)


def test_course_privacy_projection_rejects_mixed_identity_duplicate_and_empty_proofs(portal):
    credential = proof(portal)
    for credentials in ((), (credential, credential), (replace(credential, student_key='20260002'),)):
        with pytest.raises(PlatformAccessDenied):
            portal[3].privacy.overview_for_credentials(KEY, credentials, allowed_courses={COURSE})
    assert private_counts(portal) == (0, 0, 0)


def test_course_login_reads_own_gradebook_and_privacy_without_member_signup(portal):
    prepare_gradebook(portal)
    browser = course_browser(portal)
    status, data = json_response(browser, f'/student/api/course/{COURSE}')
    assert status == 200
    assert [item['value'] for item in data['result']['gradebook']['items']] == [0, None, 9.5, '<img src=x onerror=alert(1)>']
    assert '20260002' not in json.dumps(data) and 'ONLY-OTHER-STUDENT' not in json.dumps(data)
    status, _, page = browser.request(f'/courses/{COURSE}/results')
    assert status == 200 and b'&lt;img src=x' in page and b'ONLY-OTHER-STUDENT' not in page
    status, overview = json_response(browser, '/student/api/privacy')
    assert status == 200
    assert [row['course_key'] for row in overview['result']['consents']] == [COURSE]
    assert json_response(browser, '/student/api/course/come2201/privacy')[0] == 403
    assert private_counts(portal) == (0, 0, 0)
    # Closing a reminder is not an opt-in or an account registration.
    assert json_response(browser, '/student/api/privacy/dismiss', data={
        'csrf': overview['result']['csrf'], 'policy_version': POLICY_VERSION})[0] == 200
    assert private_counts(portal) == (0, 0, 0)
    status, own = json_response(browser, f'/student/api/course/{COURSE}/privacy')
    assert status == 200 and own['result']['prompt_dismissed']
    status, choice = json_response(browser, f'/student/api/course/{COURSE}/privacy', data={
        'csrf': overview['result']['csrf'], 'policy_version': POLICY_VERSION,
        'decision': 'declined', 'revision': str(own['result']['revision'])})
    assert status == 200 and choice['result']['decision'] == 'declined'
    assert private_counts(portal) == (0, 1, 1)


def test_course_gradebook_revocation_during_projection_is_rechecked(portal, monkeypatch):
    prepare_gradebook(portal)
    browser = course_browser(portal)
    gradebook = portal[3].gradebook
    original = gradebook.student_view

    def revoke(*args, **kwargs):
        portal[2][COURSE].set_student_password(student_key=KEY, password='123456')
        return original(*args, **kwargs)

    monkeypatch.setattr(gradebook, 'student_view', revoke)
    status, payload = json_response(browser, f'/student/api/course/{COURSE}')
    assert status in {401, 403}
    assert 'gradebook' not in json.dumps(payload) and '9.5' not in json.dumps(payload)


def test_course_privacy_revocation_between_session_and_overview_returns_no_choices(portal, monkeypatch):
    browser = course_browser(portal)
    original = portal[3].privacy.overview_for_credentials

    def revoke(*args, **kwargs):
        portal[2][COURSE].set_student_password(student_key=KEY, password='123456')
        return original(*args, **kwargs)

    monkeypatch.setattr(portal[3].privacy, 'overview_for_credentials', revoke)
    status, payload = json_response(browser, '/student/api/privacy')
    assert status == 401 and 'consents' not in json.dumps(payload)
    assert private_counts(portal) == (0, 0, 0)
