import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from werkzeug.datastructures import MultiDict

from database import JobDatabase
from job_tags import job_listing


class JobTagsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = JobDatabase(str(Path(self.directory.name) / 'jobs.db'))
        self.config = {'job_sites': ['https://jobs.example.com/search'],
                       'job_site_labels': {'https://jobs.example.com/search': '腾讯'},
                       'job_keywords': ['法律', '产品经理']}

    def tearDown(self):
        self.directory.cleanup()

    def add_job(self, title, company='腾讯', keyword='', fixed=False, **extra):
        value = {'title': title, 'company': company, 'url': f'https://jobs.example.com/{title}',
                 'description': title, '_search_keyword': keyword, '_fixed_url_mode': fixed, **extra}
        self.assertTrue(self.database.add_job(value))
        return value

    def listing(self, *arguments):
        return job_listing(self.database, self.config, MultiDict(arguments), 10)

    def test_records_store_separate_company_keyword_and_fixed_mode(self):
        self.add_job('搜索岗位', keyword='法律', site_label='腾讯法律')
        self.add_job('直抓岗位', keyword='忽略的关键词', fixed=True)
        records = {job['title']: job for job in self.listing()['data']}
        self.assertEqual(records['搜索岗位']['company_tag'], '腾讯')
        self.assertEqual(records['搜索岗位']['matched_keyword'], '法律')
        self.assertEqual(records['直抓岗位']['matched_keyword'], '')
        self.assertEqual(records['直抓岗位']['capture_mode'], 'fixed')
        self.assertEqual(self.listing()['filters'], {'sites': ['腾讯'], 'keywords': ['法律']})

    def test_facets_use_all_jobs_and_exclusions_apply_before_pagination(self):
        self.config['job_sites'] = []
        self.config['job_site_labels'] = {}
        for index in range(14):
            url = f'https://jobs{index}.example.com/search'
            self.config['job_sites'].append(url)
            self.config['job_site_labels'][url] = f'网址{index}'
            self.add_job(f'职位{index}', company=f'公司{index}', keyword=f'词{index}',
                         source_site=f'jobs{index}.example.com', url=url + f'/{index}')
        first = self.listing()
        self.assertEqual(len(first['data']), 10)
        self.assertEqual(len(first['filters']['sites']), 14)
        self.assertEqual(len(first['filters']['keywords']), 14)
        filtered = self.listing(('exclude_site', '网址0'), ('exclude_keyword', '词1'))
        self.assertEqual(filtered['pagination']['total'], 12)
        self.assertEqual(filtered['filters'], first['filters'])
        self.assertFalse(any(job['recruitment_site_tag'] == '网址0' or job['matched_keyword'] == '词1' for job in filtered['data']))
        self.assertEqual(self.listing(*[('exclude_site', f'网址{index}') for index in range(14)])['pagination']['total'], 0)

    def test_current_config_labels_replace_scraped_names_and_update_without_recrawling(self):
        url = 'https://jobs.bytedance.com/experienced/position/list'
        self.config['job_sites'] = [url, 'https://soulapp.jobs.feishu.cn/index']
        self.config['job_site_labels'] = {url: '字节自定义', self.config['job_sites'][1]: 'SOUL'}
        self.add_job('字节岗位', company='字节跳动', keyword='法律', site_label='字节法律',
                     source_site='jobs.bytedance.com')
        result = self.listing()
        self.assertEqual(result['filters']['sites'], ['字节自定义', 'SOUL'])
        self.assertEqual(result['data'][0]['recruitment_site_tag'], '字节自定义')
        self.assertEqual(result['data'][0]['company'], '字节跳动')
        self.config['job_site_labels'][url] = '我修改的标签'
        self.assertEqual(self.listing()['data'][0]['recruitment_site_tag'], '我修改的标签')
        self.assertEqual(self.listing(('exclude_site', '我修改的标签'))['pagination']['total'], 0)

    def test_shared_domains_keep_distinct_configured_recruitment_labels(self):
        search = 'https://careers.tencent.com/jobopportunity.html'
        fixed = 'https://careers.tencent.com/zh-cn/search.html?query=product'
        moon = 'https://app.mokahr.com/apply/moonshot/148506#/jobs'
        step = 'https://app.mokahr.com/social-recruitment/step/94904#/jobs'
        self.config.update(job_sites=[search, fixed, moon, step],
                           job_site_labels={search: '腾讯', fixed: '腾讯产品岗', moon: '月之暗面', step: '阶跃星辰'},
                           job_site_modes={search: 'search', fixed: 'fixed'})
        self.add_job('腾讯搜索', keyword='法律', site_label='腾讯法律', source_site='careers.tencent.com')
        self.add_job('腾讯固定', fixed=True, site_label='腾讯产品岗', source_site='careers.tencent.com')
        self.add_job('月暗旧数据', company='不能作为标签', site_label='月暗法律', keyword='法律',
                     source_site='app.mokahr.com', url='https://app.mokahr.com/apply/moonshot/148506#/job/123')
        records = {job['title']: job['recruitment_site_tag'] for job in self.listing()['data']}
        self.assertEqual(records, {'腾讯搜索': '腾讯', '腾讯固定': '腾讯产品岗', '月暗旧数据': '月之暗面'})
        filtered = self.listing(('exclude_site', '腾讯产品岗'))
        self.assertEqual(filtered['pagination']['total'], 2)
        self.assertNotIn('腾讯固定', [job['title'] for job in filtered['data']])
        self.add_job('历史腾讯关键词', site_label='腾讯已删除的关键词', source_site='careers.tencent.com')
        records = {job['title']: job['recruitment_site_tag'] for job in self.listing()['data']}
        self.assertEqual(records['历史腾讯关键词'], '腾讯')

    def test_keyword_filters_leave_direct_jobs_and_can_exclude_unknowns(self):
        self.add_job('关键词岗位', keyword='法律')
        self.add_job('直抓岗位', fixed=True)
        self.add_job('未知岗位')
        result = self.listing(('exclude_keyword', '法律'), ('exclude_keyword', ''))
        self.assertEqual([job['title'] for job in result['data']], ['直抓岗位'])
        self.assertEqual(result['filters']['keywords'], ['', '法律'])

    def test_legacy_keywords_recover_from_original_cache_or_known_labels(self):
        self.add_job('旧搜索岗位', site_label='腾讯法律')
        original = self.add_job('旧直抓岗位', site_label='腾讯产品经理')
        cached = dict(original, _fixed_url_mode=True)
        Path(self.directory.name, 'jobs_cache.json').write_text(json.dumps({'legacy-key': {'job': cached}}), encoding='utf-8')
        self.add_job('无法确认岗位', site_label='腾讯未知分类')
        records = {job['title']: job for job in self.listing()['data']}
        self.assertEqual(records['旧搜索岗位']['matched_keyword'], '法律')
        self.assertEqual(records['旧直抓岗位']['capture_mode'], 'fixed')
        self.assertEqual(records['旧直抓岗位']['matched_keyword'], '')
        self.assertEqual(records['无法确认岗位']['matched_keyword'], '')
        self.assertEqual(records['无法确认岗位']['capture_mode'], 'unknown')
        self.assertEqual(self.database.count_jobs(), 3)

    def test_old_database_schema_gets_tags_without_losing_existing_rows(self):
        self.add_job('已有岗位', keyword='法律')
        with closing(self.database._get_connection()) as conn, conn:
            conn.execute('ALTER TABLE jobs DROP COLUMN matched_keyword')
            conn.execute('ALTER TABLE jobs DROP COLUMN capture_mode')
        reopened = JobDatabase(self.database.db_path)
        self.assertEqual(reopened.count_jobs(), 1)
        self.assertEqual(reopened.get_all_jobs()[0]['matched_keyword'], '')


if __name__ == '__main__':
    unittest.main()
