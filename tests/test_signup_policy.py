import pytest

from src.backend.core.auth.signup_policy import SignupPolicy


def P(mode="open", domains=(), emails=(), code=""):
    return SignupPolicy(mode, frozenset(domains), frozenset(emails), code)


def test_open_unrestricted_allows_anyone_and_warns():
    p = P()
    assert p.check_password_signup("a@gmail.com", None) is None
    assert p.check_google_signup("a@gmail.com", False) is None
    assert p.warning()


@pytest.mark.parametrize("email,ok", [
    ("me@lab.org", True), ("ME@Lab.Org", True), ("me@gmail.com", False),
    ("me@evil.lab.org.com", False), ("me@sub.lab.org", False), ("boss@gmail.com", True),
])
def test_allow_list(email, ok):
    p = P(domains={"lab.org"}, emails={"boss@gmail.com"})
    assert (p.check_password_signup(email, None) is None) is ok
    assert (p.check_google_signup(email, True) is None) is ok
    assert p.warning() is None


def test_google_needs_verified_email_when_restricted():
    assert P(domains={"lab.org"}).check_google_signup("me@lab.org", False)


def test_invite_mode():
    p = P("invite", code="s3cret")
    assert p.check_password_signup("a@b.c", None)
    assert p.check_password_signup("a@b.c", "wrong")
    assert p.check_password_signup("a@b.c", "s3cret") is None


def test_closed_mode():
    p = P("closed")
    assert p.check_password_signup("a@b.c", "x") and p.check_google_signup("a@b.c", True)


def test_from_env_validation(monkeypatch):
    monkeypatch.setenv("PHOTON_SIGNUP", "whatever")
    with pytest.raises(ValueError):
        SignupPolicy.from_env()
    monkeypatch.setenv("PHOTON_SIGNUP", "invite")
    monkeypatch.delenv("PHOTON_SIGNUP_INVITE_CODE", raising=False)
    with pytest.raises(ValueError):
        SignupPolicy.from_env()


def test_signup_route_uses_policy(app, monkeypatch):
    from src.backend.api import auth_routes
    monkeypatch.setattr(auth_routes, "signup_policy", P("closed"))
    c = app.test_client()
    r = c.post("/auth/signup", json={"username": "nobody1", "email": "n@x.org", "password": "password123"})
    assert r.status_code == 403
    assert "mode = 'signup'" not in c.get("/auth/login").get_data(as_text=True)


def test_cli_add_and_passwd(app, monkeypatch, capsys):
    import io
    from src.backend import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("longpassword1\n"))
    cli.main(["user", "add", "cliuser", "cli@x.org", "--password-stdin"])
    c = app.test_client()
    assert c.post("/auth/login", json={"username_or_email": "cliuser", "password": "longpassword1"}).status_code == 200
    monkeypatch.setattr("sys.stdin", io.StringIO("otherpassword2\n"))
    cli.main(["user", "passwd", "cli@x.org", "--password-stdin"])
    assert c.post("/auth/login", json={"username_or_email": "cliuser", "password": "otherpassword2"}).status_code == 200
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit):
        cli.main(["user", "passwd", "cliuser", "--password-stdin"])
