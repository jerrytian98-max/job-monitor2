"""Offline provider routing and response parsing checks for AI job filtering."""

import json
import unittest
from unittest.mock import Mock, patch
import requests

from ai_filter import AIAnalysisError, filter_jobs_with_ai
from web_config import SECRET_MASK, preserve_masked_secrets, redact_config, update_ai_filter_config, validate_config


JOBS = [
    {'title': '战略分析', 'company': '示例甲', 'salary': '20k'},
    {'title': '法务', 'company': '示例乙', 'salary': '25k'},
]


class AiProviderTests(unittest.TestCase):
    def test_openai_chat_completions_uses_its_own_endpoint_and_key(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '[1]'}}]}
        with patch('ai_filter.requests.post', return_value=response) as post:
            self.assertEqual(filter_jobs_with_ai(JOBS, 'openai-secret', '只保留法务',
                                                  'gpt-5.4-mini', provider='openai'), [JOBS[1]])
        self.assertEqual(post.call_args.args[0], 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer openai-secret')
        self.assertEqual(post.call_args.kwargs['json']['model'], 'gpt-5.4-mini')
        self.assertNotIn('thinking', post.call_args.kwargs['json'])
        messages = post.call_args.kwargs['json']['messages']
        self.assertEqual([item['role'] for item in messages], ['system', 'user'])
        self.assertIn('[提示词]\n只保留法务', messages[0]['content'])
        self.assertEqual(json.loads(messages[1]['content'].split('\n', 1)[1])[1]['job'], JOBS[1])

    def test_deepseek_chat_completions_uses_its_own_endpoint_and_key(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '```json\n[0, 0, true, -1]\n```'}}]}
        with patch('ai_filter.requests.post', return_value=response) as post:
            self.assertEqual(filter_jobs_with_ai(JOBS, 'deepseek-secret', '只保留战略',
                                                  'deepseek-flash', provider='deepseek'), [JOBS[0]])
        self.assertEqual(post.call_args.args[0], 'https://api.deepseek.com/chat/completions')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer deepseek-secret')
        self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})

    def test_legacy_gemini_route_still_works(self):
        response = Mock()
        response.json.return_value = {'candidates': [{'content': {'parts': [{'text': '[0]'}]}}]}
        with patch('ai_filter.requests.post', return_value=response) as post:
            self.assertEqual(filter_jobs_with_ai(JOBS, 'gemini-secret', '只保留战略'), [JOBS[0]])
        self.assertIn('generativelanguage.googleapis.com', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['headers']['x-goog-api-key'], 'gemini-secret')

    def test_ai_error_does_not_approve_all_jobs(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError('unauthorized')
        with patch('ai_filter.requests.post', return_value=response):
            with self.assertRaises(AIAnalysisError):
                filter_jobs_with_ai(JOBS, 'bad-key', '只保留法务', provider='openai')

    def test_all_job_fields_are_sent_without_modifying_selected_original(self):
        full_job = dict(JOBS[0], city='北京', description='完整岗位描述',
                        url='https://example.com/job/1', site_label='示例战略',
                        publish_time='2026-10-28')
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '[0]'}}]}
        with patch('ai_filter.requests.post', return_value=response) as post:
            selected = filter_jobs_with_ai([full_job], 'key', '战略岗位', provider='openai')
        submitted = json.loads(post.call_args.kwargs['json']['messages'][1]['content'].split('\n', 1)[1])
        self.assertEqual(submitted[0]['job'], full_job)
        self.assertIs(selected[0], full_job)

    def test_large_run_is_split_without_losing_job_structure(self):
        jobs = [dict(JOBS[0], url=f'https://example.com/job/{i}') for i in range(21)]
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '[0]'}}]}
        with patch('ai_filter.requests.post', return_value=response) as post:
            selected = filter_jobs_with_ai(jobs, 'key', '筛选', provider='openai')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(selected, [jobs[0], jobs[20]])
        second_batch = json.loads(post.call_args.kwargs['json']['messages'][1]['content'].split('\n', 1)[1])
        self.assertEqual(second_batch, [{'id': 0, 'job': jobs[20]}])

    def test_keys_are_masked_and_preserved_across_provider_switches(self):
        old = {
            'gemini_api_key': 'gemini-secret', 'openai_api_key': 'openai-secret',
            'deepseek_api_key': 'deepseek-secret', 'ai_provider': 'gemini',
            'ai_filter_prompt': '只保留法务', 'ai_filter_enabled': True,
        }
        public = redact_config(old)
        for name in ('gemini_api_key', 'openai_api_key', 'deepseek_api_key'):
            self.assertEqual(public[name], SECRET_MASK)
        switched = update_ai_filter_config(old, {
            'ai_provider': 'deepseek', 'deepseek_api_key': SECRET_MASK,
            'ai_filter_prompt': '只保留法务', 'ai_filter_enabled': True,
        })
        self.assertEqual(switched['deepseek_api_key'], 'deepseek-secret')
        self.assertEqual(switched['gemini_api_key'], 'gemini-secret')
        self.assertEqual(switched['openai_api_key'], 'openai-secret')
        self.assertEqual(switched['ai_provider'], 'deepseek')

    def test_selected_provider_requires_its_key_and_legacy_defaults_to_gemini(self):
        base = {
            'job_keywords': [], 'cities': [], 'exclude_keywords': [], 'job_sites': [],
            'email': {}, 'check_interval': 4, 'gemini_api_key': 'legacy-key',
            'ai_filter_prompt': '筛选条件',
        }
        self.assertEqual(validate_config(base)['ai_provider'], 'gemini')
        with self.assertRaisesRegex(ValueError, '当前服务商'):
            validate_config({**base, 'ai_provider': 'openai', 'ai_filter_enabled': True})
        with self.assertRaisesRegex(ValueError, 'AI 服务商'):
            validate_config({**base, 'ai_provider': 'unknown'})
        restored = preserve_masked_secrets({'openai_api_key': SECRET_MASK},
                                           {'openai_api_key': 'saved-openai', 'ai_provider': 'openai'})
        self.assertEqual(restored['openai_api_key'], 'saved-openai')
        self.assertEqual(restored['ai_provider'], 'openai')

    def test_local_ai_endpoint_preserves_other_provider_keys(self):
        from app import app

        existing = {
            'ai_provider': 'gemini', 'gemini_api_key': 'saved-gemini',
            'openai_api_key': 'saved-openai', 'deepseek_api_key': '',
            'ai_filter_prompt': '只保留法务', 'ai_filter_enabled': True,
        }
        saved = []
        with patch('app.load_config', return_value=existing), patch(
            'app.save_config', side_effect=lambda value: saved.append(value) or True
        ):
            response = app.test_client().post('/api/ai-filter', json={
                'ai_provider': 'deepseek', 'gemini_api_key': SECRET_MASK,
                'openai_api_key': SECRET_MASK, 'deepseek_api_key': 'new-deepseek',
                'ai_filter_prompt': '只保留战略', 'ai_filter_enabled': True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved[0]['ai_provider'], 'deepseek')
        self.assertEqual(saved[0]['gemini_api_key'], 'saved-gemini')
        self.assertEqual(saved[0]['openai_api_key'], 'saved-openai')
        self.assertEqual(saved[0]['deepseek_api_key'], 'new-deepseek')


if __name__ == '__main__':
    unittest.main()
