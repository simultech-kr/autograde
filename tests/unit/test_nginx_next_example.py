from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_next_routes_do_not_intercept_legacy_or_student_api():
    text = (ROOT / "config/nginx-autograde-next.locations.conf.example").read_text()
    assert "location = /app {" in text
    assert "location ^~ /app/ {" in text
    assert "location ^~ /instructor/api/v1/ {" in text
    assert "location /courses" not in text
    assert "location /v1" not in text
    assert "proxy_pass http://127.0.0.1:18082;" in text
    assert "proxy_pass http://127.0.0.1:18081;" in text
    assert "proxy_set_header Origin" not in text
    api = text.split("location ^~ /instructor/api/v1/ {")[1].split("}")[0]
    assert "proxy_request_buffering off;" in api
    assert 'proxy_set_header Authorization "";' not in api
    assert 'proxy_set_header Cookie "";' not in api
    assert "proxy_intercept_errors off;" in api
    # This ^~ prefix overrides the dedicated portal SSE regex location.
    assert "proxy_http_version 1.1;" in api
    assert "proxy_buffering off;" in api
    assert "proxy_cache off;" in api
    assert "proxy_read_timeout 45s;" in api
    assert 'error_log /dev/null crit;' in api
    assert text.count('error_log /dev/null crit;') == 4


def test_fallback_does_not_depend_on_node_and_is_not_a_post_replay():
    text = (ROOT / "config/nginx-autograde-next.locations.conf.example").read_text()
    assert "error_page 502 503 504 =503 @autograde_next_unavailable;" in text
    fallback = text.split("location @autograde_next_unavailable {")[1].split("}")[0]
    assert "return 503" in fallback and 'href="/instructor"' in fallback
    assert "proxy_pass" not in fallback
    assert 'Cache-Control "no-store"' in fallback


def test_next_include_is_opt_in_and_web_only():
    text = (ROOT / "config/nginx-autograde-portal.conf.example").read_text()
    web, api = text.split("server {")[1:]
    assert "# include /opt/autograde/config/nginx-autograde-next.locations.conf.example;" in web
    assert "nginx-autograde-next" not in api
