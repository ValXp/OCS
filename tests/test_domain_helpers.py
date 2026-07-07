import re
import unittest

from opencode_session.domain_helpers import append_unique_string, string_list, utc_now


class DomainHelpersTest(unittest.TestCase):
    def test_utc_now_uses_canonical_utc_timestamp_format(self):
        self.assertRegex(utc_now(), re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"))

    def test_string_list_keeps_only_non_empty_strings_from_lists(self):
        self.assertEqual(string_list(["ses_1", "", None, "ses_2", 3]), ("ses_1", "ses_2"))
        self.assertEqual(string_list(("ses_1",)), ())

    def test_append_unique_string_appends_only_new_non_empty_strings(self):
        values = ["ses_1"]

        append_unique_string(values, "ses_1")
        append_unique_string(values, "")
        append_unique_string(values, None)
        append_unique_string(values, "ses_2")

        self.assertEqual(values, ["ses_1", "ses_2"])


if __name__ == "__main__":
    unittest.main()
