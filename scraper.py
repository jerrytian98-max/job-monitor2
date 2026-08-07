"""
招聘网站真实爬虫模块
实现真实的招聘网站数据抓取
"""

import requests
from bs4 import BeautifulSoup
import time
import random
import re
from typing import List, Dict
import logging
from urllib.parse import urljoin, quote, urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class JobScraper:
    """招聘网站爬虫基类"""

    def __init__(self):
        self.session = requests.Session()
        # 设置更真实的请求头
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })

    def _random_delay(self, min_seconds=3, max_seconds=8):
        """随机延迟，避免被识别为爬虫"""
        delay = random.uniform(min_seconds, max_seconds)
        logger.info(f"等待 {delay:.1f} 秒后继续...")
        time.sleep(delay)

    def scrape_jobs(
        self,
        target_url: str,
        keywords: List[str],
        cities: List[str] = None,
        search_mode: str = 'search',
    ) -> List[Dict]:
        """
        爬取职位信息
        """
        raise NotImplementedError

from playwright.sync_api import sync_playwright

class UniversalScraperWithPlaywright(JobScraper):
    """
    使用 Playwright 自动打开目标网址，寻找搜索框并输入关键词进行搜索。
    搜索完成后再提取页面上的职位链接和描述。
    """
    
    def scrape_jobs(
        self,
        target_url: str,
        keywords: List[str],
        cities: List[str] = None,
        search_mode: str = 'search',
    ) -> List[Dict]:
        # Modern official career sites are API-driven SPAs.  Delegate to the
        # response-aware adapter, which keeps full duties and requirements and
        # falls back to rendered cards for encrypted sites such as Moka.
        from official_site_scraper import OfficialSiteScraper
        return OfficialSiteScraper().scrape_jobs(
            target_url,
            keywords,
            cities,
            search_mode=search_mode,
        )

        # Retained below only as historical context; the adapter above is the
        # active implementation.
        jobs = []
        parsed_target = urlparse(target_url)
        if parsed_target.scheme not in ('http', 'https') or not parsed_target.netloc:
            logger.error(f"无效的招聘网址: {target_url}")
            return jobs

        # 未设置关键词时仍扫描当前页面，后续匹配器会接受全部职位。
        search_terms = keywords or ['']

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                for keyword in search_terms:
                    try:
                        task_name = f"搜索: {keyword}" if keyword else "扫描当前页面"
                        logger.info(f"正在打开 {target_url}，{task_name}")
                        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
                        self._random_delay(2, 4)
                        
                        # 尝试定位搜索框，常见的占位符或类型
                        search_box = None
                        selectors = [
                            "input[type='search']",
                            "input[placeholder*='搜索']",
                            "input[placeholder*='职位']",
                            "input[placeholder*='关键词']",
                            "input[placeholder*='Search']",
                            "input.search-input",
                            "#search-input"
                        ]
                        
                        for sel in selectors:
                            try:
                                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                                    search_box = page.locator(sel).first
                                    break
                            except Exception:
                                pass
                                
                        if not keyword:
                            logger.info("未设置关键词，直接分析当前页面内容")
                        elif not search_box:
                            logger.warning(f"在 {target_url} 中未找到明显的搜索框，尝试直接分析当前页面内容...")
                        else:
                            logger.info(f"找到搜索框，输入关键词: {keyword}")
                            search_box.fill(keyword)
                            self._random_delay(1, 2)
                            search_box.press("Enter")
                            
                            # 等待搜索结果加载
                            logger.info("等待搜索结果加载...")
                            page.wait_for_timeout(5000) # 简单等待 5 秒，SPA 应用通常够了
                            
                        # 获取页面内容，使用简单的正则提取链接
                        content = page.content()
                        soup = BeautifulSoup(content, 'html.parser')
                        parsed_domain = urlparse(target_url).netloc
                        
                        # 尝试寻找 <a> 标签作为职位链接
                        links = soup.find_all('a', href=True)
                        extracted_count = 0
                        for a in links:
                            text = a.get_text(strip=True)
                            href = a['href']
                            
                            if len(text) < 4: # 太短的忽略
                                continue
                                
                            if not href.startswith('http') and not href.startswith('/'):
                                continue
                                
                            # 简单的判断逻辑：如果链接文本包含我们要搜的关键词，或者是比较长的一段文字，很可能是职位
                            # 实际业务中这里可以更复杂，但作为通用爬虫，我们尽量宽松然后用 matcher 过滤
                            if extracted_count >= 50:
                                break
                            if not keyword or keyword.lower() in text.lower() or len(text) > 8:
                                job_link = href
                                if job_link.startswith('/'):
                                    parsed = urlparse(target_url)
                                    job_link = f"{parsed.scheme}://{parsed.netloc}{job_link}"
                                    
                                job = {
                                    'title': text[:50] + ("..." if len(text) > 50 else ""),
                                    'company': parsed_domain,
                                    'salary': '面议',
                                    'city': '未知',
                                    'url': job_link,
                                    'description': text, # 由于只抓取列表页，描述暂时用标题替代
                                    'publish_time': time.strftime('%Y-%m-%d'),
                                    'source_site': parsed_domain
                                }
                                # 简单去重
                                if not any(j['url'] == job['url'] for j in jobs):
                                    jobs.append(job)
                                extracted_count += 1
                                    
                        label = f"关键词 '{keyword}'" if keyword else "当前页面"
                        logger.info(f"{label}提取到 {extracted_count} 个可能职位")
                        
                    except Exception as e:
                        logger.error(f"处理关键词 '{keyword}' 时出错: {e}")
                        
                browser.close()
                
            logger.info(f"从 {target_url} 汇总提取到 {len(jobs)} 个匹配职位")
            
        except Exception as e:
            logger.error(f"通用爬虫(Playwright)运行失败: {e}")
            
        return jobs


