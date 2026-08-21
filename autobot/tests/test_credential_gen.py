"""
test_credential_gen.py — Unit tests for credential_gen module.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from autobot.credential_gen import (
    generate_username,
    generate_password,
    generate_display_name,
    generate_credentials,
)


class TestGenerateUsername:
    def test_format(self):
        u = generate_username()
        assert len(u) >= 8, "username too short"
        assert u[-4:].isdigit(), f"Expected trailing 4 digits, got: {u}"

    def test_uniqueness(self):
        names = {generate_username() for _ in range(50)}
        assert len(names) >= 45, "Too many duplicates in 50 generated usernames"

    def test_no_spaces(self):
        for _ in range(20):
            assert " " not in generate_username()


class TestGeneratePassword:
    def test_length(self):
        pw = generate_password()
        assert len(pw) == 18

    def test_custom_length(self):
        pw = generate_password(length=24)
        assert len(pw) == 24

    def test_has_upper(self):
        pw = generate_password()
        assert any(c.isupper() for c in pw), "No uppercase in password"

    def test_has_lower(self):
        pw = generate_password()
        assert any(c.islower() for c in pw), "No lowercase in password"

    def test_has_digit(self):
        pw = generate_password()
        assert any(c.isdigit() for c in pw), "No digit in password"

    def test_has_symbol(self):
        symbols = set("!#$%^&*-_=+")
        pw = generate_password()
        assert any(c in symbols for c in pw), "No symbol in password"

    def test_uniqueness(self):
        passwords = {generate_password() for _ in range(30)}
        assert len(passwords) == 30, "Duplicate passwords generated"


class TestGenerateDisplayName:
    def test_has_space(self):
        name = generate_display_name()
        assert " " in name, f"Expected first + last name, got: {name}"

    def test_two_parts(self):
        parts = generate_display_name().split()
        assert len(parts) == 2


class TestGenerateCredentials:
    def test_keys_present(self):
        creds = generate_credentials("test@example.com")
        for key in ["email", "username", "password", "display_name", "first_name", "last_name"]:
            assert key in creds, f"Missing key: {key}"

    def test_email_preserved(self):
        email = "hello@mail.tm"
        creds = generate_credentials(email)
        assert creds["email"] == email

    def test_password_strength(self):
        creds = generate_credentials("x@x.com")
        pw = creds["password"]
        assert len(pw) >= 16
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)
