#!/usr/bin/env python3
"""file_types tests — which name endings count as extensions.
Run: python3 services/mcp/common/test_file_types.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from file_types import file_ext, stored_name  # noqa: E402


class FileTypesTest(unittest.TestCase):
    def test_file_ext(self):
        self.assertEqual(file_ext("scan.PDF"), ".PDF")
        self.assertEqual(file_ext("Boarding Pass.pkpass"), ".pkpass")
        self.assertEqual(file_ext("notes.backup2024"), ".backup2024")

    def test_an_ending_with_a_space_is_no_extension(self):
        for name in ("Invoice No. 5", "Invoice No. 12345 from Acme Corp", "log in"):
            self.assertEqual(file_ext(name), "", name)

    def test_stored_name(self):
        self.assertEqual(stored_name("amazon receipt", "Receipt.pdf"), "amazon receipt.pdf")
        self.assertEqual(stored_name("amazon receipt.pdf", "Receipt.PDF"), "amazon receipt.pdf")
        self.assertEqual(stored_name("flight", "Boarding Pass.pkpass"), "flight.pkpass")
        self.assertEqual(stored_name("bill.jpg", "scan.pdf"), "bill.jpg.pdf")
        self.assertEqual(stored_name("acme invoice", "Invoice No. 5"), "acme invoice")

    def test_another_spelling_of_the_same_type_is_kept(self):
        self.assertEqual(stored_name("scan.jpeg", "att_1_download.jpg"), "scan.jpeg")
        self.assertEqual(stored_name("page.htm", "att_1_download.html"), "page.htm")
        self.assertEqual(stored_name("scan.jpeg", "att_1_download.pdf"), "scan.jpeg.pdf")


if __name__ == "__main__":
    unittest.main()
