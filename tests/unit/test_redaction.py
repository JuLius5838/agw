"""Unit tests for secret redaction.

Two invariants matter equally:
  * every seeded secret shape is removed from rendered output, and
  * legitimate routing evidence (public model names, provider names) is preserved.
"""

from __future__ import annotations

from agent_gateway.redaction import REDACTED, Redactor, redact


def test_redacts_bearer_token():
    out = redact("Authorization: Bearer abc123.def-456_ghi/jk=")
    assert "abc123.def-456_ghi" not in out
    assert REDACTED in out


def test_redacts_sk_proxy_key():
    out = redact("using proxy key sk-agw-XYZ_abc-1234567890 now")
    assert "sk-agw-XYZ_abc-1234567890" not in out


def test_redacts_github_token():
    out = redact("token=gho_16charsAAAAAAAAAAAAAAAAAAAAAAaa")
    assert "gho_16charsAAAAAAAAAAAAAAAAAAAAAAaa" not in out


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456"
    out = redact(f"id_token {jwt}")
    assert jwt not in out


def test_redacts_json_secret_fields_but_keeps_model():
    out = redact('{"access_token": "super-secret-value", "model": "gpt-5.3-codex"}')
    assert "super-secret-value" not in out
    assert "gpt-5.3-codex" in out  # public routing evidence preserved


def test_redacts_yaml_secret_lines_but_keeps_provider():
    out = redact("api_key: sekret-value-1234\nprovider: chatgpt\n")
    assert "sekret-value-1234" not in out
    assert "chatgpt" in out


def test_public_routing_line_is_unchanged():
    text = "routing model=gpt-5.3-codex provider=chatgpt latency=123ms status=ok"
    assert redact(text) == text


def test_redactor_masks_exact_registered_secret():
    r = Redactor()
    device_code = "WDJB-MJHT"
    r.add_secret(device_code)
    out = r.redact(f"Enter code {device_code} to authorize")
    assert device_code not in out


def test_redactor_ignores_trivially_short_secret():
    r = Redactor()
    r.add_secret("ab")  # below min length; must not blanket-redact the letters "ab"
    assert r.redact("grab a cab") == "grab a cab"


def test_all_seeded_secrets_absent_from_output():
    r = Redactor()
    seeded = ["sk-agw-abcdef1234567890", "WDJB-MJHT", "super-refresh-token-000"]
    for secret in seeded:
        r.add_secret(secret)
    blob = "\n".join(
        [
            "Authorization: Bearer tok.tok.tok.tok.tok",
            '{"refresh_token": "super-refresh-token-000"}',
            "device_code: WDJB-MJHT",
            "proxy_key: sk-agw-abcdef1234567890",
            "model: gpt-5.3-codex",  # must survive
        ]
    )
    out = r.redact(blob)
    for secret in seeded:
        assert secret not in out
    assert "tok.tok.tok" not in out
    assert "gpt-5.3-codex" in out