class ZhipinScraper(JobScraper):
    """BOSS直聘真实爬虫"""

    def scrape_jobs(self, company: str, keywords: List[str], cities: List[str] = None) -> List[Dict]:
        jobs = []

        for keyword in keywords:
            for city in cities if cities else ['深圳', '北京', '上海', '杭州']:
                try:
                    # 构建搜索URL
                    base_url = "https://www.zhipin.com/web/geek/job"
                    city_map = {
                        '深圳': '101280600',
                        '北京': '101010100',
                        '上海': '101020100',
                        '杭州': '101210100',
                        '广州': '101280100',
                        '成都': '101270100',
                    }

                    city_code = city_map.get(city, '101280600')
                    query = quote(keyword)
                    company_query = quote(company)

                    url = f"{base_url}?query={query}&city={city_code}&page=1"

                    logger.info(f"正在抓取BOSS直聘: {company} - {keyword} - {city}")
                    logger.info(f"URL: {url}")

                    # 发送请求
                    response = self.session.get(url, timeout=15)

                    if response.status_code != 200:
                        logger.error(f"请求失败，状态码: {response.status_code}")
                        continue

                    # 解析HTML
                    soup = BeautifulSoup(response.text, 'html.parser')

                    # BOSS直聘的职位在 .job-card-wrapper 类中
                    job_cards = soup.find_all('div', class_='job-card-wrapper')

                    if not job_cards:
                        # 尝试其他可能的类名
                        job_cards = soup.find_all('li', class_='job-card-wrapper')

                    if not job_cards:
                        logger.warning(f"未找到职位信息，可能被反爬虫拦截")
                        self._random_delay(5, 10)
                        continue

                    # 提取职位信息
                    for card in job_cards[:5]:  # 每页最多取5个
                        try:
                            job = self._parse_zhipin_job(card, company, city)
                            if job:
                                jobs.append(job)
                        except Exception as e:
                            logger.error(f"解析单个职位失败: {e}")
                            continue

                    logger.info(f"从BOSS直聘获取到 {len(jobs)} 个职位（{company}-{keyword}-{city}）")

                    # 随机延迟
                    self._random_delay()

                except Exception as e:
                    logger.error(f"抓取BOSS直聘失败: {e}")
                    continue

        return jobs

    def _parse_zhipin_job(self, card, company: str, city: str) -> Dict:
        """解析BOSS直聘的单个职位"""
        try:
            # 职位标题
            title_elem = card.find('span', class_='job-name')
            if not title_elem:
                title_elem = card.find('a', class_='job-title')
            title = title_elem.text.strip() if title_elem else '未知职位'

            # 薪资
            salary_elem = card.find('span', class_='salary')
            salary = salary_elem.text.strip() if salary_elem else '面议'

            # 链接
            link_elem = card.find('a')
            if link_elem and link_elem.get('href'):
                job_url = urljoin('https://www.zhipin.com', link_elem['href'])
            else:
                job_url = 'https://www.zhipin.com'

            # 公司名（使用传入的公司名，因为卡片中的可能不准确）
            # 公司名称
            company_elem = card.find('span', class_='company-name')
            if company_elem:
                card_company = company_elem.text.strip()
            else:
                card_company = company

            # 发布时间
            time_elem = card.find('span', class_='job-info-publishtime')
            publish_time = time_elem.text.strip() if time_elem else time.strftime('%Y-%m-%d')

            # 标签（技能等）
            tags = []
            tag_elems = card.find_all('span', class_='tag-item')
            for tag_elem in tag_elems:
                tags.append(tag_elem.text.strip())
            description = '、'.join(tags) if tags else '职位详情请点击链接查看'

            return {
                'title': title,
                'company': card_company,
                'salary': salary,
                'city': city,
                'url': job_url,
                'description': description,
                'publish_time': publish_time,
                'source_site': 'BOSS直聘'
            }

        except Exception as e:
            logger.error(f"解析BOSS直聘职位失败: {e}")
            return None


