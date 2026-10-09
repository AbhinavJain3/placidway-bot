from pathlib import Path

import pytest

from app.ingest.chunker import MAX_CHARS, chunk_page
from app.ingest.extractor import extract

FIX = Path(__file__).parent / "fixtures"
PKG_URL = "https://www.placidway.com/package/7902/Alternative-Adrenal-Cancer-Treatment-Package-in-Tijuana-Mexico-by-ITC"


@pytest.fixture(scope="module")
def package_page():
    return extract((FIX / "package_adrenal.html").read_text(encoding="utf-8"), PKG_URL, "treatment_package")


def all_text(page):
    return "\n".join(s.text for s in page.sections)


def test_title_and_prices_extracted(package_page):
    assert package_page.title.startswith("Alternative Adrenal Cancer Treatment Package")
    text = all_text(package_page)
    assert "Program Type: Out-Patient Program | Duration: 3 Weeks | Total Cost (USD): $18,995" in text
    assert "$30,000" in text


def test_site_chrome_removed(package_page):
    text = all_text(package_page).lower()
    for noise in ("chat with alice", "related experiences", "trusted medical tourism platform", "plastic surgery\ndentistry"):
        assert noise not in text


def test_json_ld_price_fact(package_page):
    assert [(f.price, f.currency) for f in package_page.price_facts] == [("18,995", "USD")]


def test_table_rows_are_self_describing():
    page = extract((FIX / "price_comparison.html").read_text(encoding="utf-8"),
                   "https://www.placidway.com/search-medical-pricings/Alternative-Medicine/All/1", "price_comparison")
    text = all_text(page)
    assert "Procedure: Ozone Therapy Package | Low: $500 | High: $1,500" in text


def test_template_artifacts_removed():
    page = extract((FIX / "price_list.html").read_text(encoding="utf-8"),
                   "https://www.placidway.com/price-list/5/x", "price_list")
    assert "=$priceRange?>" not in all_text(page)
    assert len(page.price_facts) >= 5


def test_chunks_have_headers_and_bounded_size(package_page):
    chunks = chunk_page(package_page)
    assert chunks
    for c in chunks:
        assert c.text.startswith(f"[Page: {package_page.title}]")
        assert len(c.text) < MAX_CHARS + 400
    assert any(c.section == "Listed starting prices" for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)
