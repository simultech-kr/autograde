"""Participation bookmarks survive the actual HTTP query allowlist."""
from html.parser import HTMLParser
import json
from urllib.parse import urlencode, urlsplit

import pytest

from autograde.platform_portal import CoursePortal
from test_assignment_participation import COURSE, participation
from test_course_portal import request, serving
from test_instructor_ux import release
from test_instructor_web import AUTH, BASE, WEB, setup


class ParticipationLinks(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and 'data-results-choice' in attrs:
            self.links.append(attrs['href'])


@pytest.fixture(params=['admin', 'legacy', 'single'])
def query_server(participation, request):
    browser, state, service, _, first, latest, other = participation
    mode = request.param
    if mode == 'single':
        facade, origin, page = service, service.public_base_url, '/instructor'
        live = '/v1/instructor/dashboard/live'
    else:
        facade = CoursePortal({COURSE: service}, b'w' * 32, WEB, service.public_base_url,
                              instructor=browser.web if mode == 'admin' else None)
        origin, page = WEB, BASE + '/submissions'
        live = page + '/live'
    with serving(facade, origin) as server:
        yield dict(server=server, mode=mode, browser=browser, state=state, service=service,
                   page=page, live=live, submissions=(first, latest, other))


def test_generated_participation_links_and_bookmarked_filters_load(query_server):
    context = query_server
    server, page = context['server'], context['page']
    status, headers, body = request(server, page, authorization=AUTH)
    assert status == 200
    links = ParticipationLinks(body.decode()).links
    assert len(links) == 6
    for link in links:
        target = link if context['mode'] != 'single' else page + '?' + urlsplit(link).query
        status, headers, body = request(server, target, authorization=AUTH)
        assert status == 200, body
        assert headers['Cache-Control'] == 'no-store'
        assert b'data-live-results' in body and b'data-results-assignment' in body
    for fields in ({'result_view': 'pending'}, {'result_view': 'attention'},
                   {'assignment_id': 'participation_a'}, {'result_view': 'all'},
                   {'assignment_id': 'asn_no_longer_listed', 'result_view': 'submitted'}):
        assert request(server, page + '?' + urlencode(fields), authorization=AUTH)[0] == 200
    if context['mode'] == 'legacy':
        assert request(server, BASE + '?assignment_id=participation_a&result_view=unsubmitted',
                       authorization=AUTH)[0] == 200


def test_participation_query_validation_remains_narrow(query_server):
    server, page = query_server['server'], query_server['page']
    invalid = [
        'unknown=value', 'assignment_id=participation_a&unknown=value',
        'result_view=unknown', 'result_view=', 'assignment_id=',
        'assignment_id=../private', 'assignment_id=bad%2Fpath',
        'assignment_id=%22%3E%3Cimg%20src=x%3E', 'assignment_id=%ED%95%9C',
        'assignment_id=' + 'a' * 129,
        'result_view=submitted&result_view=unsubmitted',
        'assignment_id=participation_a&assignment_id=participation_b',
    ]
    for query in invalid:
        status, _, body = request(server, page + '?' + query, authorization=AUTH)
        assert status == 400, (query, body)
        assert json.loads(body)['error']['code'] == 'invalid_request'


def test_participation_queries_do_not_enable_live_api_or_post_requests(query_server):
    server, page, live = query_server['server'], query_server['page'], query_server['live']
    query = '?assignment_id=participation_a&result_view=submitted'
    assert request(server, live + query, authorization=AUTH)[0] == 400
    assert request(server, live + '?unknown=value', authorization=AUTH)[0] == 400
    status, _, _ = request(server, page + query, method='POST', authorization=AUTH,
                          origin=WEB, data={})
    assert status == (405 if query_server['mode'] == 'single' else 400)
    if query_server['mode'] == 'single':
        assert request(server, '/v1/instructor/dashboard' + query, authorization=AUTH)[0] == 400
    else:
        assert request(server, BASE + '/settings' + query, authorization=AUTH)[0] == 400
        assert request(server, '/courses/come2201' + query, authorization=AUTH)[0] == 400


def test_participation_bookmarks_still_require_instructor_authentication(query_server):
    server = query_server['server']
    path = query_server['page'] + '?assignment_id=participation_a&result_view=unsubmitted'
    assert request(server, path)[0] == 401
    assert request(server, path, token='synthetic-student-token')[0] == 401
    assert request(server, path, authorization='Basic invalid')[0] == 401
    assert request(server, path, authorization=AUTH)[0] == 200


def test_participation_query_does_not_expand_course_scope_or_mutate_submissions(query_server):
    context = query_server
    state = context['state']
    release(state, course='come3105', assignment_id='foreign_assignment')
    before = [state.get_bundle_submission(item.submission_id) for item in context['submissions']]
    for assignment_id in ('participation_a', 'foreign_assignment'):
        path = context['page'] + '?' + urlencode(dict(assignment_id=assignment_id, result_view='submitted'))
        status, _, body = request(context['server'], path, authorization=AUTH)
        assert status == 200
        assert b'data-assignment-id="foreign_assignment"' not in body
        assert b'data-assignment-id="participation_a"' in body
    assert [state.get_bundle_submission(item.submission_id) for item in context['submissions']] == before