class LagouScraper(JobScraper):
    """拉勾网真实爬虫"""

    def scrape_jobs(self, company: str, keywords: List[str], cities: List[str] = None) -> List[Dict]:
        jobs = []

        for keyword in keywords:
            for city in cities if cities else ['北京', '上海', '深圳']:
                try:
                    # 拉勾网使用POST请求
                    url = "https://www.lagou.com/jobs/positionAjax.json"

                    # 城市代码
                    city_map = {
                        '北京': '北京',
                        '上海': '上海',
                        '深圳': '深圳',
                        '杭州': '杭州',
                    }

                    # 构建请求参数
                    params = {
                        'first': 'true',
                        'pn': 1,
                        'kd': keyword,
                        'city': city_map.get(city, '深圳'),
                    }

                    # 添加公司过滤
                    if company:
                        params['kp'] = company

                    logger.info(f"正在抓取拉勾网: {company} - {keyword} - {city}")

                    # 发送请求
                    response = self.session.post(url, params=params, timeout=15)

                    if response.status_code != 200:
                        logger.error(f"请求失败，状态码: {response.status_code}")
                        self._random_delay()
                        continue

                    # 拉勾网返回JSON
                    try:
                        data = response.json()

                        if data.get('success'):
                            position_data = data.get('content', {}).get('positionResult', {}).get('result', [])

                            for pos in position_data[:5]:
                                job = self._parse_lagou_job(pos, company, city)
                                if job:
                                    jobs.append(job)

                            logger.info(f"从拉勾网获取到 {len(jobs)} 个职位（{company}-{keyword}-{city}）")
                        else:
                            logger.warning(f"拉勾网返回失败: {data.get('msg')}")

                    except ValueError:
                        logger.error("解析JSON失败")

                    # 随机延迟
                    self._random_delay()

                except Exception as e:
                    logger.error(f"抓取拉勾网失败: {e}")
                    continue

        return jobs

    def _parse_lagou_job(self, pos, company: str, city: str) -> Dict:
        """解析拉勾网的单个职位"""
        try:
            # 提取职位信息
            return {
                'title': pos.get('positionName', '未知职位'),
                'company': pos.get('companyName', company),
                'salary': pos.get('salary', '面议'),
                'city': pos.get('city', city),
                'url': f"https://www.lagou.com/jobs/{pos.get('positionId', '')}.html",
                'description': pos.get('positionAdvantage', '')[:100],
                'publish_time': pos.get('createTime', time.strftime('%Y-%m-%d')),
                'source_site': '拉勾网'
            }
        except Exception as e:
            logger.error(f"解析拉勾网职位失败: {e}")
            return None


