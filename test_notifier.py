"""邮件通知条件和标题的离线测试。"""

import unittest
from unittest.mock import patch

from notifier import EmailNotifier


class EmailNotifierTests(unittest.TestCase):
    def setUp(self):
        self.notifier = EmailNotifier({
            'sender': 'sender@example.com',
            'auth_code': 'test-code',
            'receiver': 'receiver@example.com',
            'smtp_server': 'smtp.example.com',
            'smtp_port': 465,
        })

    def test_subject_contains_site_label_and_new_job_count(self):
        jobs = [
            {'title': '职位一', 'site_label': '字节战略'},
            {'title': '职位二', 'site_label': '字节法律'},
        ]

        self.assertEqual(
            self.notifier._build_notification_subject(jobs),
            '【字节战略、字节法律】发现了 2 个新职位',
        )

    def test_empty_job_list_does_not_connect_to_smtp(self):
        previous_jobs = [{'title': '历史职位'}]
        with (
            patch('notifier.smtplib.SMTP') as smtp,
            patch('notifier.smtplib.SMTP_SSL') as smtp_ssl,
        ):
            self.assertTrue(
                self.notifier.send_notification(
                    previous_jobs,
                    '字节战略',
                    new_jobs=[],
                )
            )

        smtp.assert_not_called()
        smtp_ssl.assert_not_called()

    def test_sent_message_uses_required_subject(self):
        jobs = [
            {'title': '战略岗位', 'site_label': '字节战略'},
            {'title': '法务岗位', 'site_label': '字节法律'},
        ]

        with patch('notifier.smtplib.SMTP_SSL') as smtp_ssl:
            server = smtp_ssl.return_value.__enter__.return_value
            self.assertTrue(self.notifier.send_notification(jobs))

        sent_message = server.send_message.call_args.args[0]
        self.assertEqual(
            str(sent_message['Subject']),
            '【字节战略、字节法律】发现了 2 个新职位',
        )

    def test_test_email_contains_collapsible_card_preview(self):
        with patch('notifier.smtplib.SMTP_SSL') as smtp_ssl:
            server = smtp_ssl.return_value.__enter__.return_value
            self.assertTrue(self.notifier.send_test_email())

        sent_message = server.send_message.call_args.args[0]
        self.assertEqual(len(sent_message.get_payload()), 1)
        html_part = next(
            part
            for part in sent_message.get_payload()
            if part.get_content_type() == 'text/html'
        )
        html = html_part.get_payload(decode=True).decode(
            html_part.get_content_charset()
        )
        self.assertEqual(html.count('<details class="job-card"'), 5)
        self.assertIn('本轮发现 5 个新职位', html)
        self.assertIn('高级法务顾问（点击卡片展开）', html)
        self.assertIn('数据合规专家', html)
        self.assertIn('商业合同法务经理', html)
        self.assertIn('劳动用工合规负责人', html)
        self.assertIn('投资并购与战略法务', html)
        self.assertIn('跨境数据传输', html)
        self.assertIn('合同全生命周期管理机制', html)

    def test_email_body_uses_collapsible_job_cards_and_linked_title(self):
        html = self.notifier._create_email_content([
            {
                'title': '战略分析师 职位 ID：A123',
                'site_label': '字节战略',
                'company': '字节跳动',
                'city': '北京',
                'salary': '面议',
                'publish_time': '2026-07-26 09:30:00',
                'found_time': '2026-07-26 10:00:00',
                'source_site': 'jobs.bytedance.com',
                'description': '负责业务战略分析',
                'url': 'https://jobs.bytedance.com/job/123',
                '_is_new_this_run': True,
            }
        ], new_job_count=1)

        self.assertIn('爬取的职位信息', html)
        self.assertIn('<details class="job-card"', html)
        self.assertIn('<summary', html)
        details_opening_tag = html.split('<details', 1)[1].split('>', 1)[0]
        self.assertNotIn(' open', details_opening_tag)
        self.assertIn('job-collapsed-icon', html)
        self.assertIn('job-expanded-icon', html)
        self.assertIn('字节战略', html)
        self.assertIn('2026-07-26', html)
        self.assertIn('负责业务战略分析', html)
        self.assertIn(
            'href="https://jobs.bytedance.com/job/123"',
            html,
        )
        self.assertIn('>战略分析师</a>', html)
        self.assertIn('>新</span>', html)
        self.assertNotIn('职位 ID', html)

    def test_email_body_does_not_link_unsafe_job_url(self):
        html = self.notifier._create_email_content([
            {
                'title': '示例职位',
                'site_label': '示例',
                'url': 'javascript:alert(1)',
            }
        ])

        self.assertNotIn('href="javascript:', html)


if __name__ == '__main__':
    unittest.main()
