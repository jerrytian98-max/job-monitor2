"""Recruitment scraper interface and the active official-site adapter."""

from typing import List, Dict


class JobScraper:
    """Shared interface for direct and callback-based scrapers."""

    def scrape_jobs(
        self, target_url: str, keywords: List[str], cities: List[str] = None,
        search_mode: str = 'search',
    ) -> List[Dict]:
        raise NotImplementedError


class UniversalScraperWithPlaywright(JobScraper):
    """Use the API-aware official-site parser and its rendered-page fallback."""

    def scrape_jobs(
        self, target_url: str, keywords: List[str], cities: List[str] = None,
        search_mode: str = 'search',
    ) -> List[Dict]:
        from official_site_scraper import OfficialSiteScraper
        return OfficialSiteScraper().scrape_jobs(
            target_url, keywords, cities, search_mode=search_mode,
        )


def get_scraper(site_url: str, use_real: bool = True) -> JobScraper:
    """Keep the factory signature while routing every URL to the active adapter."""
    return UniversalScraperWithPlaywright()
