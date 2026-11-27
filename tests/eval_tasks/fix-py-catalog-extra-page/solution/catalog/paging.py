"""Page arithmetic shared by the storefront and the admin export."""


def page_count(total: int, per_page: int) -> int:
    """Number of pages needed to show ``total`` items, ``per_page`` at a time.

    An empty catalog still has one (empty) page so the template can render.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if total <= 0:
        return 1
    return (total + per_page - 1) // per_page


def page_bounds(page: int, per_page: int, total: int) -> tuple[int, int]:
    """Slice bounds ``(start, end)`` for 1-based ``page``."""
    if page < 1:
        raise ValueError("page numbers start at 1")
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    return min(start, total), end
