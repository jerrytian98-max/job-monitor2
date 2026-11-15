"""Official recruitment site adapters built on top of rendered browser pages.

The supported career sites are mostly single-page applications.  Instead of
guessing from visible links only, this module listens to the JSON responses
used by the page itself and maps each supported schema to the application's
common job shape.  Rendered job cards remain as a fallback for encrypted Moka
responses and sites that do not expose a stable JSON schema.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from job_identity import canonicalize_job_url
from playwright.sync_api import BrowserContext, Page, sync_playwright


logger = logging.getLogger(__name__)

UNKNOWN_CITY = "未知"
NEGOTIABLE_SALARY = "面议"
MAX_JOBS_PER_SEARCH = 30

SEARCH_QUERY_KEYS = {
    "q", "query", "search", "searchtext", "searchword", "key",
    "keyword", "keywords", "jobname", "jobtitle", "positionname",
    "positiontitle",
}


class OfficialSiteScraper:
    """Scrape official recruitment SPAs and preserve their job details."""

    def __init__(self):
        self.session = requests.Session()
        self._last_search_blocked = False
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    def scrape_jobs(
        self,
        target_url: str,
        keywords: List[str],
        cities: Optional[List[str]] = None,
        search_mode: str = "search",
    ) -> List[Dict]:
        target_url = (
            str(target_url or "")
            .strip()
            .replace("?\n", "?")
            .replace("\r", "")
            .replace("\n", "")
        )
        parsed_target = urlparse(target_url)
        if parsed_target.scheme not in ("http", "https") or not parsed_target.netloc:
            logger.error("无效的招聘网站地址: %s", target_url)
            return []

        fixed_url_mode = str(search_mode or "").strip().lower() == "fixed"
        search_plan = self._search_plan(
            target_url,
            keywords,
            fixed_url_mode=fixed_url_mode,
        )

        # Meituan's search box is rendered inconsistently in automated browser
        # sessions, but the JSON endpoint behind it is public and stable.  Use
        # that endpoint for keyword searches so the result count and pagination
        # are identical to /web/position?keyword=....
        if parsed_target.netloc.lower() == "zhaopin.meituan.com":
            meituan_jobs: List[Dict] = []
            api_search_complete = True
            for _, effective_keyword in search_plan:
                if not effective_keyword:
                    api_search_complete = False
                    break
                term_jobs = self._scrape_meituan_keyword(
                    target_url,
                    effective_keyword,
                )
                if term_jobs is None:
                    api_search_complete = False
                    break
                term_jobs = self._limit_search_results(term_jobs)
                meituan_jobs = self._merge_jobs(meituan_jobs, term_jobs)

            if api_search_complete:
                jobs = self._finalize_jobs(meituan_jobs, parsed_target.netloc)
                logger.info(
                    "从 %s 提取到 %s 个带明细的职位",
                    target_url,
                    len(jobs),
                )
                return jobs

        alibaba_hosts = {
            "careers.aliyun.com",
            "talent.quark.cn",
            "talent.taotian.com",
            "talent.ele.me",
        }
        if parsed_target.netloc.lower() in alibaba_hosts:
            alibaba_jobs: List[Dict] = []
            api_search_complete = True
            for _, effective_keyword in search_plan:
                if not effective_keyword:
                    api_search_complete = False
                    break
                term_jobs = self._scrape_alibaba_keyword(
                    target_url,
                    effective_keyword,
                )
                if term_jobs is None:
                    api_search_complete = False
                    break
                term_jobs = self._limit_search_results(term_jobs)
                alibaba_jobs = self._merge_jobs(alibaba_jobs, term_jobs)

            if api_search_complete:
                jobs = self._finalize_jobs(alibaba_jobs, parsed_target.netloc)
                logger.info(
                    "从 %s 提取到 %s 个带明细的职位",
                    target_url,
                    len(jobs),
                )
                return jobs

        # Baidu's desktop page currently replaces itself with about:blank in
        # automated Chromium sessions. The search box is backed by a public
        # endpoint that also returns the complete responsibilities and
        # requirements, so use it directly instead of treating the redirect as
        # an empty search result.
        if parsed_target.netloc.lower() == "talent.baidu.com" and not fixed_url_mode:
            baidu_jobs: List[Dict] = []
            api_search_complete = True
            for _, effective_keyword in search_plan:
                term_jobs = self._scrape_baidu_keyword(
                    target_url,
                    effective_keyword,
                )
                if term_jobs is None:
                    api_search_complete = False
                    break
                term_jobs = self._limit_search_results(term_jobs)
                baidu_jobs = self._merge_jobs(baidu_jobs, term_jobs)

            if api_search_complete:
                jobs = self._finalize_jobs(baidu_jobs, parsed_target.netloc)
                logger.info(
                    "从 %s 提取到 %s 个带明细的职位",
                    target_url,
                    len(jobs),
                )
                return jobs

        jobs: List[Dict] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1440, "height": 1000},
                    user_agent=self.session.headers["User-Agent"],
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                for term, effective_keyword in search_plan:
                    attempts = (
                        5
                        if "pddglobalhr.com" in parsed_target.netloc.lower()
                        else 1
                    )
                    term_jobs = []
                    for _ in range(attempts):
                        term_jobs = self._scrape_one_term(
                            context,
                            target_url,
                            term,
                            effective_keyword,
                            fixed_url_mode=fixed_url_mode,
                        )
                        if term_jobs or not self._last_search_blocked:
                            break
                    term_jobs = self._limit_search_results(term_jobs)
                    jobs = self._merge_jobs(jobs, term_jobs)

                self._enrich_tencent_jobs(jobs, target_url)
                self._enrich_pdd_jobs(context, jobs)
                context.close()
                browser.close()
        except Exception as error:
            logger.error("浏览器抓取失败 [%s]: %s", target_url, error)

        jobs = self._finalize_jobs(jobs, parsed_target.netloc)
        logger.info("从 %s 提取到 %s 个带明细的职位", target_url, len(jobs))
        return jobs

    def _scrape_one_term(
        self,
        context: BrowserContext,
        target_url: str,
        term: str,
        effective_filter: str,
        fixed_url_mode: bool = False,
    ) -> List[Dict]:
        page = context.new_page()
        captured_jobs: List[Dict] = []
        navigation_url = "" if fixed_url_mode else self._keyword_navigation_url(
            target_url,
            term,
        )
        direct_keyword_navigation = bool(term and navigation_url)
        response_state = {
            "pdd_search_failed": False,
            "pdd_search_succeeded": False,
            "accept_results": not bool(term) or direct_keyword_navigation,
            "result_response_count": 0,
        }
        pdd_keyword_search_blocked = False

        def capture_response(response):
            content_type = str(response.headers.get("content-type") or "").lower()
            likely_data_endpoint = bool(re.search(
                r"/(?:api|ajax|graphql|job|jobs|position|positions|post|recruit|career)(?:/|\?|$)",
                str(response.url or ""),
                flags=re.IGNORECASE,
            ))
            if "json" not in content_type and not likely_data_endpoint:
                return
            try:
                payload = response.json()
                if not response_state["accept_results"]:
                    return
                if (
                    "pddglobalhr.com/api/recruit/position/list" in response.url
                    and isinstance(payload, dict)
                ):
                    if payload.get("success") is True:
                        response_state["pdd_search_succeeded"] = True
                    elif payload.get("success") is False:
                        response_state["pdd_search_failed"] = True
                new_jobs = self._jobs_from_payload(payload, response.url, target_url)
                if new_jobs:
                    if term and not self._response_matches_keyword(response, term):
                        return
                    response_state["result_response_count"] += 1
                    captured_jobs[:] = self._merge_jobs(captured_jobs, new_jobs)
            except Exception:
                return

        page.on("response", capture_response)
        try:
            logger.info(
                "打开招聘网站: %s%s",
                target_url,
                f"（关键词: {term}）" if term else "",
            )
            page.goto(
                (
                    target_url
                    if fixed_url_mode
                    else navigation_url or self._starting_url(target_url)
                ),
                timeout=60_000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(3_000)

            if term and not direct_keyword_navigation:
                # Initial API responses contain the site's default newest jobs.
                # Once a keyword is supplied, only retain responses generated
                # by that search to avoid mixing unrelated default results in.
                captured_jobs.clear()
                response_state["pdd_search_failed"] = False
                response_state["pdd_search_succeeded"] = False
                response_state["accept_results"] = True
                self._submit_search(page, term)
                page.wait_for_timeout(3_000)

            self._settle_page(page)
            pdd_keyword_search_blocked = (
                bool(term)
                and "pddglobalhr.com" in urlparse(target_url).netloc.lower()
                and response_state["pdd_search_failed"]
                and not response_state["pdd_search_succeeded"]
            )
            if pdd_keyword_search_blocked:
                captured_jobs.clear()
                logger.warning(
                    "拼多多招聘站拒绝了本次自动化关键词查询，"
                    "已跳过页面上的默认热招岗位，避免产生错误结果"
                )
            else:
                captured_jobs = self._merge_jobs(
                    captured_jobs,
                    self._jobs_from_rendered_page(page, target_url, effective_filter),
                )

            no_progress_pages = 0
            while len(captured_jobs) < MAX_JOBS_PER_SEARCH:
                if pdd_keyword_search_blocked:
                    break
                previous_count = len(captured_jobs)
                previous_response_count = response_state["result_response_count"]
                previous_url = page.url
                if not self._go_to_next_page(page):
                    break
                # Wait for the next result request instead of assuming every
                # site finishes within a fixed delay.
                for _ in range(40):
                    page.wait_for_timeout(250)
                    if (
                        response_state["result_response_count"]
                        > previous_response_count
                    ):
                        break
                    if page.url != previous_url:
                        page.wait_for_timeout(500)
                        break
                self._settle_page(page)
                captured_jobs = self._merge_jobs(
                    captured_jobs,
                    self._jobs_from_rendered_page(page, target_url, effective_filter),
                )
                if len(captured_jobs) == previous_count:
                    no_progress_pages += 1
                    if no_progress_pages >= 2:
                        break
                else:
                    no_progress_pages = 0
        except Exception as error:
            logger.warning("处理招聘页面失败 [%s]: %s", target_url, error)
        finally:
            page.close()
        self._last_search_blocked = pdd_keyword_search_blocked
        result = self._limit_search_results(captured_jobs)
        for job in result:
            job["_search_keyword"] = effective_filter
            if fixed_url_mode:
                job["_fixed_url_mode"] = True
        return result

    @staticmethod
    def _starting_url(target_url: str) -> str:
        """Open the site's actual search landing page when a saved URL is a homepage."""
        parsed = urlparse(target_url)
        if (
            parsed.netloc.lower() == "talent.taotian.com"
            and parsed.path.rstrip("/") == ""
        ):
            return f"{parsed.scheme}://{parsed.netloc}/off-campus/home?lang=zh"
        if (
            parsed.netloc.lower() == "jobs.bytedance.com"
            and "/position/list" in parsed.path
        ):
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query["limit"] = str(MAX_JOBS_PER_SEARCH)
            query["current"] = "1"
            return urlunparse(parsed._replace(query=urlencode(query)))
        return target_url

    @staticmethod
    def _keyword_navigation_url(target_url: str, term: str) -> str:
        """Build stable keyword-result URLs for sites that expose them."""
        if not term:
            return ""
        parsed = urlparse(target_url)
        host = parsed.netloc.lower()
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))

        if host == "jobs.bytedance.com" and "/position/list" in parsed.path:
            query.update({
                "keywords": term,
                "current": "1",
                "limit": str(MAX_JOBS_PER_SEARCH),
            })
            return urlunparse(parsed._replace(query=urlencode(query)))
        if host == "job.xiaohongshu.com" and "/social/position" in parsed.path:
            query["positionName"] = term
            return urlunparse(parsed._replace(query=urlencode(query)))
        if (
            host == "jobs.bilibili.com"
            and parsed.path.rstrip("/") == "/social/positions"
        ):
            # Bilibili persists the search term as ``name`` on the social
            # recruitment list URL, so opening it directly avoids default
            # job results being treated as a keyword search.
            query["name"] = term
            return urlunparse(parsed._replace(query=urlencode(query)))
        if host == "careers.tencent.com":
            query = {"keyword": term}
            return urlunparse(
                parsed._replace(path="/search.html", query=urlencode(query))
            )

        # Unknown recruitment systems often expose the keyword in an ordinary
        # query string (or inside an SPA hash route).  Reuse that contract when
        # it already exists instead of guessing a brand-specific endpoint.
        for key in list(query):
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized_key in SEARCH_QUERY_KEYS:
                query[key] = term
                return urlunparse(parsed._replace(query=urlencode(query)))

        fragment_path, separator, fragment_query = parsed.fragment.partition("?")
        if separator:
            hash_query = dict(parse_qsl(fragment_query, keep_blank_values=True))
            for key in list(hash_query):
                normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
                if normalized_key in SEARCH_QUERY_KEYS:
                    hash_query[key] = term
                    fragment = f"{fragment_path}?{urlencode(hash_query)}"
                    return urlunparse(parsed._replace(fragment=fragment))
        return ""

    @staticmethod
    def _clean_terms(keywords: Optional[Iterable[str]]) -> List[str]:
        result = []
        for keyword in keywords or []:
            keyword = str(keyword or "").strip()
            if keyword and keyword not in result:
                result.append(keyword)
        return result

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _limit_search_results(jobs: Optional[Iterable[Dict]]) -> List[Dict]:
        """Keep the website's original order and return at most 30 jobs."""
        return list(jobs or [])[:MAX_JOBS_PER_SEARCH]

    @classmethod
    def _search_plan(
        cls,
        target_url: str,
        keywords: Optional[Iterable[str]],
        fixed_url_mode: bool = False,
    ) -> List[tuple]:
        """Return (input term, result keyword) pairs for one target URL."""
        if fixed_url_mode:
            # The URL itself is the user's complete search/filter condition.
            # Do not infer or submit any global keyword in this mode.
            return [("", "")]
        url_keyword = cls._keyword_from_url(target_url)
        if url_keyword:
            # Backward compatibility for older saved search-result URLs.
            return [("", url_keyword)]
        terms = cls._clean_terms(keywords)
        if not terms:
            return [("", "")]
        return [(term, term) for term in terms]

    @staticmethod
    def _keyword_from_url(url: str) -> str:
        parsed = urlparse(url)
        query_sources = [parsed.query]
        if "?" in parsed.fragment:
            query_sources.append(parsed.fragment.split("?", 1)[1])
        for query_source in query_sources:
            for key, value in parse_qsl(query_source, keep_blank_values=True):
                normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
                if normalized_key in SEARCH_QUERY_KEYS and str(value).strip():
                    return str(value).strip()

        decoded = url
        for _ in range(3):
            next_value = unquote(decoded)
            if next_value == decoded:
                break
            decoded = next_value

        patterns = (
            r"(?:keywords?|positionName)\s*=\s*([^&#]+)",
            r"[\"']?urlSearch[\"']?\s*:\s*[\"']([^\"'}]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, decoded, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" \"'")
        return ""

    @staticmethod
    def _score_search_field_metadata(metadata: Dict[str, Any]) -> int:
        """Rank a text control by its likelihood of being a job search box."""
        field_type = str(metadata.get("type") or "text").casefold()
        if field_type in {
            "password", "email", "tel", "number", "date", "datetime-local",
            "file", "checkbox", "radio", "hidden", "submit", "button",
        }:
            return -1000
        if metadata.get("disabled") or metadata.get("readonly"):
            return -1000

        primary = " ".join(str(metadata.get(key) or "") for key in (
            "placeholder", "name", "id", "class_name", "aria_label", "role", "label",
        )).casefold()
        context = str(metadata.get("context") or "").casefold()[:500]
        positive_job = (
            "职位", "岗位", "职务", "招聘", "job", "position", "career",
            "vacancy", "opening",
        )
        positive_search = (
            "搜索", "查询", "检索", "关键词", "关键字", "search", "keyword",
            "query", "find",
        )
        negative = (
            "邮箱", "邮件", "手机号", "电话", "密码", "验证码", "姓名",
            "登录", "注册", "email", "phone", "mobile", "password", "login",
            "城市", "地点", "地区", "location", "city",
        )

        if any(word in primary for word in negative):
            return -500

        score = 0
        if field_type == "search":
            score += 25
        if str(metadata.get("role") or "").casefold() == "searchbox":
            score += 30
        if any(word in primary for word in positive_job):
            score += 35
        if any(word in primary for word in positive_search):
            score += 30
        if any(word in context for word in positive_job):
            score += 8
        if any(word in context for word in positive_search):
            score += 6
        if (
            any(word in context for word in positive_job)
            and any(word in context for word in positive_search)
        ):
            score += 12
        if str(metadata.get("name") or "").casefold() in {"q", "query", "keyword"}:
            score += 18
        return score

    @staticmethod
    def _score_search_action_metadata(metadata: Dict[str, Any]) -> int:
        text = " ".join(str(metadata.get(key) or "") for key in (
            "text", "aria_label", "title", "class_name", "id", "html",
        )).casefold()
        if any(word in text for word in (
            "重置", "清空", "取消", "登录", "注册", "reset", "clear", "cancel",
            "login", "sign in",
        )):
            return -100
        score = 10 if str(metadata.get("type") or "").casefold() == "submit" else 0
        if any(word in text for word in (
            "搜索", "查询", "检索", "确定", "search", "query", "find", "submit",
        )):
            score += 35
        if any(word in text for word in ("search", "query", "keyword")):
            score += 15
        return score

    @staticmethod
    def _page_surfaces(page: Page) -> List[Any]:
        try:
            frames = list(page.frames)
            return frames or [page]
        except Exception:
            return [page]

    @staticmethod
    def _surface_signature(surface: Any) -> str:
        try:
            return str(surface.evaluate(
                "() => `${location.href}\\n${(document.body && document.body.innerText || '').slice(0, 50000)}`"
            ))
        except Exception:
            return ""

    @classmethod
    def _ranked_search_fields(cls, surface: Any) -> List[Any]:
        selector = (
            "input, textarea, [contenteditable='true'], "
            "[role='searchbox'], [role='textbox']"
        )
        try:
            fields = surface.locator(selector)
            metadata = fields.evaluate_all(
                """
                elements => elements.map((element, index) => {
                  const container = element.closest(
                    'form, [role="search"], [class*="search" i], '
                    + '[class*="query" i], section, div'
                  );
                  return {
                    index,
                    type: element.getAttribute('type') || '',
                    placeholder: element.getAttribute('placeholder') || '',
                    name: element.getAttribute('name') || '',
                    id: element.id || '',
                    class_name: typeof element.className === 'string' ? element.className : '',
                    aria_label: element.getAttribute('aria-label') || '',
                    role: element.getAttribute('role') || '',
                    label: Array.from(element.labels || [])
                      .map(label => label.innerText || label.textContent || '')
                      .join(' '),
                    disabled: Boolean(element.disabled),
                    readonly: Boolean(element.readOnly),
                    context: (container && (container.innerText || container.textContent) || '').slice(0, 500)
                  };
                })
                """
            )
        except Exception:
            return []

        ranked = []
        for item in metadata:
            score = cls._score_search_field_metadata(item)
            if score >= 20:
                ranked.append((score, int(item.get("index", 0))))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [fields.nth(index) for _, index in ranked[:8]]

    @classmethod
    def _find_search_action(cls, field: Any) -> Optional[Any]:
        selectors = (
            "xpath=ancestor::form[1]//*[self::button or @role='button']",
            "xpath=ancestor::*[contains(translate(@class, 'SEARCHQUERY', 'searchquery'), 'search') or contains(translate(@class, 'SEARCHQUERY', 'searchquery'), 'query')][1]//*[self::button or @role='button']",
            "xpath=following-sibling::*[self::button or @role='button'][1]",
            "xpath=following::*[self::button or @role='button'][1]",
        )
        ranked = []
        for selector_index, selector in enumerate(selectors):
            try:
                actions = field.locator(selector)
                for index in range(min(actions.count(), 8)):
                    action = actions.nth(index)
                    if not action.is_visible() or not action.is_enabled():
                        continue
                    metadata = action.evaluate(
                        """
                        element => ({
                          text: (element.innerText || element.textContent || '').trim(),
                          aria_label: element.getAttribute('aria-label') || '',
                          title: element.getAttribute('title') || '',
                          class_name: typeof element.className === 'string' ? element.className : '',
                          id: element.id || '',
                          type: element.getAttribute('type') || '',
                          html: (element.innerHTML || '').slice(0, 500)
                        })
                        """
                    )
                    score = cls._score_search_action_metadata(metadata)
                    if score == 0 and selector_index >= 2:
                        # An adjacent icon-only action is a common search UI.
                        score = 3
                    if score > 0:
                        ranked.append((score, action))
            except Exception:
                continue
        if not ranked:
            return None
        ranked.sort(key=lambda item: -item[0])
        return ranked[0][1]

    @classmethod
    def _submit_search(cls, page: Page, term: str) -> bool:
        if "zhaopin.meituan.com" in urlparse(page.url).netloc.lower():
            try:
                fields = page.locator(
                    "input[placeholder='输入关键词搜索岗位']"
                )
                for index in range(fields.count()):
                    field = fields.nth(index)
                    if not field.is_visible() or not field.is_enabled():
                        continue
                    field.fill(term)
                    search = field.locator(
                        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' zp_search ')][1]"
                    )
                    button = search.locator("button.zp_search_btn")
                    if button.count() and button.first.is_visible():
                        button.first.click()
                    else:
                        field.press("Enter")
                    logger.info("已在页面搜索框中提交关键词: %s", term)
                    return True
            except Exception:
                pass

        if "pddglobalhr.com" in urlparse(page.url).netloc.lower():
            try:
                field = page.locator("input[placeholder='搜索职位名称']")
                button = page.locator("button.rocket-input-search-button")
                if (
                    field.count() == 1
                    and button.count() == 1
                    and field.is_visible()
                    and field.is_enabled()
                    and button.is_visible()
                    and button.is_enabled()
                ):
                    field.fill(term)
                    button.click()
                    logger.info("已在页面搜索框中提交关键词: %s", term)
                    return True
            except Exception:
                pass

        # Generic fallback: rank controls by their semantic attributes and the
        # text around them, including controls rendered inside iframes.
        for surface in cls._page_surfaces(page):
            for field in cls._ranked_search_fields(surface):
                try:
                    if not field.is_visible() or not field.is_enabled():
                        continue
                    field.fill(term)
                    before = cls._surface_signature(surface)
                    field.press("Enter")
                    page.wait_for_timeout(450)
                    if cls._surface_signature(surface) == before:
                        action = cls._find_search_action(field)
                        if action is not None:
                            action.click(timeout=5_000)
                    logger.info("已在页面搜索框中提交关键词: %s", term)
                    return True
                except Exception:
                    continue
        logger.warning("页面没有可用的职位搜索框，将分析当前结果页")
        return False

    def _scrape_meituan_keyword(
        self,
        target_url: str,
        keyword: str,
    ) -> Optional[List[Dict]]:
        """Return up to 30 results from the official Meituan keyword API.

        ``None`` means the API request failed and allows the caller to fall
        back to browser scraping.  An empty list is a valid zero-result search.
        """
        api_url = "https://zhaopin.meituan.com/api/official/job/getJobList"
        page_size = MAX_JOBS_PER_SEARCH
        page_no = 1
        jobs: List[Dict] = []
        referer = (
            "https://zhaopin.meituan.com/web/position?"
            + urlencode({"keyword": keyword})
        )

        try:
            while True:
                payload = {
                    "page": {"pageNo": page_no, "pageSize": page_size},
                    "jobShareType": "1",
                    "keywords": keyword,
                    "cityList": [],
                    "department": [],
                    "jfJgList": [],
                    "jobType": [],
                    "typeCode": [],
                    "specialCode": [],
                }
                response = self.session.post(
                    api_url,
                    json=payload,
                    headers={"Referer": referer},
                    timeout=20,
                )
                response.raise_for_status()
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                records = data.get("list") if isinstance(data, dict) else None
                if not isinstance(records, list):
                    raise ValueError("美团职位接口没有返回职位列表")

                page_jobs = [
                    job
                    for record in records
                    if isinstance(record, dict)
                    for job in [self._from_meituan(record, target_url)]
                    if job
                ]
                for job in page_jobs:
                    job["_search_keyword"] = keyword
                jobs = self._merge_jobs(jobs, page_jobs)
                if len(jobs) >= MAX_JOBS_PER_SEARCH:
                    jobs = self._limit_search_results(jobs)
                    break

                page_info = data.get("page") or {}
                total_pages = self._safe_int(page_info.get("totalPage"))
                total_count = self._safe_int(page_info.get("totalCount"))
                if total_pages:
                    if page_no >= total_pages:
                        break
                elif total_count:
                    if page_no * page_size >= total_count:
                        break
                elif len(records) < page_size:
                    break
                page_no += 1

            logger.info(
                "美团关键词[%s]返回%s个职位",
                keyword,
                len(jobs),
            )
            return jobs
        except Exception as error:
            logger.warning("美团关键词接口抓取失败 [%s]: %s", keyword, error)
            return None

    def _scrape_alibaba_keyword(
        self,
        target_url: str,
        keyword: str,
    ) -> Optional[List[Dict]]:
        """Search Alibaba-family recruitment sites through their public API."""
        parsed = urlparse(target_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        referer = self._starting_url(target_url)
        page_no = 1
        page_size = MAX_JOBS_PER_SEARCH
        jobs: List[Dict] = []

        try:
            landing = self.session.get(referer, timeout=20)
            landing.raise_for_status()
            csrf_token = self.session.cookies.get("XSRF-TOKEN")
            if not csrf_token:
                raise ValueError("招聘网站没有返回搜索令牌")

            while True:
                payload = {
                    "channel": "group_official_site",
                    "language": "zh",
                    "batchId": "",
                    "categories": "",
                    "deptCodes": [],
                    "key": keyword,
                    "pageIndex": page_no,
                    "pageSize": page_size,
                    "regions": "",
                    "subCategories": "",
                    "shareType": "",
                    "shareId": "",
                    "myReferralShareCode": "",
                }
                response = self.session.post(
                    f"{base_url}/position/search",
                    params={"_csrf": csrf_token},
                    json=payload,
                    headers={
                        "Referer": referer,
                        "X-XSRF-TOKEN": csrf_token,
                    },
                    timeout=20,
                )
                response.raise_for_status()
                body = response.json()
                content = body.get("content") if isinstance(body, dict) else None
                records = content.get("datas") if isinstance(content, dict) else None
                if not isinstance(records, list):
                    raise ValueError("招聘网站接口没有返回职位列表")

                page_jobs = [
                    job
                    for record in records
                    if isinstance(record, dict)
                    for job in [self._from_alibaba(record, target_url)]
                    if job
                ]
                for job in page_jobs:
                    job["_search_keyword"] = keyword
                jobs = self._merge_jobs(jobs, page_jobs)
                if len(jobs) >= MAX_JOBS_PER_SEARCH:
                    jobs = self._limit_search_results(jobs)
                    break

                total_count = self._safe_int(content.get("totalCount"))
                if not records or not total_count or len(jobs) >= total_count:
                    break
                page_no += 1

            logger.info(
                "%s关键词[%s]返回%s个职位",
                parsed.netloc,
                keyword,
                len(jobs),
            )
            return jobs
        except Exception as error:
            logger.warning(
                "%s关键词接口抓取失败 [%s]: %s",
                parsed.netloc,
                keyword,
                error,
            )
            return None

    def _scrape_baidu_keyword(
        self,
        target_url: str,
        keyword: str,
    ) -> Optional[List[Dict]]:
        """Search Baidu social recruitment through its public list endpoint."""
        api_url = "https://talent.baidu.com/httservice/getPostListNew"
        page_no = 1
        page_size = 10  # The endpoint rejects larger page sizes.
        jobs: List[Dict] = []

        try:
            while True:
                response = self.session.post(
                    api_url,
                    data={
                        "recruitType": "SOCIAL",
                        "workPlace": [],
                        "pageSize": page_size,
                        "keyWord": keyword,
                        "postType": [],
                        "curPage": page_no,
                        "projectType": "",
                    },
                    headers={
                        "Origin": "https://talent.baidu.com",
                        "Referer": target_url,
                    },
                    timeout=20,
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("status") != "ok":
                    raise ValueError(
                        self._text(body.get("message"))
                        if isinstance(body, dict)
                        else "百度招聘接口返回格式无效"
                    )
                data = body.get("data") or {}
                records = data.get("list")
                if not isinstance(records, list):
                    raise ValueError("百度招聘接口没有返回职位列表")

                page_jobs = [
                    job
                    for record in records
                    if isinstance(record, dict)
                    for job in [self._from_baidu(record, target_url)]
                    if job
                ]
                for job in page_jobs:
                    job["_search_keyword"] = keyword
                jobs = self._merge_jobs(jobs, page_jobs)
                if len(jobs) >= MAX_JOBS_PER_SEARCH:
                    jobs = self._limit_search_results(jobs)
                    break

                total = self._safe_int(data.get("total"))
                total_pages = self._safe_int(data.get("pages"))
                if total_pages:
                    if page_no >= total_pages:
                        break
                elif total:
                    if page_no * page_size >= total:
                        break
                elif len(records) < page_size:
                    break
                page_no += 1

            logger.info("百度关键词[%s]返回%s个职位", keyword, len(jobs))
            return jobs
        except Exception as error:
            logger.warning("百度关键词接口抓取失败 [%s]: %s", keyword, error)
            return None

    @staticmethod
    def _response_matches_keyword(response, keyword: str) -> bool:
        """Reject delayed default-list responses after a keyword search starts."""
        request = getattr(response, "request", None)
        request_url = str(getattr(request, "url", "") or response.url or "")
        post_data = str(getattr(request, "post_data", "") or "")
        haystack = f"{request_url}\n{post_data}"
        for _ in range(3):
            decoded = unquote(haystack)
            if decoded == haystack:
                break
            haystack = decoded
        haystack = haystack.casefold()
        return str(keyword or "").strip().casefold() in haystack

    @classmethod
    def _settle_page(cls, page: Page):
        for surface in cls._page_surfaces(page):
            try:
                surface.evaluate(
                    """
                    async () => {
                      for (let i = 0; i < 4; i += 1) {
                        window.scrollTo(0, document.body.scrollHeight);
                        await new Promise(resolve => setTimeout(resolve, 300));
                      }
                      window.scrollTo(0, 0);
                    }
                    """
                )
            except Exception:
                continue
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

    @classmethod
    def _go_to_next_page(cls, page: Page) -> bool:
        selectors = (
            "li.page-li.active + li.page-li",
            "button:has-text('下一页')",
            "[aria-label='下一页']",
            "[aria-label='Next']",
            "li[title='下一页']",
            "li[title='下一页'] button",
            ".ant-pagination-next button",
            ".el-pagination .btn-next",
            ".next-pagination-item.next-next",
            "button:has-text('Next')",
        )
        for surface in cls._page_surfaces(page):
            for selector in selectors:
                try:
                    candidates = surface.locator(selector)
                    for index in range(min(candidates.count(), 4)):
                        button = candidates.nth(index)
                        if not button.is_visible():
                            continue
                        class_name = str(button.get_attribute("class") or "").lower()
                        parent_class = str(
                            button.locator("..").get_attribute("class") or ""
                        ).lower()
                        if (
                            button.is_disabled()
                            or button.get_attribute("aria-disabled") == "true"
                            or "disabled" in class_name
                            or "disabled" in parent_class
                        ):
                            return False
                        button.click(timeout=5_000)
                        return True
                except Exception:
                    continue
        return False

    def _jobs_from_payload(
        self,
        payload: Any,
        response_url: str,
        target_url: str,
    ) -> List[Dict]:
        """Map the known official JSON response schemas to common job records."""
        if isinstance(payload, list):
            return self._jobs_from_generic_payload(payload, response_url, target_url)
        if not isinstance(payload, dict):
            return []

        # Tencent Careers: /tencentcareer/api/post/Query
        posts = self._dig(payload, "Data", "Posts")
        if isinstance(posts, list):
            return [
                job
                for record in posts
                if isinstance(record, dict)
                for job in [self._from_tencent(record, target_url)]
                if job
            ]

        # Alibaba recruitment platform used by Ele.me, Aliyun and Quark.
        alibaba_records = self._dig(payload, "content", "datas")
        if isinstance(alibaba_records, list):
            return [
                job
                for record in alibaba_records
                if isinstance(record, dict)
                for job in [self._from_alibaba(record, target_url)]
                if job
            ]

        # Kuaishou's official career site returns public jobs under
        # ``result.list`` rather than the ``data.list`` shape used by other
        # supported sites.  The result response is already scoped to the
        # submitted keyword by _response_matches_keyword.
        kuaishou_records = self._dig(payload, "result", "list")
        if (
            "zhaopin.kuaishou.cn" in urlparse(target_url).netloc.lower()
            and isinstance(kuaishou_records, list)
        ):
            return [
                job
                for record in kuaishou_records
                if isinstance(record, dict)
                for job in [self._from_kuaishou(record, target_url)]
                if job
            ]

        # Bilibili social recruitment: the rendered list is backed by
        # /api/srs/position/positionList and stores jobs in data.list.
        bilibili_records = self._dig(payload, "data", "list")
        if (
            "jobs.bilibili.com" in urlparse(target_url).netloc.lower()
            and "/api/srs/position/positionlist" in response_url.lower()
            and isinstance(bilibili_records, list)
        ):
            return [
                job
                for record in bilibili_records
                if isinstance(record, dict)
                for job in [self._from_bilibili(record, target_url)]
                if job
            ]

        # Baidu social recruitment: /httservice/getPostListNew.
        baidu_records = self._dig(payload, "data", "list")
        if (
            "talent.baidu.com" in urlparse(target_url).netloc.lower()
            and "/httservice/getpostlistnew" in response_url.lower()
            and isinstance(baidu_records, list)
        ):
            return [
                job
                for record in baidu_records
                if isinstance(record, dict)
                for job in [self._from_baidu(record, target_url)]
                if job
            ]

        # Xiaohongshu official recruitment API.
        xhs_records = self._dig(payload, "data", "list")
        if (
            isinstance(xhs_records, list)
            and any(isinstance(item, dict) and "positionId" in item for item in xhs_records)
        ):
            return [
                job
                for record in xhs_records
                if isinstance(record, dict)
                for job in [self._from_xiaohongshu(record, target_url)]
                if job
            ]

        # ByteDance and Feishu recruitment portals share a response shape, but
        # their public detail URL paths differ.
        feishu_records = self._dig(payload, "data", "job_post_list")
        if isinstance(feishu_records, list):
            converter = (
                self._from_bytedance
                if "jobs.bytedance.com" in urlparse(target_url).netloc.lower()
                else self._from_feishu
            )
            return [
                job
                for record in feishu_records
                if isinstance(record, dict)
                for job in [converter(record, target_url)]
                if job
            ]

        # Meituan official recruitment API.
        meituan_records = self._dig(payload, "data", "list")
        if (
            isinstance(meituan_records, list)
            and any(isinstance(item, dict) and "jobUnionId" in item for item in meituan_records)
        ):
            return [
                job
                for record in meituan_records
                if isinstance(record, dict)
                for job in [self._from_meituan(record, target_url)]
                if job
            ]

        # PDD changes its response wrapper periodically.  Parse successful
        # records by their stable position code/name pair, if the current
        # browser session is allowed to receive them.
        if (
            "pddglobalhr.com" in urlparse(target_url).netloc.lower()
            and "/api/recruit/position/list" in response_url.lower()
            and payload.get("success") is True
        ):
            records = self._find_dict_lists(payload, required_keys={"name", "code"})
            return [
                job
                for record in records
                for job in [self._from_pdd(record, target_url)]
                if job
            ]

        return self._jobs_from_generic_payload(payload, response_url, target_url)

    @staticmethod
    def _normalized_schema_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value or "").casefold())

    @classmethod
    def _record_value(cls, record: Dict, aliases: Iterable[str]) -> Any:
        normalized = {
            cls._normalized_schema_key(key): value
            for key, value in record.items()
        }
        for alias in aliases:
            value = normalized.get(cls._normalized_schema_key(alias))
            if value not in (None, "", [], {}):
                if isinstance(value, dict):
                    for nested_key in ("url", "href", "link", "value", "name", "label", "title"):
                        nested_value = value.get(nested_key)
                        if nested_value not in (None, ""):
                            return nested_value
                return value
        return None

    @classmethod
    def _generic_record_lists(cls, payload: Any) -> List[List[Dict]]:
        """Find plausible record arrays without depending on a vendor schema."""
        result: List[List[Dict]] = []

        def walk(value: Any, depth: int = 0):
            if depth > 6:
                return
            if isinstance(value, list):
                dictionaries = [item for item in value[:100] if isinstance(item, dict)]
                if dictionaries:
                    result.append(dictionaries)
                for item in value[:20]:
                    if isinstance(item, (dict, list)):
                        walk(item, depth + 1)
                return
            if isinstance(value, dict):
                for child in list(value.values())[:80]:
                    if isinstance(child, (dict, list)):
                        walk(child, depth + 1)

        walk(payload)
        return result

    def _from_generic_record(
        self,
        record: Dict,
        response_url: str,
        target_url: str,
    ) -> Optional[Dict]:
        title_aliases = (
            "jobTitle", "jobName", "positionTitle", "positionName", "postTitle",
            "postName", "vacancyTitle", "recruitPostName", "title", "职位名称",
            "岗位名称",
        )
        url_aliases = (
            "jobUrl", "positionUrl", "postUrl", "detailUrl", "applyUrl",
            "redirectUrl", "href", "link", "url", "详情地址",
        )
        id_aliases = (
            "jobId", "positionId", "postId", "vacancyId", "recruitPostId",
            "jobCode", "positionCode", "postCode", "code", "id",
        )
        description_aliases = (
            "description", "jobDescription", "positionDescription", "responsibility",
            "responsibilities", "requirement", "requirements", "qualification",
            "qualifications", "jobDuty", "jobDuties", "职责", "任职要求",
        )
        city_aliases = (
            "city", "cityName", "location", "locationName", "workPlace",
            "workplace", "workLocation", "address", "工作地点", "城市",
        )
        date_aliases = (
            "publishTime", "publishedAt", "publishDate", "releaseTime", "createTime",
            "createdAt", "updateTime", "updatedAt", "refreshTime", "发布日期",
        )
        company_aliases = (
            "company", "companyName", "brand", "brandName", "tenantName", "公司",
        )
        detail_url = self._record_value(record, url_aliases)
        title = self._record_value(record, title_aliases)
        if not title or not detail_url:
            return None

        keys = {self._normalized_schema_key(key) for key in record}
        has_extra_job_clue = any(
            self._normalized_schema_key(alias) in keys
            for alias in (
                *id_aliases, *description_aliases, *city_aliases, *date_aliases,
                "department", "category", "jobType", "positionType",
            )
        )
        response_looks_job_related = bool(re.search(
            r"job|position|post|recruit|career|vacanc",
            str(response_url or ""),
            flags=re.IGNORECASE,
        ))
        if not has_extra_job_clue and not response_looks_job_related:
            return None

        detail_url = urljoin(target_url, self._text(detail_url))
        parsed_detail = urlparse(detail_url)
        if parsed_detail.scheme not in ("http", "https") or not parsed_detail.netloc:
            return None

        descriptions = []
        for alias in description_aliases:
            value = self._record_value(record, (alias,))
            text = self._join_names(value, preserve_long_text=True)
            if text and text not in descriptions:
                descriptions.append(text)
        for alias in ("department", "departmentName", "category", "categoryName", "jobType"):
            value = self._record_value(record, (alias,))
            text = self._join_names(value)
            if text and text not in descriptions:
                descriptions.append(text)

        return self._job(
            title=title,
            company=(
                self._record_value(record, company_aliases)
                or self._company_for_url(target_url)
            ),
            city=self._record_value(record, city_aliases),
            url=detail_url,
            description="\n".join(descriptions),
            publish_time=self._record_value(record, date_aliases),
            target_url=target_url,
        )

    def _jobs_from_generic_payload(
        self,
        payload: Any,
        response_url: str,
        target_url: str,
    ) -> List[Dict]:
        jobs: List[Dict] = []
        for records in self._generic_record_lists(payload):
            parsed = [
                job
                for record in records
                for job in [self._from_generic_record(record, response_url, target_url)]
                if job
            ]
            jobs = self._merge_jobs(jobs, parsed)
            if len(jobs) >= MAX_JOBS_PER_SEARCH:
                break
        return self._limit_search_results(jobs)

    def _from_tencent(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("RecruitPostName"))
        post_id = self._text(record.get("PostId"))
        if not title or not post_id:
            return None
        detail_url = self._text(record.get("PostURL"))
        if detail_url:
            detail_url = re.sub(r"^http://", "https://", detail_url)
        else:
            detail_url = f"https://careers.tencent.com/jobdesc.html?postId={post_id}"
        return self._job(
            title=title,
            company="腾讯",
            city=record.get("LocationName"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("Responsibility")),
                ("岗位要求", record.get("Requirement")),
                ("岗位类别", record.get("CategoryName")),
            ),
            publish_time=record.get("LastUpdateTime"),
            target_url=target_url,
            internal={"_tencent_post_id": post_id},
        )

    def _from_alibaba(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("name"))
        position_url = self._text(record.get("positionUrl"))
        position_id = self._text(record.get("id"))
        if not title or not (position_url or position_id):
            return None
        if position_url:
            detail_url = urljoin(target_url, position_url)
        else:
            detail_url = urljoin(target_url, f"/position-detail?positionId={position_id}")
        return self._job(
            title=title,
            company=self._company_for_url(target_url),
            city=record.get("workLocations"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("description")),
                ("任职要求", record.get("requirement")),
                ("职位类别", record.get("categories")),
                ("所属部门", record.get("department")),
            ),
            publish_time=record.get("publishTime"),
            target_url=target_url,
        )

    def _from_kuaishou(self, record: Dict, target_url: str) -> Optional[Dict]:
        position_id = self._text(record.get("id"))
        title = self._text(record.get("name"))
        if not position_id or not title:
            return None

        parsed = urlparse(target_url)
        detail_url = (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            f"#/official/social/job-info/{position_id}"
        )
        location_codes = record.get("workLocationsCode") or record.get(
            "workLocationCode"
        )
        return self._job(
            title=title,
            company="快手",
            city=self._kuaishou_location_names(location_codes),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("description")),
                ("任职要求", record.get("positionDemand")),
                ("岗位类别", record.get("positionCategoryCode")),
            ),
            publish_time=record.get("updateTime"),
            target_url=target_url,
        )

    def _from_bilibili(self, record: Dict, target_url: str) -> Optional[Dict]:
        """Map a Bilibili social-recruitment result to the common job shape."""
        position_id = self._text(record.get("id"))
        title = self._text(record.get("positionName"))
        if not position_id or not title:
            return None

        parsed = urlparse(target_url)
        detail_url = (
            f"{parsed.scheme}://{parsed.netloc}/social/positions/{position_id}"
        )
        return self._job(
            title=title,
            company="哔哩哔哩",
            city=record.get("workLocation") or record.get("workCity"),
            url=detail_url,
            description=self._sections(
                ("岗位职责与要求", record.get("positionDescription")),
                ("岗位类别", record.get("postCodeName")),
                ("职位类型", record.get("positionTypeName")),
            ),
            publish_time=record.get("pushTime") or record.get("ctime"),
            target_url=target_url,
        )

    @classmethod
    def _kuaishou_location_names(cls, value: Any) -> Any:
        names = {
            "beijing": "北京",
            "shanghai": "上海",
            "guangzhou": "广州",
            "shenzhen": "深圳",
            "hangzhou": "杭州",
            "chengdu": "成都",
            "wuhan": "武汉",
            "tianjin": "天津",
            "xian": "西安",
        }
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return [
            names.get(cls._text(item).casefold(), cls._text(item))
            for item in values
            if cls._text(item)
        ]

    def _from_xiaohongshu(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("positionName"))
        position_id = self._text(record.get("positionId"))
        if not title or not position_id:
            return None
        parsed = urlparse(target_url)
        detail_url = (
            f"{parsed.scheme}://{parsed.netloc}/social/position/{position_id}"
        )
        return self._job(
            title=title,
            company="小红书",
            city=record.get("workplace"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("duty")),
                ("任职要求", record.get("qualification")),
                ("职位类别", record.get("jobType")),
                ("招聘项目", record.get("jobProjectName")),
            ),
            publish_time=record.get("publishTime"),
            target_url=target_url,
        )

    def _from_baidu(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("name"))
        post_id = self._text(record.get("postId"))
        if not title or not post_id:
            return None
        detail_url = (
            "https://talent.baidu.com/jobs/detail/SOCIAL/"
            f"{post_id}"
        )
        return self._job(
            title=title,
            company="百度",
            city=record.get("workPlace"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("workContent")),
                ("任职要求", record.get("serviceCondition")),
                ("职位类别", record.get("postType")),
                ("所属部门", record.get("orgName") or record.get("bgShortName")),
                ("招聘人数", record.get("recruitNum")),
            ),
            publish_time=record.get("publishDate"),
            target_url=target_url,
        )

    def _from_feishu(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("title"))
        position_id = self._text(record.get("id"))
        if not title or not position_id:
            return None
        parsed = urlparse(target_url)
        detail_url = (
            f"{parsed.scheme}://{parsed.netloc}/index/position/{position_id}/detail"
        )
        return self._job(
            title=title,
            company=self._company_for_url(target_url),
            city=record.get("city_list"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("description")),
                ("任职要求", record.get("requirement")),
                ("职位类别", record.get("job_category")),
                ("招聘类型", record.get("recruit_type")),
            ),
            publish_time=record.get("publish_time"),
            target_url=target_url,
        )

    def _from_bytedance(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("title"))
        position_id = self._text(record.get("id"))
        if not title or not position_id:
            return None
        parsed = urlparse(target_url)
        section_path = parsed.path.split("/position/list", 1)[0] or "/experienced"
        detail_url = (
            f"{parsed.scheme}://{parsed.netloc}"
            f"{section_path}/position/{position_id}/detail"
        )
        return self._job(
            title=title,
            company="字节跳动",
            city=record.get("city_list") or record.get("city_info"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("description")),
                ("任职要求", record.get("requirement")),
                ("职位类别", record.get("job_category")),
                ("招聘类型", record.get("recruit_type")),
            ),
            publish_time=record.get("publish_time"),
            target_url=target_url,
        )

    def _from_meituan(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("name"))
        job_id = self._text(record.get("jobUnionId"))
        if not title or not job_id:
            return None
        detail_url = (
            "https://zhaopin.meituan.com/web/position/detail"
            f"?jobUnionId={job_id}&highlightType=social"
        )
        return self._job(
            title=title,
            company="美团",
            city=record.get("cityList"),
            url=detail_url,
            description=self._sections(
                ("岗位职责", record.get("jobDuty")),
                ("任职要求", record.get("jobRequirement")),
                ("职位亮点", record.get("highLight")),
                ("所属部门", record.get("department")),
            ),
            publish_time=record.get("refreshTime"),
            target_url=target_url,
        )

    def _from_pdd(self, record: Dict, target_url: str) -> Optional[Dict]:
        title = self._text(record.get("name") or record.get("positionName"))
        code = self._text(record.get("code") or record.get("positionCode"))
        if not title or not code:
            return None
        detail_url = urljoin(target_url, f"/jobs/detail?code={code}")
        return self._job(
            title=title,
            company="拼多多",
            city=(
                record.get("workLocation")
                or record.get("workLocations")
                or record.get("city")
            ),
            url=detail_url,
            description=self._sections(
                (
                    "岗位职责",
                    record.get("jobDuty")
                    or record.get("workContent")
                    or record.get("description"),
                ),
                (
                    "任职要求",
                    record.get("jobRequirement")
                    or record.get("requirements")
                    or record.get("requirement"),
                ),
                ("职位类别", record.get("categoryName") or record.get("jobCategory")),
            ),
            publish_time=(
                record.get("publishTime")
                or record.get("updateTime")
                or record.get("refreshTime")
            ),
            target_url=target_url,
        )

    def _jobs_from_rendered_page(
        self,
        page: Page,
        target_url: str,
        keyword: str,
    ) -> List[Dict]:
        jobs: List[Dict] = []
        for surface in self._page_surfaces(page):
            jobs = self._merge_jobs(
                jobs,
                self._jobs_from_structured_data(surface, target_url, keyword),
            )
            jobs = self._merge_jobs(
                jobs,
                self._jobs_from_rendered_surface(surface, target_url, keyword),
            )
        return jobs

    def _jobs_from_structured_data(
        self,
        surface: Any,
        target_url: str,
        keyword: str,
    ) -> List[Dict]:
        """Read standards-based JobPosting JSON-LD from unfamiliar sites."""
        try:
            raw_scripts = surface.locator(
                "script[type='application/ld+json']"
            ).evaluate_all(
                "elements => elements.map(element => element.textContent || '')"
            )
        except Exception:
            return []

        records: List[Dict] = []

        def walk(value: Any, depth: int = 0):
            if depth > 8:
                return
            if isinstance(value, list):
                for child in value[:100]:
                    walk(child, depth + 1)
                return
            if not isinstance(value, dict):
                return
            record_type = value.get("@type")
            types = record_type if isinstance(record_type, list) else [record_type]
            if any(str(item or "").casefold() == "jobposting" for item in types):
                records.append(value)
            for key, child in value.items():
                if key != "@context" and isinstance(child, (dict, list)):
                    walk(child, depth + 1)

        for raw_script in raw_scripts:
            try:
                walk(json.loads(raw_script))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

        jobs: List[Dict] = []
        for record in records:
            title = self._text(record.get("title") or record.get("name"))
            description = self._text(
                record.get("description"),
                preserve_lines=True,
            )
            if keyword and keyword.casefold() not in f"{title}\n{description}".casefold():
                continue
            detail_url = self._text(record.get("url") or record.get("@id"))
            if not title or not detail_url:
                continue

            organization = record.get("hiringOrganization")
            company = (
                organization.get("name")
                if isinstance(organization, dict)
                else organization
            )
            location = record.get("jobLocation")
            if isinstance(location, list):
                location_values = location
            else:
                location_values = [location]
            cities = []
            for item in location_values:
                if not isinstance(item, dict):
                    continue
                address = item.get("address")
                if isinstance(address, dict):
                    city = (
                        address.get("addressLocality")
                        or address.get("addressRegion")
                        or address.get("streetAddress")
                    )
                else:
                    city = address or item.get("name")
                city = self._text(city)
                if city and city not in cities:
                    cities.append(city)

            jobs.append(self._job(
                title=title,
                company=company or self._company_for_url(target_url),
                city=cities,
                url=detail_url,
                description=description,
                publish_time=record.get("datePosted"),
                target_url=target_url,
            ))
        return self._merge_jobs([], jobs)

    def _jobs_from_rendered_surface(
        self,
        surface: Any,
        target_url: str,
        keyword: str,
    ) -> List[Dict]:
        try:
            cards = surface.locator("a[href], [data-href], [data-url]")
            raw_cards = cards.evaluate_all(
                """
                elements => elements.map(anchor => {
                  const pick = selector => {
                    const node = anchor.querySelector(selector);
                    return node ? (node.innerText || node.textContent || '').trim() : '';
                  };
                  const rawHref = anchor.href || anchor.getAttribute('href')
                    || anchor.getAttribute('data-href') || anchor.getAttribute('data-url') || '';
                  let href = rawHref;
                  try { href = new URL(rawHref, document.baseURI).href; } catch (_) {}
                  return {
                    href,
                    text: (anchor.innerText || anchor.textContent || '').trim(),
                    title: pick(
                      '[class*="title-"], [class*="job-title" i], [class*="job_name" i], '
                      + '[class*="job-name" i], [class*="position-name" i], '
                      + '[class*="position-title" i], [data-testid*="title" i], h2, h3'
                    ),
                    date: pick('[class*="published-at-"], [class*="date" i], time'),
                    info: pick(
                      '[class*="info-"], [class*="location" i], [class*="city" i], '
                      + '[class*="meta" i]'
                    ),
                    detail: pick(
                      '[class*="short-description-"], [class*="description-"], '
                      + '[class*="summary" i], [class*="requirement" i]'
                    ),
                    excluded: Boolean(anchor.closest(
                      '#latest_jobs, #recommended_jobs, '
                      + '[id*="recommend"], [class*="recommend-job"], '
                      + '[class*="latest-job"]'
                    ))
                  };
                })
                """
            )
        except Exception:
            return []

        jobs = []
        for card in raw_cards:
            if card.get("excluded"):
                continue
            href = self._text(card.get("href"))
            full_text = self._text(card.get("text"), preserve_lines=True)
            if not href or not full_text or not self._looks_like_job_url(href):
                continue
            if keyword and keyword.casefold() not in full_text.casefold():
                continue

            lines = self._unique_lines(full_text)
            title = self._text(card.get("title")) or (lines[0] if lines else "")
            if not 2 <= len(title) <= 160:
                continue

            detail = self._text(card.get("detail"), preserve_lines=True)
            description = detail or "\n".join(lines[1:])
            if len(description) < 20 and len(full_text) >= 20:
                description = full_text

            date_text = self._text(card.get("date")) or self._extract_date(full_text)
            info = self._text(card.get("info"), preserve_lines=True)
            city = self._city_from_card_info(info)
            jobs.append(
                self._job(
                    title=title,
                    company=self._company_for_url(target_url),
                    city=city,
                    url=href,
                    description=description,
                    publish_time=date_text,
                    target_url=target_url,
                )
            )
        return [job for job in jobs if job]

    @staticmethod
    def _looks_like_job_url(url: str) -> bool:
        lowered = url.lower()
        patterns = (
            r"/job/[^/?#]+",
            r"/jobs/[^/?#]+",
            r"/jobs/detail",
            r"/position/[^/?#]+",
            r"/positions/[^/?#]+",
            r"jobdesc",
            r"job-detail",
            r"jobdetail",
            r"position/detail",
            r"position-detail",
            r"positiondetail",
            r"(?:[?&#]|%3f)(?:job|position|post)(?:id|code)=",
            r"#/(?:jobs?|positions?|posts?)/[^/?#]+",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _enrich_tencent_jobs(self, jobs: List[Dict], target_url: str):
        pending = [
            job for job in jobs
            if job.get("_tencent_post_id")
            and "任职要求" not in str(job.get("description") or "")
            and "岗位要求" not in str(job.get("description") or "")
        ]
        if not pending:
            return

        def fetch(job):
            post_id = job["_tencent_post_id"]
            response = self.session.get(
                "https://careers.tencent.com/tencentcareer/api/post/ByPostId",
                params={"postId": post_id, "language": "zh-cn"},
                headers={"Referer": target_url},
                timeout=12,
            )
            response.raise_for_status()
            data = response.json().get("Data") or {}
            return job, data

        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
            futures = [executor.submit(fetch, job) for job in pending]
            for future in as_completed(futures):
                try:
                    job, data = future.result()
                    details = self._sections(
                        ("岗位职责", data.get("Responsibility")),
                        ("岗位要求", data.get("Requirement")),
                        ("重要说明", data.get("ImportantItem")),
                    )
                    if details:
                        job["description"] = details
                    if data.get("LastUpdateTime"):
                        job["publish_time"] = self._normalize_date(
                            data.get("LastUpdateTime")
                        )
                except Exception as error:
                    logger.debug("腾讯职位明细补充失败: %s", error)

    def _enrich_pdd_jobs(self, context: BrowserContext, jobs: List[Dict]):
        pending = [
            job for job in jobs
            if "pddglobalhr.com" in str(job.get("source_site") or "").lower()
            and len(str(job.get("description") or "").strip()) < 30
        ][:10]
        for job in pending:
            detail_page = context.new_page()
            try:
                detail_page.goto(
                    job["url"],
                    timeout=45_000,
                    wait_until="domcontentloaded",
                )
                detail_page.wait_for_timeout(2_500)
                body_text = self._text(
                    detail_page.locator("body").inner_text(),
                    preserve_lines=True,
                )
                if "职位已过期或不存在" in body_text:
                    continue
                detail = detail_page.locator(".recruit-detail-body")
                if detail.count():
                    detail_text = self._text(
                        detail.first.inner_text(),
                        preserve_lines=True,
                    )
                    if len(detail_text) >= 30:
                        job["description"] = detail_text
                date_text = self._extract_date(body_text)
                if date_text:
                    job["publish_time"] = self._normalize_date(date_text)
            except Exception:
                continue
            finally:
                detail_page.close()

    def _job(
        self,
        *,
        title: Any,
        company: Any,
        city: Any,
        url: Any,
        description: Any,
        publish_time: Any,
        target_url: str,
        internal: Optional[Dict] = None,
    ) -> Dict:
        source_site = urlparse(target_url).netloc.lower()
        job = {
            "title": self._text(title),
            "company": self._text(company) or source_site,
            "salary": NEGOTIABLE_SALARY,
            "city": self._join_names(city) or UNKNOWN_CITY,
            "url": urljoin(target_url, self._text(url)),
            "description": self._text(description, preserve_lines=True),
            "publish_time": self._normalize_date(publish_time),
            "source_site": source_site,
        }
        if internal:
            job.update(internal)
        return job

    def _finalize_jobs(self, jobs: List[Dict], fallback_source: str) -> List[Dict]:
        finalized = []
        for job in self._merge_jobs([], jobs):
            title = self._text(job.get("title"))
            url = self._text(job.get("url"))
            if not title or not url:
                continue
            clean = dict(job)
            clean["title"] = title
            clean["url"] = url
            clean["description"] = self._text(
                clean.get("description"),
                preserve_lines=True,
            )
            clean["company"] = self._text(clean.get("company")) or fallback_source
            clean["salary"] = self._text(clean.get("salary")) or NEGOTIABLE_SALARY
            clean["city"] = self._join_names(clean.get("city")) or UNKNOWN_CITY
            clean["publish_time"] = self._normalize_date(clean.get("publish_time"))
            clean["source_site"] = (
                self._text(clean.get("source_site")).lower()
                or fallback_source.lower()
            )
            clean.pop("_tencent_post_id", None)
            finalized.append(clean)
        return finalized

    @classmethod
    def _merge_jobs(cls, existing: List[Dict], incoming: Iterable[Dict]) -> List[Dict]:
        merged = list(existing)
        index = {}
        for position, job in enumerate(merged):
            key = cls._job_key(job)
            if key:
                index[key] = position

        for job in incoming or []:
            if not isinstance(job, dict):
                continue
            key = cls._job_key(job)
            if not key:
                continue
            if key not in index:
                index[key] = len(merged)
                merged.append(job)
                continue

            old = merged[index[key]]
            search_keywords = []
            for source in (old, job):
                values = source.get("_search_keywords")
                if not isinstance(values, (list, tuple, set)):
                    values = [source.get("_search_keyword")]
                for value in values:
                    clean_value = str(value or "").strip()
                    if clean_value and clean_value not in search_keywords:
                        search_keywords.append(clean_value)
            if search_keywords:
                old["_search_keyword"] = search_keywords[0]
                old["_search_keywords"] = search_keywords

            for field, value in job.items():
                if field in ("_search_keyword", "_search_keywords"):
                    continue
                if field == "description":
                    if len(str(value or "")) > len(str(old.get(field) or "")):
                        old[field] = value
                elif field == "city":
                    if str(old.get(field) or "") in ("", UNKNOWN_CITY):
                        old[field] = value
                elif not old.get(field) and value:
                    old[field] = value
        return merged

    @staticmethod
    def _job_key(job: Dict) -> str:
        url = canonicalize_job_url(job.get("url"))
        if url:
            return f"url:{url}"
        title = str(job.get("title") or "").strip()
        company = str(job.get("company") or "").strip()
        return f"title:{company}:{title}" if title else ""

    @classmethod
    def _sections(cls, *sections) -> str:
        result = []
        for heading, value in sections:
            text = cls._join_names(value, preserve_long_text=True)
            if text:
                result.append(f"{heading}\n{text}")
        return "\n\n".join(result)

    @classmethod
    def _join_names(cls, value: Any, preserve_long_text: bool = False) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            preferred = (
                value.get("name")
                or value.get("label")
                or value.get("value")
                or value.get("title")
            )
            return cls._text(preferred)
        if isinstance(value, (list, tuple, set)):
            parts = [cls._join_names(item, preserve_long_text) for item in value]
            unique = []
            for part in parts:
                if part and part not in unique:
                    unique.append(part)
            return "、".join(unique)
        return cls._text(value, preserve_lines=preserve_long_text)

    @staticmethod
    def _text(value: Any, preserve_lines: bool = False) -> str:
        if value is None:
            return ""
        text = str(value)
        if "<" in text and ">" in text:
            text = BeautifulSoup(text, "html.parser").get_text("\n")
        text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
        if preserve_lines:
            lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
            return "\n".join(line for line in lines if line).strip()
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _normalize_date(cls, value: Any) -> str:
        if value in (None, ""):
            return "未知"
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                return "未知"

        text = cls._text(value)
        match = re.search(r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})", text)
        if match:
            year, month, day = (int(part) for part in match.groups())
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return text[:40] or "未知"

    @staticmethod
    def _extract_date(text: str) -> str:
        match = re.search(r"20\d{2}[./年-]\d{1,2}[./月-]\d{1,2}", text or "")
        return match.group(0) if match else ""

    @classmethod
    def _unique_lines(cls, text: str) -> List[str]:
        lines = []
        for raw_line in str(text or "").splitlines():
            line = cls._text(raw_line)
            if line and (not lines or lines[-1] != line):
                lines.append(line)
        return lines

    @classmethod
    def _city_from_card_info(cls, info: str) -> str:
        candidates = cls._unique_lines(info)
        ignored = {"全职", "兼职", "实习", "社招", "校招"}
        for candidate in reversed(candidates):
            if candidate in ignored or candidate.endswith("类"):
                continue
            if (
                any(suffix in candidate for suffix in ("市", "区", "省"))
                or candidate in {"北京", "上海", "广州", "深圳", "杭州", "成都"}
            ):
                return candidate
        return UNKNOWN_CITY

    @staticmethod
    def _company_for_url(url: str) -> str:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        lowered = url.lower()
        if "tencent" in host:
            return "腾讯"
        if "ele.me" in host:
            return "饿了么"
        if "aliyun" in host:
            return "阿里云"
        if "quark" in host:
            return "夸克"
        if "taotian" in host:
            return "淘天"
        if "xiaohongshu" in host:
            return "小红书"
        if "bilibili.com" in host:
            return "哔哩哔哩"
        if "bytedance" in host:
            return "字节跳动"
        if "baidu" in host:
            return "百度"
        if host == "vrfi1sk8a0.jobs.feishu.cn":
            return "MiniMax"
        if "/high-flyer/" in lowered:
            return "DeepSeek"
        if "/step/" in lowered:
            return "阶跃星辰"
        if "moonshot" in lowered:
            return "月之暗面"
        if "/zphz/" in lowered:
            return "智谱"
        if "meituan" in host:
            return "美团"
        if "pddglobalhr" in host:
            return "拼多多"
        return host

    @staticmethod
    def _dig(value: Any, *path: str) -> Any:
        current = value
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    @classmethod
    def _find_dict_lists(
        cls,
        value: Any,
        required_keys: set,
    ) -> List[Dict]:
        found = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and required_keys.issubset(item):
                    found.append(item)
                else:
                    found.extend(cls._find_dict_lists(item, required_keys))
        elif isinstance(value, dict):
            for item in value.values():
                found.extend(cls._find_dict_lists(item, required_keys))
        return found
