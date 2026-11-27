"""Turn a product list into a page object for the templates."""

from dataclasses import dataclass, field

from .paging import page_bounds, page_count


@dataclass
class Page:
    number: int
    total_pages: int
    items: list = field(default_factory=list)

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    def label(self) -> str:
        return f"Page {self.number} of {self.total_pages}"


def paginate(products: list, page: int, per_page: int = 20) -> Page:
    pages = page_count(len(products), per_page)
    if page > pages:
        page = pages
    start, end = page_bounds(page, per_page, len(products))
    return Page(number=page, total_pages=pages, items=products[start:end])
