import unittest

from catalog import page_count, paginate


class HiddenPagingTest(unittest.TestCase):
    def test_exact_multiples(self):
        self.assertEqual(page_count(40, 20), 2)
        self.assertEqual(page_count(20, 20), 1)
        self.assertEqual(page_count(100, 10), 10)

    def test_non_multiples(self):
        self.assertEqual(page_count(41, 20), 3)
        self.assertEqual(page_count(1, 20), 1)
        self.assertEqual(page_count(19, 20), 1)
        self.assertEqual(page_count(21, 20), 2)

    def test_empty_has_one_page(self):
        self.assertEqual(page_count(0, 20), 1)

    def test_bad_per_page(self):
        with self.assertRaises(ValueError):
            page_count(10, 0)

    def test_last_page_not_empty(self):
        products = list(range(40))
        last = paginate(products, 99, per_page=20)
        self.assertEqual(last.number, 2)
        self.assertEqual(last.items, list(range(20, 40)))
        self.assertFalse(last.has_next)
        self.assertEqual(last.label(), "Page 2 of 2")

    def test_partial(self):
        page = paginate(list(range(45)), 3, per_page=20)
        self.assertEqual(page.items, [40, 41, 42, 43, 44])
        self.assertEqual(page.total_pages, 3)


if __name__ == "__main__":
    unittest.main()
