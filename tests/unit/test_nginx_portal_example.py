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
