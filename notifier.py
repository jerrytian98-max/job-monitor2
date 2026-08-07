"""
邮件通知模块
当发现符合条件的职位时发送邮件通知
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict
import logging
import re
from html import escape
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class EmailNotifier:
    """邮件通知器"""

    SITE_LABEL_COLORS = [
        ('#e8edff', '#4454b8', '#cbd4ff'),
        ('#e5f5ed', '#277653', '#bee6d1'),
        ('#fff0dc', '#a65d0b', '#f6d7ad'),
        ('#fbe7f0', '#a13f6d', '#f1c7da'),
        ('#ece8fb', '#624db0', '#d8cff4'),
        ('#e2f3f6', '#207386', '#bce2e9'),
        ('#f8eadf', '#885734', '#ead0bc'),
        ('#f6e7e7', '#98454d', '#e9c6c9'),
    ]
    
    def __init__(self, config: dict):
        self.sender = config.get('sender', '')
        self.auth_code = config.get('auth_code', '')
        self.receiver = config.get('receiver', '')
        self.smtp_server = config.get('smtp_server', 'smtp.gmail.com')
        self.smtp_port = config.get('smtp_port', 587)

    def _is_configured(self) -> bool:
        missing = [
            name for name, value in (
                ('发件邮箱', self.sender),
                ('邮箱授权码', self.auth_code),
                ('接收邮箱', self.receiver),
                ('SMTP 服务器', self.smtp_server),
            ) if not value
        ]
        if missing:
            logger.error("邮件配置不完整，缺少: %s", '、'.join(missing))
            return False
        return True

    @staticmethod
    def _resolve_site_label(jobs: List[Dict], site_label: str = None) -> str:
        label = str(site_label or '').strip()
        if not label:
            labels = []
            for job in jobs:
                job_label = str(job.get('site_label') or '').strip()
                if job_label and job_label not in labels:
                    labels.append(job_label)
            label = '、'.join(labels)

        # 邮件标题不能包含换行符，避免标签破坏邮件头。
        return label.replace('\r', ' ').replace('\n', ' ') or '招聘网站'

    def _build_notification_subject(
        self,
        jobs: List[Dict],
        site_label: str = None,
    ) -> str:
        label = self._resolve_site_label(jobs, site_label)
        return f'【{label}】发现了 {len(jobs)} 个新职位'

    @staticmethod
    def _safe_http_url(value: str) -> str:
        url = str(value or '').strip()
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() in ('http', 'https') and parsed.netloc:
                return url
        except (TypeError, ValueError):
            pass
        return ''

    @staticmethod
    def _display_job_date(job: Dict) -> str:
        raw_date = str(job.get('publish_time') or '').strip()
        if not raw_date or raw_date == '未知':
            return '未知'

        date_part = re.search(r'\d{4}-\d{1,2}-\d{1,2}', raw_date)
        return date_part.group(0) if date_part else raw_date

    @staticmethod
    def _display_job_title(job: Dict) -> str:
        original_title = str(job.get('title') or '未知职位').strip()
        title = re.sub(
            r'\s*职位\s*ID\s*[:：].*$',
            '',
            original_title,
            flags=re.IGNORECASE,
        ).strip()
        return title or original_title

    @classmethod
    def _site_label_color(cls, label: str):
        prefix = list(str(label or '').strip())[:2]
        if not prefix:
            return '#e9ecef', '#495057', '#ced4da'

        color_hash = 0
        for character in prefix:
            color_hash = ((color_hash * 31) + ord(character)) & 0xffffffff
        return cls.SITE_LABEL_COLORS[color_hash % len(cls.SITE_LABEL_COLORS)]
    
    def _create_email_content(
        self,
        jobs: List[Dict],
        new_job_count: int = None,
    ) -> str:
        """创建兼容常见邮箱客户端的静态职位卡片。"""
        new_count = len(jobs) if new_job_count is None else new_job_count
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
        </head>
        <body style="margin:0;padding:0;background:#f3f4f8;color:#2f3441;
                     font-family:Arial,'Microsoft YaHei',sans-serif;">
            <div style="max-width:760px;margin:0 auto;padding:24px 12px;">
                <div style="padding:22px 24px;border-radius:14px 14px 0 0;
                            background:#667eea;color:#ffffff;text-align:center;">
                    <div style="font-size:22px;font-weight:700;">爬取的职位信息</div>
                    <div style="margin-top:6px;font-size:14px;opacity:0.94;">
                        本轮发现 {new_count} 个新职位，以下为最近 50 条以内的职位
                    </div>
                </div>
        """

        # 调用方已按网页列表的数据库顺序取数，这里保持完全相同的顺序。
        for job in jobs:
            title = escape(self._display_job_title(job))
            company = escape(str(job.get('company') or '未知公司'))
            salary = escape(str(job.get('salary') or '面议'))
            city = escape(str(job.get('city') or '未知'))
            publish_time = escape(str(job.get('publish_time') or '未知'))
            found_time = escape(str(job.get('found_time') or '未知'))
            description = escape(
                str(job.get('description') or '暂无描述')
            ).replace('\n', '<br>')
            source_site = escape(str(job.get('source_site') or '未知'))
            site_label_raw = str(
                job.get('site_label')
                or job.get('source_site')
                or '未知网址'
            ).strip()
            site_label = escape(site_label_raw)
            date = escape(self._display_job_date(job))
            safe_job_url = self._safe_http_url(job.get('url'))
            job_url = escape(safe_job_url, quote=True)
            label_bg, label_text, label_border = self._site_label_color(
                site_label_raw
            )
            if job_url:
                title_html = (
                    f'<a href="{job_url}" target="_blank" '
                    'style="color:#2f3441;text-decoration:underline;'
                    f'font-weight:700;">{title}</a>'
                )
            else:
                title_html = (
                    f'<span style="color:#2f3441;font-weight:700;">'
                    f'{title}</span>'
                )
            new_badge = ''
            if job.get('_is_new_this_run'):
                new_badge = (
                    '<span style="display:inline-block;margin-left:8px;'
                    'padding:2px 8px;border-radius:12px;background:#667eea;'
                    'color:#ffffff;font-size:11px;font-weight:700;'
                    'vertical-align:middle;">新</span>'
                )

            html += f"""
                <div style="margin-top:14px;border:1px solid #dfe2eb;
                            border-radius:14px;background:#ffffff;overflow:hidden;">
                    <table role="presentation" width="100%" cellpadding="0"
                           cellspacing="0" style="border-collapse:separate;">
                        <tr>
                            <td style="padding:14px 8px 14px 16px;width:1%;
                                       white-space:nowrap;vertical-align:middle;">
                                <span style="display:inline-block;min-width:58px;
                                             max-width:130px;padding:6px 10px;
                                             border:1px solid {label_border};
                                             border-radius:9px;background:{label_bg};
                                             color:{label_text};font-size:12px;
                                             font-weight:700;text-align:center;">
                                    {site_label}
                                </span>
                            </td>
                            <td style="padding:14px 8px;vertical-align:middle;
                                       font-size:16px;line-height:1.45;">
                                {title_html}{new_badge}
                            </td>
                            <td style="padding:14px 16px 14px 8px;
                                       vertical-align:middle;text-align:right;
                                       color:#737b8c;font-size:13px;
                                       white-space:nowrap;">
                                {date}
                            </td>
                        </tr>
                    </table>
                    <div style="border-top:1px solid #eceef5;padding:15px 16px;
                                background:#fbfbfd;">
                        <table role="presentation" width="100%" cellpadding="0"
                               cellspacing="0" style="border-collapse:collapse;">
                            <tr>
                                <td width="33%" style="padding:4px 12px 10px 0;
                                                     vertical-align:top;">
                                    <div style="color:#8a91a0;font-size:12px;">公司</div>
                                    <div style="color:#343a40;font-size:14px;">{company}</div>
                                </td>
                                <td width="33%" style="padding:4px 12px 10px 0;
                                                     vertical-align:top;">
                                    <div style="color:#8a91a0;font-size:12px;">地点</div>
                                    <div style="color:#343a40;font-size:14px;">{city}</div>
                                </td>
                                <td width="34%" style="padding:4px 0 10px;
                                                     vertical-align:top;">
                                    <div style="color:#8a91a0;font-size:12px;">薪资</div>
                                    <div style="color:#277653;font-size:14px;
                                                font-weight:700;">{salary}</div>
                                </td>
                            </tr>
                            <tr>
                                <td width="33%" style="padding:4px 12px 10px 0;
                                                     vertical-align:top;">
                                    <div style="color:#8a91a0;font-size:12px;">来源网站</div>
                                    <div style="color:#343a40;font-size:14px;">{source_site}</div>
                                </td>
                                <td width="33%" style="padding:4px 12px 10px 0;
                                                     vertical-align:top;">
                                    <div style="color:#8a91a0;font-size:12px;">发布时间</div>
                                    <div style="color:#343a40;font-size:14px;">{publish_time}</div>
                                </td>
                                <td width="34%" style="padding:4px 0 10px;
                                                     vertical-align:top;">
                                    <div style="color:#8a91a0;font-size:12px;">发现时间</div>
                                    <div style="color:#343a40;font-size:14px;">{found_time}</div>
                                </td>
                            </tr>
                        </table>
                        <div style="margin-top:2px;color:#555c68;font-size:14px;
                                    line-height:1.65;">{description}</div>
                    </div>
                </div>
            """

        html += f"""
                <div style="padding:22px 8px 8px;text-align:center;
                            color:#7f8795;font-size:12px;line-height:1.7;">
                    <div>邮件发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
                    <div>点击职位名称可直接打开招聘网站的岗位页面</div>
                </div>
            </div>
        </body>
        </html>
        """
        
        return html
    
    def send_notification(
        self,
        jobs: List[Dict],
        site_label: str = None,
        new_jobs: List[Dict] = None,
    ) -> bool:
        """
        发送邮件通知
        
        Args:
            jobs: 职位列表
        
        Returns:
            True表示发送成功，False表示发送失败
        """
        notification_jobs = jobs if new_jobs is None else new_jobs
        if not notification_jobs:
            logger.info("没有新职位需要发送通知")
            return True
        if not self._is_configured():
            return False
        
        try:
            # 创建邮件
            msg = MIMEMultipart('alternative')
            msg['Subject'] = self._build_notification_subject(
                notification_jobs,
                site_label,
            )
            msg['From'] = self.sender
            msg['To'] = self.receiver
            
            # 添加HTML内容
            html_content = self._create_email_content(
                jobs,
                new_job_count=len(notification_jobs),
            )
            html_part = MIMEText(html_content, 'html', 'utf-8')
            msg.attach(html_part)
            
            # 发送邮件
            logger.info(f"正在发送邮件到 {self.receiver}...")
            
            # 根据端口选择不同的连接方式
            if int(self.smtp_port) == 465:
                # 465 端口使用 SSL
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30) as server:
                    server.login(self.sender, self.auth_code)
                    server.send_message(msg)
            else:
                # 其他端口（如 587, 25）使用普通连接后启动 TLS
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
                    server.starttls()  # 启用TLS加密
                    server.login(self.sender, self.auth_code)
                    server.send_message(msg)
            
            logger.info(f"邮件发送成功！共发送 {len(jobs)} 个职位信息")
            return True
            
        except Exception as e:
            logger.error(f"发送邮件失败: {e}")
            return False

    def send_test_email(self, target_email: str = None) -> bool:
        """
        发送测试邮件
        
        Args:
            target_email: 目标邮箱地址，如果未提供则使用配置中的接收邮箱
            
        Returns:
            True表示发送成功，False表示发送失败
        """
        receiver = target_email if target_email else self.receiver
        
        if not receiver:
            logger.error("未配置接收邮箱，无法发送测试邮件")
            return False
        if not self.sender or not self.auth_code or not self.smtp_server:
            logger.error("邮件配置不完整，无法发送测试邮件")
            return False
            
        try:
            msg = MIMEMultipart()
            msg['Subject'] = '招聘监测系统 - 邮件配置测试'
            msg['From'] = self.sender
            msg['To'] = receiver
            
            content = f"""
            您好！
            
            这是一封来自招聘监测系统的测试邮件。
            如果您收到了这封邮件，说明您的邮件配置是正确的！
            
            发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
            
            msg.attach(MIMEText(content, 'plain', 'utf-8'))
            
            logger.info(f"正在发送测试邮件到 {receiver}...")
            
            # 根据端口选择不同的连接方式
            if int(self.smtp_port) == 465:
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30) as server:
                    server.login(self.sender, self.auth_code)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
                    server.starttls()
                    server.login(self.sender, self.auth_code)
                    server.send_message(msg)
                
            logger.info("测试邮件发送成功")
            return True
            
        except Exception as e:
            logger.error(f"发送测试邮件失败: {e}")
            return False


class ConsoleNotifier:
    """控制台通知器（用于测试）"""
    
    def send_notification(
        self,
        jobs: List[Dict],
        site_label: str = None,
        new_jobs: List[Dict] = None,
    ) -> bool:
        """
        在控制台输出职位信息
        
        Args:
            jobs: 职位列表
        
        Returns:
            True表示输出成功
        """
        notification_jobs = jobs if new_jobs is None else new_jobs
        if not notification_jobs:
            print("没有发现新职位")
            return True
        
        print("\n" + "="*60)
        label = EmailNotifier._resolve_site_label(notification_jobs, site_label)
        print(f"【{label}】发现了 {len(notification_jobs)} 个新职位")
        print("="*60 + "\n")
        
        for i, job in enumerate(notification_jobs, 1):
            print(f"{i}. {job.get('title', '')}")
            print(f"   公司: {job.get('company', '')}")
            print(f"   薪资: {job.get('salary', '面议')}")
            print(f"   城市: {job.get('city', '')}")
            print(f"   发布时间: {job.get('publish_time', '')}")
            print(f"   链接: {job.get('url', '')}")
            print("-"*60 + "\n")
        
        return True
