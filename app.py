"""
招聘监测系统 - Flask Web应用
提供Web界面管理监测配置和查看监测状态
"""

from flask import Flask, render_template, jsonify, request, Response
import yaml

import os
from config_bootstrap import ensure_config_file
from local_schedule import WeeklySchedule
from job_tags import job_listing
from profile_config import (
    config_filename,
    normalize_profile,
    profile_display_name,
    profile_suffix,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = normalize_profile(os.environ.get('JOB_PROFILE', ''))
PROFILE_SUFFIX = profile_suffix(PROFILE)
PROFILE_DISPLAY_NAME = profile_display_name(PROFILE)
CONFIG_FILE = ensure_config_file(os.environ.get(
    'JOB_MONITOR_CONFIG_FILE', os.path.join(BASE_DIR, config_filename(PROFILE))
))
LOG_FILE = os.path.join(
    os.environ.get('JOB_MONITOR_DATA_DIR') or BASE_DIR,
    f'app{PROFILE_SUFFIX}.log',
)

import threading
from datetime import datetime
from main import JobMonitor
from database import db
from matcher import CACHE_FILE
import logging
from event_stream import EventHistory, encode_event_batch
import json
import math
from scraper_with_callback import get_scraper_with_callback
from notifier import EmailNotifier
from github_sync import (
    GitHubSyncError,
    download_latest_job_state,
    public_settings as public_github_settings,
    save_settings as save_github_settings,
    upload_local_configuration,
)
from web_config import (
    JOBS_PER_PAGE, SECRET_MASK, validate_config, redact_config,
    preserve_masked_secrets, unique_site_labels_by_domain, update_ai_filter_config,
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

# Each tab reads with its own cursor; bounded history replaces a shared queue.
event_history = EventHistory()


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
        if config.get('local_schedule'):
            WeeklySchedule(CONFIG_FILE).configure(config['local_schedule'])
        return True
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return False




def send_event(event_type: str, data: dict):
    """Publish once; every browser tab can independently read the event."""
    event = {
        'type': event_type,
        'data': data,
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }
    event_history.publish(event)


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
    """Use the shared monitor pipeline with Web callbacks and statistics."""

    def __init__(self, config_file: str = CONFIG_FILE, test_mode: bool = False):
        super().__init__(config_file, test_mode)
        self.status_callback = status_callback
        self.job_callback = job_callback

    def _create_scraper(self, site_url):
        return get_scraper_with_callback(site_url, None, None)

    def _after_check(self):
        stats = ({
            'total_jobs': monitor_status.get('total_jobs_found', 0),
            'new_jobs_today': monitor_status.get('new_jobs_today', 0),
        } if getattr(self, 'test_mode', False) else db.get_statistics())
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
    return render_template(
        'index.html',
        config=redact_config(config),
        status=monitor_status,
        profile_display_name=PROFILE_DISPLAY_NAME,
    )


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
            if config.get('local_schedule', {}).get('enabled') and not is_monitoring and config.get('job_sites'):
                start_monitor()
            return jsonify({'success': True, 'message': '配置保存成功'})
        else:
            return jsonify({'success': False, 'message': '保存配置失败'})
            
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/ai-filter', methods=['POST'])
def update_ai_filter():
    try:
        updated = update_ai_filter_config(load_config(), request.get_json(silent=True))
        if not save_config(updated):
            return jsonify(success=False, message='保存失败'), 500
        return jsonify(success=True, message='AI 筛选设置已保存')
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400


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
    """Upload only the current user's private configuration Secret."""
    if not github_sync_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': '已有 GitHub 同步任务正在运行'}), 409
    try:
        result = upload_local_configuration(profile=PROFILE)
        logger.info(
            "%s 的本地配置已同步到 GitHub 仓库: %s",
            PROFILE_DISPLAY_NAME,
            result['repository'],
        )
        return jsonify({
            'success': True,
            'message': f'{PROFILE_DISPLAY_NAME} 的本地配置已上传到 GitHub',
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
        result = download_latest_job_state(profile=PROFILE)
        total_jobs = sum(result.get('job_counts', {}).values())
        logger.info(
            "已从 GitHub 同步 %s 的职位数据: %s，共 %s 条",
            PROFILE_DISPLAY_NAME,
            result['repository'],
            total_jobs,
        )
        return jsonify({
            'success': True,
            'message': f'已下载{PROFILE_DISPLAY_NAME}的职位数据，共 {total_jobs} 条',
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

        if config.get('local_schedule') and not config['local_schedule']['enabled']:
            config['local_schedule']['enabled'] = True
            if not save_config(config):
                raise ValueError('无法保存运行计划')

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
            config = load_config()
            if config.get('local_schedule'):
                config['local_schedule']['enabled'] = False
                if not save_config(config):
                    raise ValueError('无法保存暂停状态')
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
        return jsonify(job_listing(db, load_config(), request.args, JOBS_PER_PAGE))

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
    """Return a finite event batch; the browser resumes with its own cursor."""
    cursor, records = event_history.read(request.headers.get('Last-Event-ID'))
    return Response(
        encode_event_batch(records, cursor), mimetype='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
    )


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
    saved = load_config()
    if saved.get('local_schedule', {}).get('enabled') and saved.get('job_sites'):
        with app.app_context():
            start_monitor()
    app.run(host='127.0.0.1', port=port, debug=debug, use_reloader=False)
