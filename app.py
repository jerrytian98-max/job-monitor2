"""
招聘监测系统 - Flask Web应用
提供Web界面管理监测配置和查看监测状态
"""

from flask import Flask, render_template, jsonify, request, Response
import yaml

import os
from config_bootstrap import ensure_config_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.environ.get('JOB_PROFILE', '')
PROFILE_SUFFIX = f'_{PROFILE}' if PROFILE else ''
CONFIG_FILE = ensure_config_file(
    os.path.join(BASE_DIR, f'config{PROFILE_SUFFIX}.yaml')
)
LOG_FILE = os.path.join(BASE_DIR, f'app{PROFILE_SUFFIX}.log')

import threading
from datetime import datetime
from main import JobMonitor
from database import db
from matcher import CACHE_FILE
import logging
import queue
import json
import copy
import math
from urllib.parse import urlparse
from scraper_with_callback import get_scraper_with_callback
from notifier import EmailNotifier
from site_labels import (
    build_site_labels,
    build_site_modes,
    combine_site_keyword_labels,
    get_site_label,
    get_site_mode,
    SITE_MODE_FIXED,
)
from github_sync import (
    GitHubSyncError,
    download_latest_job_state,
    public_settings as public_github_settings,
    save_settings as save_github_settings,
    upload_local_configuration,
)

# 配置日志 - 同时输出到文件和控制台
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ],
    force=True,
)
logger = logging.getLogger(__name__)

app = Flask(__name__,
           template_folder='templates',
           static_folder='static')

# 全局变量
monitor = None
monitor_thread = None
is_monitoring = False
monitor_stop_event = threading.Event()
monitor_lock = threading.Lock()
github_sync_lock = threading.Lock()
is_syncing_job_data = False
monitor_status = {
    'status': 'stopped',
    'last_check': None,
    'total_jobs_found': 0,
    'new_jobs_today': 0,
    'running_time': None
}

# 分页设置
JOBS_PER_PAGE = 10
SECRET_MASK = '••••••••'

# 事件队列（用于实时推送）
event_queue = queue.Queue()


