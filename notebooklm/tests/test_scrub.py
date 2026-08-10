"""Auth material must never reach the log."""

from __future__ import annotations

import unittest

from notebooklm.scrub import REDACTED, scrub_args, scrub_text


class ScrubTextTest(unittest.TestCase):
    def assert_clean(self, text: str, secret: str) -> None:
        scrubbed = scrub_text(text)
        self.assertNotIn(secret, scrubbed)
        self.assertIn(REDACTED, scrubbed)

    def test_session_cookies(self) -> None:
        self.assert_clean("Cookie: SID=abcd1234efgh; other=1", "abcd1234efgh")

    def test_secure_psidts_cookie(self) -> None:
        self.assert_clean("__Secure-1PSIDTS=sidts-CjEB3e4Ab", "sidts-CjEB3e4Ab")

    def test_oauth_bearer_token(self) -> None:
        self.assert_clean("Authorization: ya29.a0AfB_byC-longtokenvalue",
                          "ya29.a0AfB_byC-longtokenvalue")

    def test_master_token_shape(self) -> None:
        self.assert_clean("got aas_et/AKppINa-secretvalue back", "aas_et/AKppINa-secretvalue")

    def test_json_token_member(self) -> None:
        self.assert_clean('{"oauth_token": "oauth2_4/0AfsomethingSecret"}',
                          "oauth2_4/0AfsomethingSecret")

    def test_playwright_storage_state_cookie_entry(self) -> None:
        """The cookie NAME is a value here, and the secret hides under a generic
        "value" key - no key-name rule would ever catch it."""
        self.assert_clean(
            '{"name": "__Secure-1PSID", "value": "g.a000zwhSECRETVALUE", "domain": ".google.com"}',
            "g.a000zwhSECRETVALUE",
        )

    def test_playwright_storage_state_with_reversed_member_order(self) -> None:
        self.assert_clean(
            '{"value": "g.a000zwhSECRETVALUE", "name": "SAPISID"}', "g.a000zwhSECRETVALUE"
        )

    def test_bare_long_value_member_is_redacted(self) -> None:
        self.assert_clean('{"value": "0123456789abcdefghijklmnop"}', "0123456789abcdefghijklmnop")

    def test_authorization_bearer_credential_not_just_the_scheme(self) -> None:
        self.assert_clean("Authorization: Bearer abcDEF123456ghiJKL", "abcDEF123456ghiJKL")

    def test_sapisidhash_credential(self) -> None:
        self.assert_clean("Authorization: SAPISIDHASH 1754_deadbeefcafe", "1754_deadbeefcafe")

    def test_python_repr_cookie_dict(self) -> None:
        self.assert_clean("{'SID': 'g.a000_reprSECRET', 'HSID': 'A6xSECRET2'}",
                          "g.a000_reprSECRET")

    def test_cookie_names_are_matched_case_insensitively(self) -> None:
        self.assert_clean("sid=lowercaseSECRETvalue", "lowercaseSECRETvalue")

    def test_domain_and_other_metadata_survive_so_logs_stay_useful(self) -> None:
        scrubbed = scrub_text(
            '{"name": "SID", "value": "SECRETVALUE", "domain": ".google.com"}'
        )
        self.assertIn("domain", scrubbed)
        self.assertIn(".google.com", scrubbed)
        self.assertNotIn("SECRETVALUE", scrubbed)

    def test_ordinary_text_is_untouched(self) -> None:
        message = "generation complete for notebook nb_12345 in 842s"
        self.assertEqual(scrub_text(message), message)

    def test_none_and_empty(self) -> None:
        self.assertEqual(scrub_text(None), "")
        self.assertEqual(scrub_text(""), "")


class ScrubArgsTest(unittest.TestCase):
    def test_value_after_a_secret_flag_is_masked(self) -> None:
        args = ["notebooklm", "login", "--oauth-token", "oauth2_4/0Afsecret", "--json"]
        self.assertEqual(
            scrub_args(args),
            ["notebooklm", "login", "--oauth-token", REDACTED, "--json"],
        )

    def test_equals_form_is_masked(self) -> None:
        self.assertEqual(scrub_args(["--oauth-token=secretvalue"]), [f"--oauth-token={REDACTED}"])

    def test_ordinary_argv_survives(self) -> None:
        args = ["notebooklm", "generate", "audio", "deep dive on physics", "--length", "long"]
        self.assertEqual(scrub_args(args), args)


if __name__ == "__main__":
    unittest.main()