class LiepinScraper(JobScraper):
    """猎聘网真实爬虫"""

    def scrape_jobs(self, company: str, keywords: List[str], cities: List[str] = None) -> List[Dict]:
        jobs = []

        for keyword in keywords:
            for city in cities if cities else ['上海', '北京', '深圳']:
                try:
                    # 猎聘网搜索URL
                    base_url = "https://www.liepin.com/zhaopin/"

                    city_map = {
                        '上海': '020',
                        '北京': '010',
                        '深圳': '050020',
                    }

                    city_code = city_map.get(city, '020')
                    query = quote(keyword)

                    url = f"{base_url}?key={query}&city={city_code}"

                    logger.info(f"正在抓取猎聘网: {company} - {keyword} - {city}")
                    logger.info(f"URL: {url}")

                    # 发送请求
                    response = self.session.get(url, timeout=15)

                    if response.status_code != 200:
                        logger.error(f"请求失败，状态码: {response.status_code}")
                        self._random_delay()
                        continue

                    # 解析HTML
                    soup = BeautifulSoup(response.text, 'html.parser')

                    # 猎聘网的职位在 .job-info 类中
                    job_cards = soup.find_all('div', class_='job-info')

                    if not job_cards:
                        logger.warning(f"未找到职位信息")
                        self._random_delay(5, 10)
                        continue

                    # 提取职位信息
                    for card in job_cards[:5]:
                        try:
                            job = self._parse_liepin_job(card, company, city)
                            if job:
                                jobs.append(job)
                        except Exception as e:
                            logger.error(f"解析单个职位失败: {e}")
                            continue

                    logger.info(f"从猎聘网获取到 {len(jobs)} 个职位（{company}-{keyword}-{city}）")

                    # 随机延迟
                    self._random_delay()

                except Exception as e:
                    logger.error(f"抓取猎聘网失败: {e}")
                    continue

        return jobs

    def _parse_liepin_job(self, card, company: str, city: str) -> Dict:
        """解析猎聘网的单个职位"""
        try:
            # 职位标题
            title_elem = card.find('a', class_='job-title')
            title = title_elem.text.strip() if title_elem else '未知职位'

            # 薪资
            salary_elem = card.find('span', class_='text-warning')
            salary = salary_elem.text.strip() if salary_elem else '面议'

            # 链接
            if title_elem and title_elem.get('href'):
                job_url = urljoin('https://www.liepin.com', title_elem['href'])
            else:
                job_url = 'https://www.liepin.com'

            # 公司名称
            company_elem = card.find('a', class_='company-name')
            if company_elem:
                card_company = company_elem.text.strip()
            else:
                card_company = company

            # 发布时间
            time_elem = card.find('time')
            publish_time = time_elem.text.strip() if time_elem else time.strftime('%Y-%m-%d')

            # 职位描述（简化）
            description = '职位详情请点击链接查看'

            return {
                'title': title,
                'company': card_company,
                'salary': salary,
                'city': city,
                'url': job_url,
                'description': description,
                'publish_time': publish_time,
                'source_site': '猎聘网'
            }

        except Exception as e:
            logger.error(f"解析猎聘网职位失败: {e}")
            return None


def get_scraper(site_url: str, use_real: bool = True) -> JobScraper:
    """
    根据网站URL获取对应的爬虫实例
    现在所有网址统一使用 UniversalScraperWithPlaywright 进行抓取
    """
    return UniversalScraperWithPlaywright()
