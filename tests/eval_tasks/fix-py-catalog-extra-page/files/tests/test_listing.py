import unittest

from catalog import paginate


class ListingTest(unittest.TestCase):
    def test_first_page(self):
        page = paginate(list(range(45)), 1, per_page=20)
        self.assertEqual(page.items, list(range(20)))
        self.assertFalse(page.has_prev)
        self.assertTrue(page.has_next)

    def test_last_partial_page(self):
        page = paginate(list(range(45)), 3, per_page=20)
        self.assertEqual(page.items, list(range(40, 45)))
        self.assertEqual(page.label(), "Page 3 of 3")

    def test_empty(self):
        page = paginate([], 1)
        self.assertEqual(page.items, [])
        self.assertEqual(page.total_pages, 1)


if __name__ == "__main__":
    unittest.main()
