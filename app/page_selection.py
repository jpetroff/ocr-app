"""One-based page selection shared by the CLI, HTTP adapters and coordinator."""

from dataclasses import dataclass
import re

from .errors import OCRInputError


@dataclass(frozen=True)
class PageRange:
    start: int = 1
    end: int | None = None

    def __post_init__(self):
        for value in (self.start, self.end):
            if value is not None and (type(value) is not int or value <= 0):
                raise OCRInputError("Page numbers must be positive integers.")
        if self.start is None or (self.end is not None and self.start > self.end):
            raise OCRInputError("Page ranges must have increasing endpoints.")


def parse_page_ranges(value: str) -> tuple[PageRange, ...]:
    """Parse inclusive ranges without expanding open or very large ranges."""
    ranges = []
    for item in value.split(","):
        item = item.strip()
        if not re.fullmatch(r"[0-9]+|[0-9]*-[0-9]*", item):
            raise OCRInputError("Use comma-separated pages/ranges, e.g. 1-23,45-44,50,79- or -5.")
        if "-" not in item:
            start = end = int(item)
        else:
            left, right = item.split("-")
            start, end = int(left) if left else 1, int(right) if right else None
            if end is not None and start > end:
                start, end = end, start
        ranges.append(PageRange(start, end))
    return tuple(ranges)


@dataclass(frozen=True)
class PageSelection:
    include: tuple[PageRange, ...] | None = None
    exclude: tuple[PageRange, ...] | None = None

    def __post_init__(self):
        if self.include is not None and self.exclude is not None:
            raise OCRInputError("include and exclude are mutually exclusive.")
        if self.include == () or self.exclude == ():
            raise OCRInputError("Page selection must not be empty.")

    def resolve(self, page_count: int) -> list[int]:
        ranges = self.include if self.include is not None else self.exclude
        if ranges is None:
            return list(range(1, page_count + 1))
        selected = set()
        for interval in ranges:
            end = page_count if interval.end is None else interval.end
            if interval.start > page_count or end > page_count:
                raise OCRInputError(f"Page selection is out of range; document has {page_count} page(s).")
            selected.update(range(interval.start, end + 1))
        if self.exclude is not None:
            selected = set(range(1, page_count + 1)) - selected
        if not selected:
            raise OCRInputError("Page selection leaves no pages to process.")
        return sorted(selected)
