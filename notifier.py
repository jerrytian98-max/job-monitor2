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
        """创建面向网易邮箱的可折叠职位卡片。"""
        new_count = len(jobs) if new_job_count is None else new_job_count
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                .job-card summary::-webkit-details-marker {{ display:none; }}
                .job-card summary::marker {{ content:""; }}
                .job-card[open] .job-collapsed-icon {{ display:none !important; }}
                .job-card[open] .job-expanded-icon {{ display:inline !important; }}
            </style>
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
                <details class="job-card"
                         style="display:block;margin-top:14px;border:1px solid #dfe2eb;
                                border-radius:14px;background:#ffffff;overflow:hidden;">
                    <summary style="display:block;padding:0;cursor:pointer;
                                    list-style:none;outline:none;">
                        <span role="presentation"
                              style="display:table;width:100%;border-collapse:separate;">
                            <span style="display:table-cell;padding:14px 8px 14px 16px;
                                         width:1%;white-space:nowrap;vertical-align:middle;">
                                <span style="display:inline-block;min-width:58px;
                                             max-width:130px;padding:6px 10px;
                                             border:1px solid {label_border};
                                             border-radius:9px;background:{label_bg};
                                             color:{label_text};font-size:12px;
                                             font-weight:700;text-align:center;">
                                    {site_label}
                                </span>
                            </span>
                            <span style="display:table-cell;padding:14px 8px;
                                         vertical-align:middle;font-size:16px;
                                         line-height:1.45;text-align:left;">
                                {title_html}{new_badge}
                            </span>
                            <span style="display:table-cell;padding:14px 8px;
                                         width:1%;vertical-align:middle;text-align:right;
                                         color:#737b8c;font-size:13px;white-space:nowrap;">
                                {date}
                            </span>
                            <span style="display:table-cell;padding:14px 16px 14px 2px;
                                         width:1%;vertical-align:middle;text-align:right;
                                         color:#667eea;font-size:18px;font-weight:700;
                                         white-space:nowrap;">
                                <span class="job-collapsed-icon">＋</span>
                                <span class="job-expanded-icon"
                                      style="display:none;">－</span>
                            </span>
                        </span>
                    </summary>
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
                </details>
            """

        html += f"""
                <div style="padding:22px 8px 8px;text-align:center;
                            color:#7f8795;font-size:12px;line-height:1.7;">
                    <div>邮件发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
                    <div>点击卡片可展开详情；点击职位名称可直接打开岗位页面</div>
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
            msg = MIMEMultipart('alternative')
            msg['Subject'] = '招聘监测系统 - 邮件配置测试'
            msg['From'] = self.sender
            msg['To'] = receiver

            now = datetime.now()
            preview_jobs = [
                {
                    'title': '高级法务顾问（点击卡片展开）',
                    'site_label': '腾讯法律',
                    'company': '示例科技公司',
                    'city': '深圳',
                    'salary': '30-45K·16薪',
                    'publish_time': now.strftime('%Y-%m-%d'),
                    'found_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'source_site': '邮件折叠测试',
                    'description': (
                        '岗位职责：\n'
                        '1. 负责互联网产品、平台服务及商业合作项目的法律支持，参与业务方案设计、风险评估和合同谈判。\n'
                        '2. 起草、审核并持续优化采购、销售、技术许可、广告合作及保密协议等常用合同模板。\n'
                        '3. 跟踪互联网、人工智能及消费者权益保护领域的监管政策，形成清晰可执行的合规建议。\n'
                        '任职要求：具有三年以上企业法务或律师事务所经验，具备良好的沟通、研究和跨团队协作能力。'
                    ),
                    'url': '',
                    '_is_new_this_run': True,
                },
                {
                    'title': '数据合规专家',
                    'site_label': '腾讯合规',
                    'company': '示例云计算公司',
                    'city': '北京',
                    'salary': '35-50K·15薪',
                    'publish_time': now.strftime('%Y-%m-%d'),
                    'found_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'source_site': '邮件折叠测试',
                    'description': (
                        '岗位职责：\n'
                        '1. 建立个人信息保护、数据安全和跨境数据传输相关制度，推动制度在产品研发与运营流程中落地。\n'
                        '2. 组织开展隐私影响评估、数据资产梳理和供应商合规审查，识别高风险处理活动并制定整改计划。\n'
                        '3. 为大模型训练、数据标注、日志分析及用户画像等场景提供专项合规意见。\n'
                        '任职要求：熟悉个人信息保护法、数据安全法及网络安全法，能够独立完成复杂项目的合规分析。'
                    ),
                    'url': '',
                    '_is_new_this_run': True,
                },
                {
                    'title': '商业合同法务经理',
                    'site_label': '字节法务',
                    'company': '示例内容平台',
                    'city': '上海',
                    'salary': '28-42K·15薪',
                    'publish_time': now.strftime('%Y-%m-%d'),
                    'found_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'source_site': '邮件折叠测试',
                    'description': (
                        '岗位职责：\n'
                        '1. 支持广告、电商、内容版权及市场营销业务，独立处理日常合同审核、商务谈判和法律咨询。\n'
                        '2. 结合业务实际整理高频风险问题，建立标准条款、审查清单和合同全生命周期管理机制。\n'
                        '3. 协助处理客户投诉、知识产权争议和商业纠纷，并与外部律师共同制定解决方案。\n'
                        '任职要求：法律基础扎实，能够在业务效率与风险控制之间作出合理判断，有平台型企业经验者优先。'
                    ),
                    'url': '',
                    '_is_new_this_run': True,
                },
                {
                    'title': '劳动用工合规负责人',
                    'site_label': '美团风控',
                    'company': '示例生活服务集团',
                    'city': '成都',
                    'salary': '25-38K·14薪',
                    'publish_time': now.strftime('%Y-%m-%d'),
                    'found_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'source_site': '邮件折叠测试',
                    'description': (
                        '岗位职责：\n'
                        '1. 为招聘、绩效、薪酬、员工关系和人员优化等场景提供劳动法律支持，审核相关制度及操作方案。\n'
                        '2. 处理劳动仲裁、诉讼和重大员工争议，复盘案件并推动人力资源流程持续改进。\n'
                        '3. 面向业务管理者开展劳动用工培训，定期发布典型案例、风险提示和实务操作指引。\n'
                        '任职要求：熟悉劳动合同法及各地司法实践，具备独立处理复杂员工关系事项的经验。'
                    ),
                    'url': '',
                    '_is_new_this_run': True,
                },
                {
                    'title': '投资并购与战略法务',
                    'site_label': '阿里战略',
                    'company': '示例数字商业公司',
                    'city': '杭州',
                    'salary': '40-60K·16薪',
                    'publish_time': now.strftime('%Y-%m-%d'),
                    'found_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'source_site': '邮件折叠测试',
                    'description': (
                        '岗位职责：\n'
                        '1. 参与境内外股权投资、并购、合资及战略合作项目，负责交易结构论证、法律尽调和风险清单整理。\n'
                        '2. 起草并谈判投资协议、股东协议、增资协议及交割文件，协调财务、税务和外部顾问推进项目实施。\n'
                        '3. 跟踪投后治理、承诺事项和退出安排，及时识别重大变化并向管理层提供决策建议。\n'
                        '任职要求：具备五年以上投资并购相关经验，英文可作为工作语言，能够同时管理多个复杂交易项目。'
                    ),
                    'url': '',
                    '_is_new_this_run': True,
                },
            ]
            html_content = self._create_email_content(
                preview_jobs,
                new_job_count=len(preview_jobs),
            )

            # 网易邮箱可能优先展示 multipart/alternative 中的纯文本部分；
            # 测试邮件与正式职位通知保持一致，只提供 HTML 卡片。
            msg.attach(MIMEText(html_content, 'html', 'utf-8'))
            
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
