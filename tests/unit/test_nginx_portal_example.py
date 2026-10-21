"""Static configuration contract; deployment still requires nginx -t."""
from pathlib import Path


def test_portal_referrer_override_is_web_only():
    config = (Path(__file__).resolve().parents[2]
              / "config/nginx-autograde-portal.conf.example").read_text()
    web, api = config.split("server {")[1:]
    assert "listen 20010 ssl;" in web
    assert "proxy_pass http://127.0.0.1:18081;" in web
    assert web.count("proxy_hide_header Referrer-Policy;") == 1
    assert web.count('add_header Referrer-Policy "same-origin" always;') == 1
    assert "listen 20000 ssl;" in api
    assert "proxy_pass http://127.0.0.1:18080;" in api
    assert "Referrer-Policy" not in api
    assert "proxy_set_header Origin" not in config
    assert "proxy_hide_header Content-Security-Policy" not in config


def test_document_form_has_scoped_unicode_body_limit():
    config = (Path(__file__).resolve().parents[2]
              / "config/nginx-autograde-portal.conf.example").read_text()
    web, api = config.split("server {")[1:]
    prefix, editor = web.split(
        "location ~ ^/courses/[a-z0-9_-]+/instructor/assignments/[A-Za-z0-9_-]+/document$ {"
    )
    editor, rest = editor.split("}", 1)
    assert "client_max_body_size 64k;" in prefix
    assert "client_max_body_size 256k;" in editor
    assert "proxy_request_buffering off;" in editor
    assert "proxy_pass http://127.0.0.1:18081;" in editor
    assert "proxy_set_header Host $http_host;" in editor
    assert "proxy_set_header X-Forwarded-Proto https;" in editor
    assert "client_max_body_size" not in rest
    assert "/document" not in api


def test_exam_pdf_limit_is_scoped_to_authenticated_upload_only():
    config = (Path(__file__).resolve().parents[2]
              / "config/nginx-autograde-portal.conf.example").read_text()
    web, api = config.split("server {")[1:]
    prefix, location = web.split(
        "location ~ ^/courses/[a-z0-9_-]+/instructor/exam-papers/ep_[A-Za-z0-9_-]+/upload$ {"
    )
    location = location.split('}', 1)[0]
    assert 'client_max_body_size 64k;' in prefix
    assert 'client_max_body_size 11m;' in location
    assert 'proxy_request_buffering off;' in location
    assert 'proxy_pass http://127.0.0.1:18081;' in location
    assert 'exam-papers' not in api


def test_individual_material_upload_limits_do_not_publish_private_storage():
    config = (Path(__file__).resolve().parents[2]
              / "config/nginx-autograde-portal.conf.example").read_text()
    web, api = config.split("server {")[1:]
    for suffix, limit in [('upload', '26m'), ('upload-batch', '101m')]:
        location = web.split(f'location ~ ^/courses/[a-z0-9_-]+/instructor/materials/mat_[A-Za-z0-9_-]+/{suffix}$ {{')[1].split('}', 1)[0]
        assert f'client_max_body_size {limit};' in location
        assert 'proxy_request_buffering off;' in location
        assert 'proxy_pass http://127.0.0.1:18081;' in location
        assert 'access_log off;' in location
    assert 'client_max_body_size 64k;' in web
    assert '/materials/' not in api
    assert 'alias ' not in config
