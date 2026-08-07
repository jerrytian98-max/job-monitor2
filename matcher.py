"""
职位匹配模块
根据配置过滤和匹配符合条件的职位
"""

from ai_filter import filter_jobs_with_ai
from typing import List, Dict
import logging
import json

import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.environ.get('JOB_PROFILE', '')
PROFILE_SUFFIX = f'_{PROFILE}' if PROFILE else ''
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
            if site_label and site_label != str(state.get('site_label') or '').strip():
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
    
    def match_job(self, job: Dict) -> bool:
        """
        判断职位是否匹配配置条件
        
        Args:
            job: 职位信息
        
        Returns:
            True表示匹配，False表示不匹配
        """
        # 移除了目标公司过滤逻辑，因为现在通过目标网址抓取，抓取到的即视为该公司的职位
        
        # 检查城市
        cities = self.config.get('cities', [])
        if cities and job.get('city', '') not in cities and job.get('city', '') != '未知':
            return False
        
        # 检查排除关键词
        exclude_keywords = self.config.get('exclude_keywords', [])
        title = job.get('title', '')
        description = job.get('description', '')
        
        for keyword in exclude_keywords:
            if keyword in title or keyword in description:
                return False
        
        # 检查职位关键词
        job_keywords = self.config.get('job_keywords', [])
        if job.get('_fixed_url_mode'):
            # Fixed-URL rows use the URL's own filters as the source of truth.
            # City and exclusion rules above still apply.
            return True
        if not job_keywords:
            return True # 如果没有配置关键词，默认全部匹配
            
        for keyword in job_keywords:
            if keyword.lower() in title.lower() or keyword.lower() in description.lower():
                return True
        
        return False
    
    def filter_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """
        过滤职位，返回符合条件的新职位
        
        Args:
            jobs: 职位列表
        
        Returns:
            符合条件的新职位列表
        """
        candidates = []
        for job in jobs:
            try:
                if self.match_job(job):
                    candidates.append(job)
            except Exception as e:
                logger.error(f"过滤职位时出错: {e}")

        # 只有同时配置 Key 和筛选要求时才调用 AI；失败时自动退回规则结果。
        candidates = filter_jobs_with_ai(
            candidates,
            self.config.get('gemini_api_key', ''),
            self.config.get('ai_filter_prompt', ''),
            self.config.get('gemini_model', 'gemini-3.5-flash-lite')
        )

        matched_jobs = []
        seen_job_ids = set()
        for job in candidates:
            try:
                job_id = self._generate_job_id(job)
                if job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job_id)
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
