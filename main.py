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
from scraper import get_scraper
from matcher import JobMatcher
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
                if email_auth_code and not config.get('email', {}).get('auth_code'):
                    config.setdefault('email', {})['auth_code'] = email_auth_code
                if gemini_api_key and not config.get('gemini_api_key'):
                    config['gemini_api_key'] = gemini_api_key
                logger.info(f"配置文件加载成功: {config_file}")
                return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise
    
    def _scrape_all_sites(self) -> List[Dict]:
        """从所有配置的招聘网站爬取职位"""
        all_jobs = []
        
        keywords = self.config.get('job_keywords', [])
        cities = self.config.get('cities', [])
        job_sites = self.config.get('job_sites', [])
        
        for site_url in job_sites:
            site_label = get_site_label(self.config, site_url)
            site_mode = get_site_mode(self.config, site_url)
            scraper = get_scraper(site_url)
            if scraper:
                try:
                    logger.info(f"开始爬取网站 [{site_label}]: {site_url}")
                    
                    if site_mode == SITE_MODE_FIXED:
                        jobs = scraper.scrape_jobs(
                            site_url,
                            [],
                            cities,
                            search_mode=site_mode,
                        )
                    else:
                        jobs = scraper.scrape_jobs(site_url, keywords, cities)
                    for job in jobs:
                        if isinstance(job, dict):
                            if site_mode == SITE_MODE_FIXED:
                                job['_fixed_url_mode'] = True
                            search_keywords = job.get('_search_keywords')
                            if not isinstance(search_keywords, (list, tuple, set)):
                                search_keywords = [job.get('_search_keyword', '')]
                            job['site_label'] = combine_site_keyword_labels(
                                site_label,
                                search_keywords,
                            )
                    all_jobs.extend(jobs)
                    logger.info(f"从 [{site_label}] {site_url} 爬取到 {len(jobs)} 个职位")
                
                except Exception as e:
                    logger.error(f"爬取网站 [{site_label}] {site_url} 时出错: {e}")
        
        return all_jobs

    @staticmethod
    def _job_identity(job: Dict):
        return (
            str(job.get('url') or '').strip(),
            str(job.get('title') or '').strip(),
        )

    def _build_email_job_list(self, new_jobs: List[Dict]) -> List[Dict]:
        """读取与网页相同排序的前 50 条职位，并标记本轮新职位。"""
        new_job_keys = {
            self._job_identity(job)
            for job in new_jobs
        }
        display_jobs = []
        database = getattr(self.matcher, 'database', None)
        if getattr(self.matcher, 'persist', False) and database:
            try:
                display_jobs = database.get_all_jobs(limit=50, offset=0)
            except Exception as error:
                logger.error("读取邮件职位列表失败，将只发送本轮新职位: %s", error)

        if not display_jobs:
            display_jobs = list(new_jobs)[:50]

        result = []
        for job in display_jobs[:50]:
            email_job = dict(job)
            email_job['_is_new_this_run'] = (
                self._job_identity(email_job) in new_job_keys
            )
            result.append(email_job)
        return result

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
        """
        检查新职位并发送通知
        
        Returns:
            True表示检查成功
        """
        try:
            checked_at = datetime.now().isoformat(timespec='seconds')
            self.last_check_result = {
                'success': False,
                'checked_at': checked_at,
                'scraped_jobs': 0,
                'matched_jobs': 0,
                'notification_sent': False,
            }
            logger.info("="*60)
            logger.info("开始检查新职位...")
            logger.info("="*60)
            self._emit_status("开始检查新职位", "info")
            
            # 爬取所有职位
            all_jobs = self._scrape_all_sites()
            logger.info(f"共爬取到 {len(all_jobs)} 个职位")
            self.last_check_result['scraped_jobs'] = len(all_jobs)
            
            if not all_jobs:
                logger.info("没有找到任何职位")
                self.last_check_result['success'] = True
                self._emit_status("检查完成，没有找到职位", "warning")
                return True
            
            # 过滤符合条件的新职位
            matched_jobs = self.matcher.filter_jobs(all_jobs)
            logger.info(f"找到 {len(matched_jobs)} 个符合条件的新职位")
            self.last_check_result['matched_jobs'] = len(matched_jobs)

            callback = getattr(self, 'job_callback', None)
            if callable(callback):
                for job in matched_jobs:
                    callback(job)
            
            # 发送通知
            if matched_jobs:
                success, notified_count, failed_labels = (
                    self._send_notification_summary(matched_jobs)
                )
                self.last_check_result['notification_sent'] = notified_count > 0
                if success:
                    logger.info("职位检查完成，已发送通知")
                    self._emit_status(
                        f"检查完成，发现并通知 {notified_count} 个新职位",
                        "success"
                    )
                else:
                    logger.error(
                        "以下网址标签的邮件发送失败: %s",
                        '、'.join(failed_labels),
                    )
                    self._emit_status("发现新职位，但邮件发送失败；下次将重试", "error")
                self.last_check_result['success'] = success
                return success
            else:
                logger.info("没有发现新职位")
                self.last_check_result['success'] = True
                self._emit_status("检查完成，没有符合条件的新职位", "success")
                return True
            
        except Exception as e:
            logger.error(f"检查职位时出错: {e}")
            self.last_check_result['success'] = False
            self._emit_status(f"检查职位时出错: {e}", "error")
            return False

    def _emit_status(self, message: str, status_type: str = 'info'):
        callback = getattr(self, 'status_callback', None)
        if callable(callback):
            callback(message, status_type)
    
    def run_once(self):
        """运行一次检查"""
        self.check_jobs()
    
    def run_forever(self, stop_event: threading.Event = None):
        """持续运行监测"""
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
                if stop_event.wait(check_interval):
                    break
                
        except KeyboardInterrupt:
            logger.info("\n监测已停止")
            print("\n感谢使用招聘监测系统！")
        finally:
            self._emit_status("监测已停止", "info")


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
            monitor.run_once()
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
