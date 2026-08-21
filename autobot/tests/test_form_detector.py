"""
test_form_detector.py — Unit tests for form_detector classification logic.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from autobot.form_detector import FormDetector, FieldMap


class TestFieldClassification:
    """Test the _classify_form heuristic with synthetic form_data dicts."""

    def _make_input(self, type_: str, name: str, id_: str = "",
                    placeholder: str = "", required: bool = False) -> dict:
        fi, ii = 0, 0
        sel = f"#{id_}" if id_ else f'[name="{name}"]'
        return {
            "tag": "input", "type": type_, "name": name, "id": id_,
            "placeholder": placeholder, "aria-label": "", "autocomplete": "",
            "required": required, "label_text": "", "fi": fi, "ii": ii,
            "selector": sel,
        }

    def _form_data(self, inputs: list, form_text: str = "create account") -> dict:
        return {"index": 0, "formText": form_text, "action": "", "method": "post", "inputs": inputs}

    def _detect(self, inputs: list, form_text: str = "create account") -> FieldMap:
        detector = FormDetector.__new__(FormDetector)  # skip __init__ (no browser)
        return detector._classify_form(self._form_data(inputs, form_text))

    def test_email_field_detected(self):
        fm = self._detect([self._make_input("email", "email")])
        assert fm.email is not None

    def test_password_field_detected(self):
        fm = self._detect([self._make_input("password", "password")])
        assert fm.password is not None

    def test_confirm_password_detected(self):
        inputs = [
            self._make_input("password", "password"),
            self._make_input("password", "confirm_password"),
        ]
        fm = self._detect(inputs)
        assert fm.password is not None
        assert fm.confirm_password is not None

    def test_username_detected(self):
        fm = self._detect([self._make_input("text", "username")])
        assert fm.username is not None

    def test_high_confidence_register_form(self):
        inputs = [
            self._make_input("email",    "email"),
            self._make_input("text",     "username"),
            self._make_input("password", "password"),
            self._make_input("password", "confirm_password"),
        ]
        fm = self._detect(inputs, "create your account register")
        assert fm.confidence >= 0.5

    def test_low_confidence_login_form(self):
        # Login form: email + password only, login text — should be low confidence
        inputs = [
            self._make_input("email",    "email"),
            self._make_input("password", "password"),
        ]
        fm = self._detect(inputs, "log in to your account")
        assert fm.confidence < 0.5

    def test_payment_flag(self):
        inputs = [self._make_input("text", "credit_card")]
        fm = self._detect(inputs)
        assert fm.payment_required

    def test_phone_required_flag(self):
        inputs = [self._make_input("tel", "phone", required=True)]
        fm = self._detect(inputs)
        assert fm.phone_required
