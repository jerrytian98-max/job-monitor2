"""
招聘网站爬虫 - 带实时状态推送
"""

from scraper import UniversalScraperWithPlaywright
from typing import List, Dict, Callable
import logging

logger = logging.getLogger(__name__)


class UniversalScraperWithCallback(UniversalScraperWithPlaywright):
    """通用爬虫 - 带回调"""

    def __init__(self, status_callback: Callable = None, job_callback: Callable = None):
        super().__init__()
        self.status_callback = status_callback
        self.job_callback = job_callback

    def _notify_status(self, message: str, status_type: str = 'info'):
        """通知状态更新"""
        if self.status_callback:
            self.status_callback(message, status_type)

    def _notify_job_found(self, job: Dict):
        """通知发现新职位"""
        if self.job_callback:
            self.job_callback(job)

    def scrape_jobs(
        self,
        target_url: str,
        keywords: List[str],
        cities: List[str] = None,
        search_mode: str = 'search',
    ) -> List[Dict]:
        jobs = []

        self._notify_status(f'开始抓取: {target_url}...', 'info')

        try:
            # 调用父类方法抓取
            scraped_jobs = super().scrape_jobs(
                target_url,
                keywords,
                cities,
                search_mode=search_mode,
            )

            for job in scraped_jobs:
                self._notify_job_found(job)
                jobs.append(job)
                self._notify_status(f'发现职位: {job["title"]}', 'success')

            if scraped_jobs:
                self._notify_status(f'从 {target_url} 找到 {len(scraped_jobs)} 个匹配职位', 'success')
            else:
                self._notify_status(f'从 {target_url} 未找到匹配职位', 'warning')

        except Exception as e:
            self._notify_status(f'抓取失败: {target_url}', 'error')
            logger.error(f"抓取失败: {e}")

        return jobs


def get_scraper_with_callback(site_url: str, status_callback: Callable = None, job_callback: Callable = None):
    """获取带回调的通用爬虫实例"""
    return UniversalScraperWithCallback(status_callback, job_callback)
