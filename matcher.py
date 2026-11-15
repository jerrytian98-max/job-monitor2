"""
职位匹配模块
根据配置过滤和匹配符合条件的职位
"""

from ai_filter import filter_jobs_with_ai
from web_config import AI_PROVIDER_FIELDS
from typing import List, Dict
import logging
import json
import hashlib
import unicodedata

import os
from profile_config import normalize_profile, profile_suffix
BASE_DIR = os.environ.get('JOB_MONITOR_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
PROFILE = normalize_profile(os.environ.get('JOB_PROFILE', ''))
PROFILE_SUFFIX = profile_suffix(PROFILE)
CACHE_FILE = os.path.join(BASE_DIR, f'jobs_cache{PROFILE_SUFFIX}.json')

from database import db
from job_identity import job_identity_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class JobMatcher:
    """职位匹配器"""
    
    def __init__(
        self,
        config: dict,
        storage_file: str = CACHE_FILE,
        database=None,
        persist: bool = True,
    ):
        self.config = config
        self.storage_file = storage_file
        self.database = database or db
        self.persist = persist
        self.known_jobs = self._load_known_jobs() if persist else {}
    
    def _load_known_jobs(self) -> dict:
        """加载已知的职位缓存"""
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载职位缓存失败: {e}")
                return {}
        return {}
    
    def _save_known_jobs(self):
        """保存已知的职位缓存"""
        if not self.persist:
            return
        try:
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                json.dump(self.known_jobs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存职位缓存失败: {e}")
    
    def _generate_job_id(self, job: Dict) -> str:
        """为职位生成唯一ID（基于URL和标题，避免因抓取信息的微小差异导致重复）"""
        return job_identity_hash(job)
    
    def is_new_job(self, job: Dict) -> bool:
        """
        判断是否为新职位

        Args:
            job: 职位信息

        Returns:
            True表示新职位，False表示已存在
        """
        job_id = self._generate_job_id(job)

        # 测试检查只验证抓取和筛选，不污染数据库或去重缓存。
        if not self.persist:
            return True

        state = self.database.get_job_state(job)
        legacy_entry = self.known_jobs.get(job_id, {})
        legacy_notified = bool(legacy_entry.get('found_time'))

        if state:
            site_label = str(job.get('site_label') or '').strip()
            # A job keeps the first label it was saved with.  It may be found
            # by another configured keyword later, but that must not replace
            # the original keyword label (for example 腾讯法律 -> 腾讯法务).
            if site_label and not str(state.get('site_label') or '').strip():
                self.database.update_job_site_label(job)
            # 兼容旧版本：旧 JSON 缓存记录过通知时间，但 SQLite 没有同步。
            if legacy_notified and not state.get('is_notified'):
                self.database.mark_jobs_as_notified([job])
                return False
            # 通知失败的职位保持 is_notified=0，下次检查时应继续重试。
            return not bool(state.get('is_notified'))

        if not self.database.add_job(job):
            # 并发插入时重新读取最终状态。
            state = self.database.get_job_state(job)
            return bool(state) and not bool(state.get('is_notified'))

        self.known_jobs[job_id] = {
            'job': job,
            'found_time': None
        }
        return True
    
    @staticmethod
    def _normalized_text(value) -> str:
        """Normalize scraped and configured text for reliable matching."""
        if value is None:
            return ''
        if isinstance(value, set):
            value = sorted(value, key=str)
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return unicodedata.normalize('NFKC', str(value)).strip().casefold()

    def _matches_city_constraint(self, job: Dict) -> bool:
        """Apply configured cities when a job exposes a meaningful location."""
        configured = [
            self._normalized_text(city)
            for city in self.config.get('cities', [])
            if self._normalized_text(city)
        ]
        if not configured:
            return True

        job_city = self._normalized_text(job.get('city'))
        unrestricted_locations = {
            '', '未知', '不限', '地点不限', '全国', '远程', 'remote',
        }
        if job_city in unrestricted_locations:
            # Some sites do not expose a city field or offer no city filter.
            # Do not discard an otherwise valid job merely for missing data.
            return True

        return any(
            city in job_city or job_city in city
            for city in configured
        )

    def _contains_excluded_keyword(self, job: Dict) -> bool:
        """Search every user-facing scraped field after the crawl completes."""
        keywords = [
            self._normalized_text(keyword)
            for keyword in self.config.get('exclude_keywords', [])
            if self._normalized_text(keyword)
        ]
        if not keywords:
            return False

        searchable_values = []
        for field, value in job.items():
            # Internal control fields and raw URLs are not job content. A URL
            # path such as /intern/ must not exclude an otherwise unrelated job.
            if str(field).startswith('_') or field == 'url':
                continue
            normalized = self._normalized_text(value)
            if normalized:
                searchable_values.append(normalized)
        searchable_text = '\n'.join(searchable_values)
        return any(keyword in searchable_text for keyword in keywords)

    def _matches_required_keywords(self, job: Dict) -> bool:
        """Apply the ordinary keyword rule after hard constraints."""
        if job.get('_fixed_url_mode'):
            return True

        job_keywords = self.config.get('job_keywords', [])
        if not job_keywords:
            return True

        title = self._normalized_text(job.get('title'))
        description = self._normalized_text(job.get('description'))
        return any(
            self._normalized_text(keyword) in title
            or self._normalized_text(keyword) in description
            for keyword in job_keywords
            if self._normalized_text(keyword)
        )

    def matches_hard_constraints(self, job: Dict) -> bool:
        """Rules that must hold with or without the optional AI filter."""
        return (
            self._matches_city_constraint(job)
            and not self._contains_excluded_keyword(job)
        )

    def match_job(self, job: Dict) -> bool:
        """
        判断职位是否匹配配置条件
        
        Args:
            job: 职位信息
        
        Returns:
            True表示匹配，False表示不匹配
        """
        # 移除了目标公司过滤逻辑，因为现在通过目标网址抓取，抓取到的即视为该公司的职位
        
        if not self.matches_hard_constraints(job):
            return False
        return self._matches_required_keywords(job)
    
    def filter_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """
        先识别本轮新职位，再让 AI 判断，最后只保存通过的职位。
        
        Args:
            jobs: 职位列表
        
        Returns:
            符合条件的新职位列表
        """
        provider = self.config.get('ai_provider') or 'gemini'
        key_field, model_field, default_model = AI_PROVIDER_FIELDS.get(provider, AI_PROVIDER_FIELDS['gemini'])
        ai_key = self.config.get(key_field, '')
        ai_prompt = self.config.get('ai_filter_prompt', '')
        use_ai = self.config.get('ai_filter_enabled', bool(ai_key and ai_prompt))
        policy = hashlib.sha256(json.dumps(
            [provider, self.config.get(model_field, default_model), ai_prompt],
            ensure_ascii=False,
        ).encode('utf-8')).hexdigest() if use_ai else None

        candidates = []
        pending_notification = []
        seen_job_ids = set()
        for job in jobs:
            try:
                if not isinstance(job, dict):
                    continue
                job_id = self._generate_job_id(job)
                if job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job_id)
                # City and exclusion rules are deterministic user constraints.
                # They always run before database writes and before the optional
                # AI filter. AI only replaces the ordinary positive-keyword
                # matching step.
                if not self.matches_hard_constraints(job):
                    continue
                if not use_ai and not self._matches_required_keywords(job):
                    continue
                if self.persist:
                    state = self.database.get_job_state(job)
                    if state:
                        if not state.get('is_notified'):
                            pending_notification.append(job)
                        continue
                    cached = self.known_jobs.get(job_id, {})
                    if cached.get('found_time') or (use_ai and cached.get('ai_rejected_policy') == policy):
                        continue
                candidates.append(job)
            except Exception as e:
                logger.error(f"过滤职位时出错: {e}")

        if use_ai and candidates:
            selected = filter_jobs_with_ai(
                candidates,
                ai_key,
                ai_prompt,
                self.config.get(model_field, default_model),
                provider=provider,
            )
            if self.persist:
                selected_ids = {self._generate_job_id(job) for job in selected}
                for job in candidates:
                    job_id = self._generate_job_id(job)
                    if job_id not in selected_ids:
                        self.known_jobs[job_id] = {'ai_rejected_policy': policy}
            candidates = selected

        matched_jobs = []
        for job in pending_notification + candidates:
            try:
                if self.is_new_job(job):
                    matched_jobs.append(job)
                    logger.info(
                        "发现新职位: %s - %s",
                        job.get('title', '未知职位'),
                        job.get('company', '未知公司')
                    )
            except Exception as e:
                logger.error(f"保存职位时出错: {e}")
        
        # 更新缓存
        self._save_known_jobs()
        
        return matched_jobs
    
    def mark_as_notified(self, jobs: List[Dict]):
        """
        标记职位已通知
        
        Args:
            jobs: 职位列表
        """
        if not self.persist:
            return

        self.database.mark_jobs_as_notified(jobs)
        for job in jobs:
            job_id = self._generate_job_id(job)
            if job_id in self.known_jobs:
                import time
                self.known_jobs[job_id]['found_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
        
        self._save_known_jobs()