def load_config():
    """加载配置文件"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
                return config if isinstance(config, dict) else {}
        return {}
    except Exception as e:
        logger.error(f"加载配置失败: {e}")
        return {}


def save_config(config):
    """保存配置文件"""
    try:
        temp_file = f"{CONFIG_FILE}.tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        os.replace(temp_file, CONFIG_FILE)
        return True
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return False


def validate_config(config: dict) -> dict:
    """验证并规范化来自 Web 界面的配置。"""
    if not isinstance(config, dict):
        raise ValueError("配置必须是 JSON 对象")

    required = ('job_keywords', 'cities', 'exclude_keywords', 'job_sites', 'email', 'check_interval')
    missing = [field for field in required if field not in config]
    if missing:
        raise ValueError(f"缺少字段: {', '.join(missing)}")

    normalized = dict(config)
    for field in ('job_keywords', 'cities', 'exclude_keywords', 'job_sites'):
        value = normalized.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field} 必须是字符串列表")
        normalized[field] = list(dict.fromkeys(item.strip() for item in value if item.strip()))

    for url in normalized['job_sites']:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError(f"无效的招聘网址: {url}")

    raw_site_labels = normalized.get('job_site_labels', {})
    if raw_site_labels is not None and not isinstance(raw_site_labels, dict):
        raise ValueError("job_site_labels 必须是网址到标签的映射")
    normalized['job_site_labels'] = build_site_labels(
        normalized['job_sites'],
        raw_site_labels,
    )

    raw_site_modes = normalized.get('job_site_modes', {})
    if raw_site_modes is not None and not isinstance(raw_site_modes, dict):
        raise ValueError("job_site_modes 必须是网址到抓取方式的映射")
    invalid_modes = [
        url
        for url, mode in (raw_site_modes or {}).items()
        if url in normalized['job_sites']
        and str(mode or '').strip().lower() not in ('', 'search', 'fixed')
    ]
    if invalid_modes:
        raise ValueError("抓取方式只能是 search 或 fixed")
    normalized['job_site_modes'] = build_site_modes(
        normalized['job_sites'],
        raw_site_modes,
    )

    try:
        interval = float(normalized.get('check_interval', 2))
    except (TypeError, ValueError):
        raise ValueError("监测间隔必须是数字")
    if not 1 / 60 <= interval <= 168:
        raise ValueError("监测间隔必须在 1 分钟到 168 小时之间")
    normalized['check_interval'] = interval

    email = normalized.get('email', {})
    if not isinstance(email, dict):
        raise ValueError("email 必须是对象")
    normalized_email = {
        'sender': str(email.get('sender', '')).strip(),
        'auth_code': str(email.get('auth_code', '')).strip(),
        'receiver': str(email.get('receiver', '')).strip(),
        'smtp_server': str(email.get('smtp_server', '')).strip(),
    }
    try:
        normalized_email['smtp_port'] = int(email.get('smtp_port', 587))
    except (TypeError, ValueError):
        raise ValueError("SMTP 端口必须是整数")
    if not 1 <= normalized_email['smtp_port'] <= 65535:
        raise ValueError("SMTP 端口超出有效范围")
    normalized['email'] = normalized_email

    normalized['gemini_api_key'] = str(normalized.get('gemini_api_key', '')).strip()
    normalized['ai_filter_prompt'] = str(normalized.get('ai_filter_prompt', '')).strip()
    normalized['gemini_model'] = str(
        normalized.get('gemini_model', 'gemini-3.5-flash-lite')
    ).strip() or 'gemini-3.5-flash-lite'
    return normalized


def redact_config(config: dict) -> dict:
    """返回适合发送到浏览器的配置副本。"""
    public_config = copy.deepcopy(config)
    public_config['job_site_labels'] = build_site_labels(
        public_config.get('job_sites', []),
        public_config.get('job_site_labels', {}),
    )
    public_config['job_site_modes'] = build_site_modes(
        public_config.get('job_sites', []),
        public_config.get('job_site_modes', {}),
    )
    if public_config.get('gemini_api_key'):
        public_config['gemini_api_key'] = SECRET_MASK
    email = public_config.get('email')
    if isinstance(email, dict) and email.get('auth_code'):
        email['auth_code'] = SECRET_MASK
    return public_config


def preserve_masked_secrets(config: dict, existing: dict) -> dict:
    """浏览器提交掩码时保留磁盘上的原值。"""
    if config.get('gemini_api_key') == SECRET_MASK:
        config['gemini_api_key'] = existing.get('gemini_api_key', '')
    email = config.get('email')
    existing_email = existing.get('email', {})
    if isinstance(email, dict) and email.get('auth_code') == SECRET_MASK:
        email['auth_code'] = existing_email.get('auth_code', '')
    return config


def unique_site_labels_by_domain(config: dict) -> dict:
    """返回域名到唯一网址标签的映射；同域多标签时不做猜测。"""
    job_sites = config.get('job_sites', []) if isinstance(config, dict) else []
    labels = build_site_labels(
        job_sites,
        config.get('job_site_labels', {}) if isinstance(config, dict) else {},
    )
    grouped = {}
    for site_url in job_sites:
        domain = urlparse(site_url).netloc.lower()
        label = labels.get(site_url, '').strip()
        if domain and label:
            grouped.setdefault(domain, set()).add(label)
    return {
        domain: next(iter(domain_labels))
        for domain, domain_labels in grouped.items()
        if len(domain_labels) == 1
    }


def send_event(event_type: str, data: dict):
    """发送事件到队列"""
    event = {
        'type': event_type,
        'data': data,
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }
    try:
        event_queue.put_nowait(event)
    except queue.Full:
        pass  # 队列满时忽略


# 全局回调函数
def status_callback(
    message: str,
    status_type: str,
    work_log: bool = False,
):
    """状态更新回调"""
    send_event(
        'status',
        {
            'message': message,
            'type': status_type,
            'work_log': bool(work_log),
        },
    )


def job_callback(job: dict):
    """职位发现回调"""
    send_event('job_found', job)


class WebJobMonitor(JobMonitor):
    """
    Web环境下的监测器，支持回调
    """
    def __init__(self, config_file: str = CONFIG_FILE, test_mode: bool = False):
        super().__init__(config_file, test_mode)
        self.status_callback = status_callback
        self.job_callback = job_callback

    def _scrape_all_sites(self) -> list:
        """抓取全部配置网址；单个网址失败不影响后续网址。"""
        all_jobs = []
        
        keywords = self.config.get('job_keywords', [])
        cities = self.config.get('cities', [])
        job_sites = self.config.get('job_sites', [])
        
        for site_url in job_sites:
            site_label = get_site_label(self.config, site_url)
            site_mode = get_site_mode(self.config, site_url)
            scraper = get_scraper_with_callback(site_url, None, None)
            if scraper:
                try:
                    logger.info(f"开始抓取目标网址 [{site_label}]: {site_url}")
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
                    logger.info(
                        f"从 [{site_label}] {site_url} 抓取到 {len(jobs)} 个匹配职位"
                    )
                
                except Exception as e:
                    logger.error(f"抓取目标网址 [{site_label}] {site_url} 时出错: {e}")
        
        return all_jobs

    def check_jobs(self) -> bool:
        """逐个网址抓取和判定新职位，确保每个网址都完整执行。"""
        checked_at = datetime.now().isoformat(timespec='seconds')
        self.last_check_result = {
            'success': False,
            'checked_at': checked_at,
            'scraped_jobs': 0,
            'matched_jobs': 0,
            'notification_sent': False,
        }

        matched_jobs = []
        scraped_total = 0
        seen_jobs = set()

        try:
            keywords = self.config.get('job_keywords', [])
            cities = self.config.get('cities', [])
            job_sites = list(dict.fromkeys(self.config.get('job_sites', [])))

            for site_url in job_sites:
                site_label = get_site_label(self.config, site_url)
                site_mode = get_site_mode(self.config, site_url)
                site_new_jobs = []
                self.status_callback(
                    f"正在抓取【{site_label}】",
                    "info",
                    True,
                )

                try:
                    scraper = get_scraper_with_callback(site_url, None, None)
                    if not scraper:
                        raise RuntimeError("无法创建招聘网站抓取器")

                    if site_mode == SITE_MODE_FIXED:
                        jobs = scraper.scrape_jobs(
                            site_url,
                            [],
                            cities,
                            search_mode=site_mode,
                        ) or []
                    else:
                        jobs = scraper.scrape_jobs(site_url, keywords, cities) or []
                    scraped_total += len(jobs)

                    unique_jobs = []
                    for job in jobs:
                        if not isinstance(job, dict):
                            continue
                        if site_mode == SITE_MODE_FIXED:
                            job['_fixed_url_mode'] = True
                        search_keywords = job.get('_search_keywords')
                        if not isinstance(search_keywords, (list, tuple, set)):
                            search_keywords = [job.get('_search_keyword', '')]
                        job['site_label'] = combine_site_keyword_labels(
                            site_label,
                            search_keywords,
                        )
                        job_key = (
                            str(job.get('url') or '').strip(),
                            str(job.get('title') or '').strip(),
                        )
                        if job_key in seen_jobs:
                            continue
                        seen_jobs.add(job_key)
                        unique_jobs.append(job)

                    site_new_jobs = self.matcher.filter_jobs(unique_jobs)
                    matched_jobs.extend(site_new_jobs)

                    if callable(self.job_callback):
                        for job in site_new_jobs:
                            self.job_callback(job)

                    logger.info(
                        "抓取完毕 [%s]，找到 %s 个新职位",
                        site_label,
                        len(site_new_jobs),
                    )
                except Exception as e:
                    logger.error(
                        "抓取目标网址 [%s] %s 时出错: %s",
                        site_label,
                        site_url,
                        e,
                    )
                    self._emit_status(
                        f"抓取【{site_label}】发生错误，已继续下一个网址",
                        "error",
                    )
                finally:
                    self.status_callback(
                        f"抓取完毕【{site_label}】找到{len(site_new_jobs)}个新职位",
                        "success",
                        True,
                    )

            self.last_check_result['scraped_jobs'] = scraped_total
            self.last_check_result['matched_jobs'] = len(matched_jobs)

            if matched_jobs:
                success, notified_count, failed_labels = (
                    self._send_notification_summary(matched_jobs)
                )
                self.last_check_result['notification_sent'] = notified_count > 0
                if success:
                    self._emit_status(
                        f"本轮监测完成，共发现并通知{notified_count}个新职位",
                        "success",
                    )
                else:
                    logger.error(
                        "以下网址标签的邮件发送失败: %s",
                        '、'.join(failed_labels),
                    )
                    self._emit_status(
                        "发现新职位，但邮件发送失败；下次将重试",
                        "error",
                    )
                self.last_check_result['success'] = success
                return success

            self.last_check_result['success'] = True
            self._emit_status("本轮监测完成，没有新职位", "success")
            return True
        except Exception as e:
            logger.error(f"检查职位时出错: {e}")
            self.last_check_result['success'] = False
            self._emit_status(f"检查职位时出错: {e}", "error")
            return False
        finally:
            stats = db.get_statistics()
            monitor_status.update({
                'last_check': self.last_check_result.get('checked_at'),
                'total_jobs_found': stats.get('total_jobs', 0),
                'new_jobs_today': stats.get('new_jobs_today', 0),
                'last_scraped_jobs': self.last_check_result.get('scraped_jobs', 0),
                'last_matched_jobs': self.last_check_result.get('matched_jobs', 0),
            })


def run_monitor():
    """在后台运行监测"""
    global monitor, is_monitoring, monitor_status
    
    try:
        is_monitoring = True
        monitor_status['status'] = 'running'
        monitor_status['running_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 使用 WebJobMonitor
        monitor = WebJobMonitor(CONFIG_FILE, test_mode=False)
        monitor.run_forever(monitor_stop_event)
        
    except Exception as e:
        logger.error(f"监测出错: {e}")
        status_callback(f"监测出错: {e}", "error")
    finally:
        with monitor_lock:
            is_monitoring = False
            monitor_status['status'] = 'stopped'
            monitor = None


# 路由

@app.route('/')
def index():
    """主页"""
    config = load_config()
    return render_template('index.html', config=redact_config(config), status=monitor_status)


@app.route('/api/config', methods=['GET'])
def get_config():
    """获取配置"""
    config = load_config()
    return jsonify({'success': True, 'data': redact_config(config)})


@app.route('/api/config', methods=['POST'])
def update_config():
    """更新配置"""
    try:
        existing = load_config()
        submitted = request.get_json(silent=True)
        if isinstance(submitted, dict):
            submitted = preserve_masked_secrets(submitted, existing)
        config = validate_config(submitted)
        
        # 保存配置
        if save_config(config):
            logger.info("配置已更新")
            return jsonify({'success': True, 'message': '配置保存成功'})
        else:
            return jsonify({'success': False, 'message': '保存配置失败'})
            
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/github-sync/settings', methods=['GET'])
def get_github_sync_settings():
    """Return masked local GitHub synchronization settings."""
    try:
        return jsonify({'success': True, 'data': public_github_settings()})
    except GitHubSyncError as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/github-sync/settings', methods=['POST'])
def update_github_sync_settings():
    """Save repository and token locally without returning the raw token."""
    try:
        data = request.get_json(silent=True) or {}
        settings = save_github_settings(
            data.get('repository', ''),
            data.get('token', ''),
        )
        logger.info("GitHub 同步设置已保存: %s", settings['repository'])
        return jsonify({
            'success': True,
            'message': 'GitHub 连接设置已保存',
            'data': settings,
        })
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except GitHubSyncError as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/github-sync/upload-config', methods=['POST'])
def upload_config_to_github():
    """Upload all local profiles to the repository configuration Secret."""
    if not github_sync_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': '已有 GitHub 同步任务正在运行'}), 409
    try:
        result = upload_local_configuration()
        logger.info("本地配置已同步到 GitHub 仓库: %s", result['repository'])
        return jsonify({
            'success': True,
            'message': '本地配置已上传到 GitHub',
            'data': result,
        })
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except GitHubSyncError as e:
        logger.warning("上传 GitHub 配置失败: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 502
    finally:
        github_sync_lock.release()


@app.route('/api/github-sync/download-jobs', methods=['POST'])
def download_jobs_from_github():
    """Download the newest Actions artifact and safely restore local job state."""
    global is_syncing_job_data

    with monitor_lock:
        if is_monitoring:
            return jsonify({
                'success': False,
                'message': '请先停止本地监测，再下载 GitHub 职位数据',
            }), 409
        if is_syncing_job_data:
            return jsonify({'success': False, 'message': '职位数据正在同步'}), 409
        is_syncing_job_data = True

    if not github_sync_lock.acquire(blocking=False):
        with monitor_lock:
            is_syncing_job_data = False
        return jsonify({'success': False, 'message': '已有 GitHub 同步任务正在运行'}), 409

    try:
        result = download_latest_job_state()
        total_jobs = sum(result.get('job_counts', {}).values())
        logger.info(
            "已从 GitHub 同步职位数据: %s，共 %s 条",
            result['repository'],
            total_jobs,
        )
        return jsonify({
            'success': True,
            'message': f'已从 GitHub 下载职位数据，共 {total_jobs} 条',
            'data': result,
        })
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except GitHubSyncError as e:
        logger.warning("下载 GitHub 职位数据失败: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 502
    finally:
        github_sync_lock.release()
        with monitor_lock:
            is_syncing_job_data = False


@app.route('/api/monitor/start', methods=['POST'])
def start_monitor():
    """启动监测"""
    global monitor_thread, is_monitoring

    try:
        config = validate_config(load_config())
        if not config.get('job_sites'):
            return jsonify({'success': False, 'message': '请先至少添加一个招聘网址'}), 400

        with monitor_lock:
            if is_monitoring:
                return jsonify({'success': False, 'message': '监测已在运行中'})
            if is_syncing_job_data:
                return jsonify({
                    'success': False,
                    'message': '职位数据正在同步，请稍后再启动监测',
                }), 409
            is_monitoring = True
            monitor_status['status'] = 'starting'
            monitor_stop_event.clear()

        # 在新线程中启动监测
        monitor_thread = threading.Thread(target=run_monitor, daemon=True)
        monitor_thread.start()
        
        logger.info("监测已启动")
        return jsonify({'success': True, 'message': '监测已启动'})
        
    except Exception as e:
        with monitor_lock:
            is_monitoring = False
            monitor_status['status'] = 'stopped'
        logger.error(f"启动监测失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/monitor/stop', methods=['POST'])
def stop_monitor():
    """停止监测"""
    global is_monitoring

    try:
        with monitor_lock:
            if not is_monitoring:
                return jsonify({'success': False, 'message': '监测未运行'})
            monitor_status['status'] = 'stopping'
            monitor_stop_event.set()

        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=2)

        stopped = not monitor_thread or not monitor_thread.is_alive()
        message = '监测已停止' if stopped else '正在停止，当前抓取完成后退出'
        logger.info(message)
        return jsonify({'success': True, 'message': message, 'stopped': stopped})
        
    except Exception as e:
        logger.error(f"停止监测失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/monitor/status', methods=['GET'])
def get_status():
    """获取监测状态"""
    monitor_status['is_monitoring'] = is_monitoring
    stats = db.get_statistics()
    monitor_status['total_jobs_found'] = stats.get('total_jobs', 0)
    monitor_status['new_jobs_today'] = stats.get('new_jobs_today', 0)
    
    # 如果正在监测，更新运行时间
    if is_monitoring and monitor_status['running_time']:
        start_time = datetime.strptime(monitor_status['running_time'], '%Y-%m-%d %H:%M:%S')
        elapsed = datetime.now() - start_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        monitor_status['elapsed_time'] = f"{hours}小时{minutes}分钟"
    elif not is_monitoring:
        monitor_status['elapsed_time'] = None
    
    return jsonify({'success': True, 'data': monitor_status})


@app.route('/api/test/check', methods=['POST'])
def test_check():
    """测试检查一次"""
    try:
        config = validate_config(load_config())
        if not config.get('job_sites'):
            return jsonify({'success': False, 'message': '请先至少添加一个招聘网址'}), 400

        test_monitor = WebJobMonitor(CONFIG_FILE, test_mode=True)
        success = test_monitor.check_jobs()

        if success:
            return jsonify({
                'success': True,
                'message': '测试检查完成（未发送邮件、未写入数据库）',
                'data': test_monitor.last_check_result,
            })
        else:
            return jsonify({'success': False, 'message': '测试检查失败'})

    except Exception as e:
        logger.error(f"测试检查失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    """获取职位列表"""
    try:
        page = max(1, int(request.args.get('page', 1)))
        keyword = request.args.get('keyword', '').strip()[:200]
        site_label_keyword = request.args.get('site_label', '').strip()[:100]
        config = load_config()
        source_label_map = unique_site_labels_by_domain(config)
        db.backfill_site_labels(source_label_map)

        total = db.count_jobs(keyword, site_label_keyword)
        total_pages = math.ceil(total / JOBS_PER_PAGE) if total else 0
        if total_pages:
            page = min(page, total_pages)
        offset = (page - 1) * JOBS_PER_PAGE

        # 搜索或获取全部
        if keyword:
            jobs = db.search_jobs(
                keyword,
                site_label_keyword,
                limit=JOBS_PER_PAGE,
                offset=offset,
            )
        else:
            jobs = db.get_all_jobs(
                limit=JOBS_PER_PAGE,
                offset=offset,
                site_label_keyword=site_label_keyword,
            )

        for job in jobs:
            job['site_label'] = (
                str(job.get('site_label') or '').strip()
                or source_label_map.get(str(job.get('source_site') or '').strip().lower(), '')
                or str(job.get('source_site') or '').strip()
                or '未知网址'
            )

        return jsonify({
            'success': True,
            'data': jobs,
            'pagination': {
                'page': page,
                'per_page': JOBS_PER_PAGE,
                'total': total,
                'total_pages': total_pages,
            },
        })

    except Exception as e:
        logger.error(f"获取职位列表失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/jobs/<int:job_id>', methods=['GET'])
def get_job(job_id):
    """获取单个职位详情。"""
    try:
        job = db.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'message': '职位不存在'}), 404
        return jsonify({'success': True, 'data': job})
    except Exception as e:
        logger.error(f"获取职位详情失败: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/jobs/clear', methods=['POST'])
def clear_jobs():
    """清除职位记录"""
    try:
        # 检查是否是完全清除
        clear_all = request.args.get('all', 'false').lower() == 'true'
        
        if clear_all:
            cleared = db.clear_all_jobs()
            try:
                with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump({}, f)
            except OSError as e:
                logger.warning(f"清除兼容缓存失败: {e}")
            message = f'已清除所有 {cleared} 条职位记录'
        else:
            days = int(request.args.get('days', 30))
            cleared = db.clear_old_jobs(days)
            message = f'已清除 {cleared} 条旧职位记录'

        return jsonify({'success': True, 'message': message})

    except Exception as e:
        logger.error(f"清除职位失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/statistics', methods=['GET'])
def get_statistics():
    """获取统计信息"""
    try:
        stats = db.get_statistics()
        return jsonify({'success': True, 'data': stats})

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/events')
def events():
    """Server-Sent Events - 实时推送工作状态"""
    def generate():
        while True:
            try:
                # 从队列获取事件
                event = event_queue.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                # 发送心跳
                yield "data: {\"type\": \"heartbeat\"}\n\n"
            except GeneratorExit:
                break

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/email/test', methods=['POST'])
def test_email():
    """发送测试邮件"""
    try:
        config = load_config()
        email_config = config.get('email', {})
        
        if not email_config:
            return jsonify({'success': False, 'message': '未找到邮件配置，请先保存配置'})
            
        data = request.get_json(silent=True) or {}
        target_email = data.get('email') or email_config.get('receiver')
        
        if not target_email:
            return jsonify({'success': False, 'message': '请提供接收邮箱或在配置中设置'})
            
        notifier = EmailNotifier(email_config)
        success = notifier.send_test_email(target_email)
        
        if success:
            return jsonify({'success': True, 'message': f'测试邮件已发送至 {target_email}'})
        else:
            return jsonify({'success': False, 'message': '发送失败，请检查邮箱配置和授权码'})
            
    except Exception as e:
        logger.error(f"测试邮件失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


if __name__ == '__main__':
    # 确保必要目录存在
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static', exist_ok=True)
    
    print("="*60)
    print("招聘监测系统 Web界面")
    print("="*60)
    port = int(os.environ.get("FLASK_PORT", 5000))
    print(f"访问地址: http://127.0.0.1:{port}")
    print("="*60)
    print()
    
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host='127.0.0.1', port=port, debug=debug, use_reloader=debug)
