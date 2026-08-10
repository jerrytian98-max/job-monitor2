"""招聘网址标签解析的离线测试。"""

import unittest

from site_labels import (
    build_site_labels,
    build_site_modes,
    combine_site_keyword_label,
    combine_site_keyword_labels,
    derive_site_label,
    get_site_label,
    get_site_mode,
)


class SiteLabelTests(unittest.TestCase):
    def test_decodes_chinese_keyword_label(self):
        url = (
            'https://jobs.bytedance.com/experienced/position/list'
            '?keywords=%E6%B3%95%E5%8A%A1'
        )
        self.assertEqual(derive_site_label(url), '法务')

    def test_prefers_keyword_over_other_chinese_parameters(self):
        url = (
            'https://jobs.example.com/search'
            '?city=%E5%8C%97%E4%BA%AC&keywords=%E6%88%98%E7%95%A5'
        )
        self.assertEqual(derive_site_label(url), '战略')

    def test_preserves_manually_edited_label(self):
        url = 'https://jobs.example.com/search?keywords=%E6%B3%95%E5%8A%A1'
        labels = build_site_labels([url], {url: '法务与合规'})
        self.assertEqual(labels[url], '法务与合规')
        self.assertEqual(get_site_label({'job_site_labels': labels}, url), '法务与合规')

    def test_falls_back_to_domain_without_chinese_query(self):
        self.assertEqual(
            derive_site_label('https://jobs.example.com/search?q=legal'),
            'jobs.example.com',
        )

    def test_recognizes_known_company_from_unfiltered_url(self):
        self.assertEqual(
            derive_site_label('https://zhaopin.meituan.com/web/social'),
            '美团',
        )
        self.assertEqual(
            derive_site_label(
                'https://jobs.bytedance.com/experienced/position/list?keywords='
            ),
            '字节',
        )
        self.assertEqual(
            derive_site_label(
                'https://talent.quark.cn/off-campus/position-list?lang=zh'
            ),
            '夸克',
        )

    def test_combines_base_site_label_and_search_keyword(self):
        self.assertEqual(combine_site_keyword_label('美团', '法务'), '美团法务')
        self.assertEqual(combine_site_keyword_label('夸克', '合规'), '夸克合规')

    def test_does_not_duplicate_keyword_in_legacy_label(self):
        self.assertEqual(
            combine_site_keyword_label('腾讯法务', '法务'),
            '腾讯法务',
        )

    def test_keeps_the_first_keyword_label_when_a_job_matches_multiple_keywords(self):
        self.assertEqual(
            combine_site_keyword_labels('美团', ['法律', '法务', '法律']),
            '美团法律',
        )

    def test_old_sites_default_to_keyword_search_and_fixed_mode_is_preserved(self):
        search_url = 'https://jobs.example.com/search'
        fixed_url = 'https://jobs.example.com/fixed-list?department=legal'

        modes = build_site_modes(
            [search_url, fixed_url],
            {fixed_url: 'fixed'},
        )

        self.assertEqual(modes[search_url], 'search')
        self.assertEqual(modes[fixed_url], 'fixed')
        self.assertEqual(
            get_site_mode({'job_site_modes': modes}, fixed_url),
            'fixed',
        )


if __name__ == '__main__':
    unittest.main()
