"""The only pages the bot may learn from. Nothing outside this list is ever fetched."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    url: str
    page_type: str


SOURCES: list[Source] = [
    Source("https://www.placidway.com/profile/5/Alternative-Cancer-Treatment-by-ITC-Immunity-Therapy-Center", "clinic_profile"),
    Source("https://www.placidway.com/package/7903/Alternative-Adenocarcinoma-Cancer-Treatment-Package-in-Tijuana-Mexico-by-ITC", "treatment_package"),
    Source("https://www.placidway.com/package/7902/Alternative-Adrenal-Cancer-Treatment-Package-in-Tijuana-Mexico-by-ITC", "treatment_package"),
    Source("https://www.placidway.com/package/7901/Alternative-Anal-Cancer-Treatment-Package-in-Tijuana-Mexico-by-ITC", "treatment_package"),
    Source("https://www.placidway.com/profile-videos/5/Alternative-Cancer-Treatment-by-ITC-Immunity-Therapy-Center", "clinic_videos"),
    Source("https://www.placidway.com/price-list/5/Alternative-Cancer-Treatment-by-ITC-Immunity-Therapy-Center", "price_list"),
    Source("https://www.placidway.com/search-medical-pricings/Alternative-Medicine/All/1", "price_comparison"),
]
