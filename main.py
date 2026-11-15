"""
招聘监测系统主程序
定时监测招聘网站，发现符合条件的职位时发送邮件通知
"""

import yaml
import time
import logging
import argparse
import threading
import os
from datetime import datetime
from typing import List, Dict
from config_bootstrap import ensure_config_file
from local_schedule import WeeklySchedule
from scraper import get_scraper
from matcher import JobMatcher
from job_identity import job_identity_hash
from notifier import EmailNotifier, ConsoleNotifier
from site_labels import (
    combine_site_keyword_labels,
    get_site_label,
    get_site_mode,
    SITE_MODE_FIXED,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class JobMonitor:
    """招聘监测器"""
    
    def __init__(self, config_file: str = 'config.yaml', test_mode: bool = False):
        """
        初始化监测器
        
        Args:
            config_file: 配置文件路径
            test_mode: 测试模式（使用控制台输出而非邮件）
        """
        config_file = ensure_config_file(config_file)
        self.config_file = config_file
        self.config = self._load_config(config_file)
        self.test_mode = test_mode
        self.matcher = JobMatcher(self.config, persist=not test_mode)
        self.last_check_result = {
            'success': None,
            'checked_at': None,
            'scraped_jobs': 0,
            'matched_jobs': 0,
            'notification_sent': False,
        }
        
        # 根据模式选择通知方式
        if test_mode:
            self.notifier = ConsoleNotifier()
            logger.info("运行在测试模式，通知将输出到控制台")
        else:
            self.notifier = EmailNotifier(self.config['email'])
            logger.info("运行在正常模式，将通过邮件发送通知")
    
    def _load_config(self, config_file: str) -> dict:
        """加载配置文件"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
                if not isinstance(config, dict):
                    raise ValueError("配置文件顶层必须是对象")

                # 云端任务可用仓库 Secrets 覆盖本地敏感值，无需提交密钥。
                email_auth_code = os.environ.get('JOB_EMAIL_AUTH_CODE', '').strip()
                gemini_api_key = os.environ.get('GEMINI_API_KEY', '').strip()
                openai_api_key = os.environ.get('OPENAI_API_KEY', '').strip()
                deepseek_api_key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
                if email_auth_code and not config.get('email', {}).get('auth_code'):
                    config.setdefault('email', {})['auth_code'] = email_auth_code
                if gemini_api_key and not config.get('gemini_api_key'):
                    config['gemini_api_key'] = gemini_api_key
                if openai_api_key and not config.get('openai_api_key'):
                    config['openai_api_key'] = openai_api_key
                if deepseek_api_key and not config.get('deepseek_api_key'):
                    config['deepseek_api_key'] = deepseek_api_key
                logger.info(f"配置文件加载成功: {config_file}")
                return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise
    
    def _create_scraper(self, site_url):
        return get_scraper(site_url)

    def _scrape_all_sites(self) -> List[Dict]:
        """Collect every URL before filtering; a failed URL never skips others."""
        all_jobs = []
        seen = set()
        self._job_site_by_object = {}
        self.last_check_result['site_results'] = []
        keywords = self.config.get('job_keywords', [])
        cities = self.config.get('cities', [])
        for site_url in dict.fromkeys(self.config.get('job_sites', [])):
            label = get_site_label(self.config, site_url)
            mode = get_site_mode(self.config, site_url)
            result = dict(url=site_url, label=label, status='failed', scraped_jobs=0)
            self.last_check_result['site_results'].append(result)
            self._emit_status(f'正在抓取【{label}】', 'info', True)
            try:
                scraper = self._create_scraper(site_url)
                if scraper is None:
                    raise RuntimeError('无法创建招聘网站抓取器')
                if mode == SITE_MODE_FIXED:
                    jobs = scraper.scrape_jobs(site_url, [], cities, search_mode=mode) or []
                else:
                    jobs = scraper.scrape_jobs(site_url, keywords, cities) or []
                result.update(status='completed', scraped_jobs=len(jobs))
                self.last_check_result['scraped_jobs'] += len(jobs)
                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    if mode == SITE_MODE_FIXED:
                        job['_fixed_url_mode'] = True
                    terms = job.get('_search_keywords')
                    if not isinstance(terms, (list, tuple, set)):
                        terms = [job.get('_search_keyword', '')]
                    job['site_label'] = combine_site_keyword_labels(label, terms)
                    identity = job_identity_hash(job)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    all_jobs.append(job)
                    self._job_site_by_object[id(job)] = site_url
            except Exception as error:
                logger.error('抓取目标网址 [%s] %s 失败: %s', label, site_url, error)
                self._emit_status(f'抓取【{label}】发生错误，已继续下一个网址', 'error')
        return all_jobs

    def _emit_site_summaries(self, matched_jobs):
        counts = {}
        for job in matched_jobs:
            site = getattr(self, '_job_site_by_object', {}).get(id(job))
            counts[site] = counts.get(site, 0) + 1
        for result in self.last_check_result.get('site_results', []):
            count = counts.get(result['url'], 0)
            self._emit_status(f"抓取完毕【{result['label']}】找到{count}个新职位", 'success', True)

    def _build_email_job_list(self, new_jobs: List[Dict]) -> List[Dict]:
        """邮件只包含本轮通过规则或 AI 筛选的职位。"""
        return [dict(job, _is_new_this_run=True) for job in new_jobs]

    def _send_notification_summary(self, new_jobs: List[Dict]):
        """汇总发送邮件；没有新职位时不发送。"""
        if not new_jobs:
            return True, 0, []

        labels = []
        for job in new_jobs:
            site_label = str(job.get('site_label') or '').strip() or '招聘网站'
            if site_label not in labels:
                labels.append(site_label)

        email_jobs = self._build_email_job_list(new_jobs)
        if self.notifier.send_notification(email_jobs, new_jobs=new_jobs):
            self.matcher.mark_as_notified(new_jobs)
            return True, len(new_jobs), []

        return False, 0, labels
    
    def check_jobs(self) -> bool:
        """Shared CLI/Web/cloud pipeline: scrape, review, persist, notify."""
        self.last_check_result = dict(
            success=False, checked_at=datetime.now().isoformat(timespec='seconds'),
            scraped_jobs=0, matched_jobs=0, notification_sent=False, site_results=[],
        )
        try:
            all_jobs = self._scrape_all_sites()
            matched_jobs = self.matcher.filter_jobs(all_jobs)
            self.last_check_result['matched_jobs'] = len(matched_jobs)
            self._emit_site_summaries(matched_jobs)
            callback = getattr(self, 'job_callback', None)
            if callable(callback):
                for job in matched_jobs:
                    callback(job)
            if matched_jobs:
                success, notified_count, _ = self._send_notification_summary(matched_jobs)
                self.last_check_result['notification_sent'] = notified_count > 0
                self.last_check_result['success'] = success
                self._emit_status(
                    f'本轮监测完成，共发现并通知{notified_count}个新职位' if success
                    else '发现新职位，但邮件发送失败；下次将重试',
                    'success' if success else 'error',
                )
                return success
            self.last_check_result['success'] = True
            self._emit_status('本轮监测完成，没有新职位', 'success')
            return True
        except Exception as error:
            logger.error('检查职位时出错: %s', error)
            self._emit_status(f'检查职位时出错: {error}', 'error')
            return False
        finally:
            self._after_check()

    def _after_check(self):
        """Optional UI status hook."""

    def _emit_status(self, message: str, status_type: str = 'info', work_log=False):
        getattr(logger, status_type, logger.info)(message)
        callback = getattr(self, 'status_callback', None)
        if callable(callback):
            if work_log:
                callback(message, status_type, True)
            else:
                callback(message, status_type)
    
    def run_once(self):
        """运行一次检查"""
        return self.check_jobs()
    
    def run_forever(self, stop_event: threading.Event = None):
        """持续运行监测"""
        if self.config.get('local_schedule'):
            return self.run_weekly(stop_event)
        try:
            interval_hours = float(self.config.get('check_interval', 2))
        except (TypeError, ValueError):
            interval_hours = 2
        check_interval = max(interval_hours, 1 / 60) * 3600
        stop_event = stop_event or threading.Event()
        
        logger.info(f"监测已启动，每 {check_interval/3600:.1f} 小时检查一次")
        logger.info("按 Ctrl+C 停止监测")
        print()
        
        try:
            while not stop_event.is_set():
                self.check_jobs()

                if stop_event.is_set():
                    break
                
                # 计算下次检查时间
                next_check = time.strftime('%Y-%m-%d %H:%M:%S', 
                                          time.localtime(time.time() + check_interval))
                logger.info(f"下次检查时间: {next_check}")
                logger.info(f"等待 {check_interval/3600:.1f} 小时后再次检查...")
                print()
                
                # Event.wait 允许 Web 界面的“停止”立即唤醒后台线程。
                deadline = time.monotonic() + check_interval
                while not stop_event.is_set() and time.monotonic() < deadline:
                    if stop_event.wait(min(15, max(0, deadline - time.monotonic()))):
                        break
                    self.config = self._load_config(self.config_file)
                    if self.config.get('local_schedule'):
                        return self.run_weekly(stop_event)
                
        except KeyboardInterrupt:
            logger.info("\n监测已停止")
            print("\n感谢使用招聘监测系统！")
        finally:
            self._emit_status("监测已停止", "info")

    def run_weekly(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        scheduler = WeeklySchedule(self.config_file)
        try:
            while not stop_event.is_set():
                self.config = self._load_config(self.config_file)
                schedule = self.config['local_schedule']
                if not schedule.get('enabled', True):
                    break
                due = scheduler.due(schedule)
                if due is not None:
                    self.matcher.config = self.config
                    if not self.test_mode:
                        self.notifier = EmailNotifier(self.config['email'])
                    self._emit_status('执行定时抓取（包含漏跑补抓）', 'info')
                    self.check_jobs()
                    # Completed attempts, including reported failures, run once.
                    # A process interrupted mid-attempt will retry on restart.
                    scheduler.complete(schedule, datetime.now())
                stop_event.wait(15)
        finally:
            self._emit_status('监测已停止', 'info')


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='招聘监测系统')
    parser.add_argument('--config', default='config.yaml', 
                       help='配置文件路径（默认: config.yaml）')
    parser.add_argument('--test', action='store_true',
                       help='测试模式：使用控制台输出而非邮件')
    parser.add_argument('--once', action='store_true',
                       help='只运行一次检查，不循环')
    
    args = parser.parse_args()
    
    try:
        monitor = JobMonitor(args.config, args.test)
        
        if args.once:
            return 0 if monitor.run_once() else 1
        else:
            monitor.run_forever()
            
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"程序运行出错: {e}")
        return 1
    
    return 0


if __name__ == '__main__':
    exit(main())
