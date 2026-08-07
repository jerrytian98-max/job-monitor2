"""爬虫解析器的离线回归测试，不访问真实招聘网站。"""

import unittest
from unittest.mock import Mock

from official_site_scraper import OfficialSiteScraper
from scraper import UniversalScraperWithPlaywright, get_scraper


class ScraperTests(unittest.TestCase):
    def test_missing_publish_date_is_saved_as_unknown(self):
        scraper = OfficialSiteScraper()

        self.assertEqual(scraper._normalize_date(None), "未知")
        self.assertEqual(scraper._normalize_date(""), "未知")
        self.assertEqual(scraper._normalize_date(float("inf")), "未知")

    def test_factory_returns_universal_scraper(self):
        scraper = get_scraper("https://jobs.example.com/search")
        self.assertIsInstance(scraper, UniversalScraperWithPlaywright)

    def test_invalid_target_url_is_rejected_without_browser(self):
        jobs = UniversalScraperWithPlaywright().scrape_jobs("not-a-url", ["战略"])
        self.assertEqual(jobs, [])

    def test_tencent_api_payload_keeps_full_job_fields(self):
        scraper = OfficialSiteScraper()
        payload = {
            "Data": {
                "Posts": [{
                    "PostId": "123",
                    "RecruitPostName": "法务经理",
                    "LocationName": "深圳",
                    "Responsibility": "负责合同审查",
                    "Requirement": "法律专业本科以上",
                    "CategoryName": "法律",
                    "LastUpdateTime": "2026-07-25",
                    "PostURL": "http://careers.tencent.com/jobdesc.html?postId=123",
                }]
            }
        }
        jobs = scraper._jobs_from_payload(
            payload,
            "https://careers.tencent.com/tencentcareer/api/post/Query",
            "https://careers.tencent.com/search.html?keyword=法务",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "法务经理")
        self.assertEqual(jobs[0]["city"], "深圳")
        self.assertIn("负责合同审查", jobs[0]["description"])
        self.assertIn("法律专业本科以上", jobs[0]["description"])
        self.assertTrue(jobs[0]["url"].startswith("https://"))

    def test_supported_api_payloads_map_to_detail_urls_and_descriptions(self):
        scraper = OfficialSiteScraper()
        cases = [
            (
                {"content": {"datas": [{
                    "id": 1,
                    "name": "法务专家",
                    "positionUrl": "/position-detail/1",
                    "workLocations": ["杭州"],
                    "description": "处理法律事务",
                    "requirement": "通过法律职业资格考试",
                    "publishTime": 1784900000000,
                }]}},
                "https://careers.aliyun.com/off-campus/position-list",
                "处理法律事务",
            ),
            (
                {"statusCode": 200, "data": {"list": [{
                    "positionId": "xhs-1",
                    "positionName": "资深法务",
                    "workplace": "上海",
                    "duty": "负责争议解决",
                    "qualification": "五年以上经验",
                    "publishTime": 1784900000000,
                }]}},
                "https://job.xiaohongshu.com/social/position?positionName=法务",
                "负责争议解决",
            ),
            (
                {"code": 0, "data": {"job_post_list": [{
                    "id": "fs-1",
                    "title": "法务BP",
                    "city_list": [{"name": "北京"}],
                    "description": "支持业务合规",
                    "requirement": "熟悉互联网业务",
                    "publish_time": 1784900000000,
                }]}},
                "https://vrfi1sk8a0.jobs.feishu.cn/index/?keywords=法务",
                "支持业务合规",
            ),
            (
                {"data": {"list": [{
                    "jobUnionId": "mt-1",
                    "name": "华南法务专家",
                    "cityList": [{"name": "广州"}, {"name": "深圳"}],
                    "jobDuty": "处理诉讼仲裁",
                    "jobRequirement": "七年以上经验",
                    "refreshTime": 1784900000000,
                }]}},
                "https://zhaopin.meituan.com/web/social?keyword=法",
                "处理诉讼仲裁",
            ),
        ]
        for payload, target_url, expected_detail in cases:
            with self.subTest(target_url=target_url):
                jobs = scraper._jobs_from_payload(payload, "https://api.example", target_url)
                self.assertEqual(len(jobs), 1)
                self.assertIn(expected_detail, jobs[0]["description"])
                self.assertNotEqual(jobs[0]["city"], "未知")
                self.assertIn("http", jobs[0]["url"])

    def test_kuaishou_api_payload_maps_to_searchable_job_details(self):
        scraper = OfficialSiteScraper()
        target_url = (
            "https://zhaopin.kuaishou.cn/recruit/e/"
            "#/official/social/?workLocationCode=domestic"
        )
        payload = {
            "code": 0,
            "result": {
                "total": 1,
                "list": [{
                    "id": 24968,
                    "name": "法务专家",
                    "workLocationsCode": ["Beijing", "Shanghai"],
                    "description": "负责法律事务与合规支持",
                    "positionDemand": "具备法律专业背景",
                    "positionCategoryCode": "J0002",
                    "updateTime": "2026-08-07T16:04:32.000+08:00",
                }],
            },
        }

        jobs = scraper._jobs_from_payload(
            payload,
            "https://zhaopin.kuaishou.cn/recruit/e/api/v1/open/positions/simple",
            target_url,
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "快手")
        self.assertEqual(jobs[0]["city"], "北京、上海")
        self.assertIn("负责法律事务", jobs[0]["description"])
        self.assertIn("具备法律专业背景", jobs[0]["description"])
        self.assertEqual(
            jobs[0]["url"],
            "https://zhaopin.kuaishou.cn/recruit/e/"
            "#/official/social/job-info/24968",
        )

    def test_rendered_card_merge_prefers_full_description(self):
        scraper = OfficialSiteScraper()
        short = {
            "title": "法务经理",
            "url": "https://jobs.example.com/job/1",
            "description": "法务经理",
            "city": "未知",
        }
        detailed = {
            "title": "法务经理",
            "url": "https://jobs.example.com/job/1",
            "description": "岗位职责\n负责合同审查和争议解决",
            "city": "北京",
        }
        merged = scraper._merge_jobs([short], [detailed])
        self.assertEqual(len(merged), 1)
        self.assertIn("争议解决", merged[0]["description"])
        self.assertEqual(merged[0]["city"], "北京")

    def test_merge_preserves_every_keyword_that_found_the_same_job(self):
        scraper = OfficialSiteScraper()
        first = [{
            "title": "法务专家",
            "url": "https://jobs.example.com/job/1",
            "_search_keyword": "法律",
        }]
        second = [{
            "title": "法务专家",
            "url": "https://jobs.example.com/job/1",
            "_search_keyword": "法务",
        }]

        merged = scraper._merge_jobs(first, second)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["_search_keywords"], ["法律", "法务"])

    def test_merge_ignores_alibaba_tracking_ids(self):
        scraper = OfficialSiteScraper()
        first = [{
            "title": "法务专家",
            "url": (
                "https://talent.quark.cn/off-campus/position-detail"
                "?positionId=1001&track_id=first"
            ),
            "_search_keyword": "法律",
        }]
        second = [{
            "title": "法务专家",
            "url": (
                "https://talent.quark.cn/off-campus/position-detail"
                "?track_id=second&positionId=1001"
            ),
            "_search_keyword": "法务",
        }]

        merged = scraper._merge_jobs(first, second)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["_search_keywords"], ["法律", "法务"])

    def test_meituan_keyword_api_fetches_all_reported_pages(self):
        scraper = OfficialSiteScraper()

        def response(page_no, total_pages, job_id):
            result = Mock()
            result.raise_for_status.return_value = None
            result.json.return_value = {
                "data": {
                    "list": [{
                        "jobUnionId": job_id,
                        "name": f"法务职位-{job_id}",
                        "cityList": [{"name": "北京"}],
                        "jobDuty": "负责法律事务",
                    }],
                    "page": {
                        "pageNo": page_no,
                        "pageSize": 100,
                        "totalPage": total_pages,
                        "totalCount": total_pages,
                    },
                }
            }
            return result

        scraper.session.post = Mock(side_effect=[
            response(1, 2, "mt-1"),
            response(2, 2, "mt-2"),
        ])

        jobs = scraper._scrape_meituan_keyword(
            "https://zhaopin.meituan.com/web/social",
            "法务",
        )

        self.assertEqual([job["title"] for job in jobs], [
            "法务职位-mt-1",
            "法务职位-mt-2",
        ])
        self.assertTrue(all(job["_search_keyword"] == "法务" for job in jobs))
        self.assertEqual(scraper.session.post.call_count, 2)
        self.assertEqual(
            scraper.session.post.call_args_list[1].kwargs["json"]["page"]["pageNo"],
            2,
        )

    def test_alibaba_keyword_api_uses_csrf_and_fetches_all_pages(self):
        scraper = OfficialSiteScraper()
        landing = Mock()
        landing.raise_for_status.return_value = None
        scraper.session.get = Mock(return_value=landing)
        scraper.session.cookies.set(
            "XSRF-TOKEN",
            "test-token",
            domain="talent.quark.cn",
        )

        def response(page_no, total_count, position_id):
            result = Mock()
            result.raise_for_status.return_value = None
            result.json.return_value = {
                "content": {
                    "datas": [{
                        "id": position_id,
                        "name": f"法务职位-{position_id}",
                        "positionUrl": (
                            "/off-campus/position-detail?positionId="
                            + position_id
                        ),
                        "workLocations": ["北京"],
                    }],
                    "totalCount": total_count,
                    "currentPage": page_no,
                    "pageSize": 1,
                }
            }
            return result

        scraper.session.post = Mock(side_effect=[
            response(1, 2, "q-1"),
            response(2, 2, "q-2"),
        ])

        jobs = scraper._scrape_alibaba_keyword(
            "https://talent.quark.cn/off-campus/position-list",
            "法务",
        )

        self.assertEqual([job["title"] for job in jobs], [
            "法务职位-q-1",
            "法务职位-q-2",
        ])
        self.assertEqual(scraper.session.post.call_count, 2)
        self.assertEqual(
            scraper.session.post.call_args_list[1].kwargs["json"]["pageIndex"],
            2,
        )
        self.assertEqual(
            scraper.session.post.call_args_list[0].kwargs["params"]["_csrf"],
            "test-token",
        )

    def test_keyword_navigation_urls_avoid_default_result_requests(self):
        byte_url = OfficialSiteScraper._keyword_navigation_url(
            "https://jobs.bytedance.com/experienced/position/list?keywords=&limit=10",
            "法律",
        )
        xhs_url = OfficialSiteScraper._keyword_navigation_url(
            "https://job.xiaohongshu.com/social/position?positionName=&jobTypes=",
            "法务",
        )
        tencent_url = OfficialSiteScraper._keyword_navigation_url(
            "https://careers.tencent.com/jobopportunity.html",
            "法务",
        )

        self.assertIn("keywords=%E6%B3%95%E5%BE%8B", byte_url)
        self.assertIn("limit=30", byte_url)
        self.assertIn("positionName=%E6%B3%95%E5%8A%A1", xhs_url)
        self.assertIn("/search.html?keyword=%E6%B3%95%E5%8A%A1", tencent_url)

    def test_response_keyword_guard_rejects_delayed_default_list(self):
        default_response = Mock()
        default_response.url = "https://jobs.example.com/api/search"
        default_response.request.url = default_response.url
        default_response.request.post_data = '{"keyword":""}'
        search_response = Mock()
        search_response.url = "https://jobs.example.com/api/search"
        search_response.request.url = (
            search_response.url + "?keyword=%25E6%25B3%2595%25E5%258A%25A1"
        )
        search_response.request.post_data = None

        self.assertFalse(
            OfficialSiteScraper._response_matches_keyword(default_response, "法务")
        )
        self.assertTrue(
            OfficialSiteScraper._response_matches_keyword(search_response, "法务")
        )

    def test_unfiltered_url_searches_every_configured_keyword(self):
        scraper = OfficialSiteScraper()
        url = (
            "https://jobs.bytedance.com/experienced/position/list"
            "?keywords=&category=&location="
        )
        self.assertEqual(
            scraper._search_plan(url, ["法务", "合规", "法务"]),
            [("法务", "法务"), ("合规", "合规")],
        )

    def test_saved_search_url_keeps_backward_compatible_keyword(self):
        scraper = OfficialSiteScraper()
        url = (
            "https://jobs.bytedance.com/experienced/position/list"
            "?keywords=%E6%B3%95%E5%8A%A1"
        )
        self.assertEqual(
            scraper._search_plan(url, ["合规"]),
            [("", "法务")],
        )

    def test_fixed_url_mode_does_not_infer_or_submit_global_keywords(self):
        scraper = OfficialSiteScraper()
        url = (
            "https://jobs.bytedance.com/experienced/position/list"
            "?keywords=%E6%B3%95%E5%8A%A1&location=110000"
        )

        self.assertEqual(
            scraper._search_plan(url, ["法律", "合规"], fixed_url_mode=True),
            [("", "")],
        )

    def test_taotian_homepage_uses_social_recruitment_search_landing_page(self):
        self.assertEqual(
            OfficialSiteScraper._starting_url("https://talent.taotian.com/"),
            "https://talent.taotian.com/off-campus/home?lang=zh",
        )

    def test_bytedance_search_requests_only_the_first_thirty_results(self):
        starting_url = OfficialSiteScraper._starting_url(
            "https://jobs.bytedance.com/experienced/position/list"
            "?keywords=&category=&current=3&limit=10"
        )

        self.assertIn("limit=30", starting_url)
        self.assertIn("current=1", starting_url)
        self.assertIn("keywords=", starting_url)
        self.assertIn("category=", starting_url)

    def test_pdd_recommendation_payload_is_not_treated_as_search_results(self):
        scraper = OfficialSiteScraper()
        recommendation_payload = {
            "success": True,
            "result": {
                "latestPositionList": [
                    {"name": "算法工程师", "code": "T100"}
                ]
            },
        }
        search_payload = {
            "success": True,
            "result": {
                "list": [
                    {"name": "法务专家", "code": "L100"}
                ]
            },
        }
        target_url = "https://careers.pddglobalhr.com/jobs"

        self.assertEqual(
            scraper._jobs_from_payload(
                recommendation_payload,
                "https://careers.pddglobalhr.com/api/recruit/position/latest_list",
                target_url,
            ),
            [],
        )
        jobs = scraper._jobs_from_payload(
            search_payload,
            "https://careers.pddglobalhr.com/api/recruit/position/list",
            target_url,
        )
        self.assertEqual([job["title"] for job in jobs], ["法务专家"])

    def test_bytedance_payload_uses_public_detail_url_and_full_fields(self):
        scraper = OfficialSiteScraper()
        payload = {
            "code": 0,
            "data": {
                "job_post_list": [{
                    "id": "123456",
                    "title": "商业产品法务",
                    "description": "负责产品法律支持",
                    "requirement": "通过法律职业资格考试",
                    "city_list": [{"name": "北京"}],
                    "job_category": {"name": "法务"},
                    "publish_time": 1784900000000,
                }]
            },
        }
        jobs = scraper._jobs_from_payload(
            payload,
            "https://jobs.bytedance.com/api/v1/search/job/posts",
            "https://jobs.bytedance.com/experienced/position/list?keywords=",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "字节跳动")
        self.assertEqual(jobs[0]["city"], "北京")
        self.assertIn("负责产品法律支持", jobs[0]["description"])
        self.assertEqual(
            jobs[0]["url"],
            "https://jobs.bytedance.com/experienced/position/123456/detail",
        )

    def test_merge_does_not_apply_cross_keyword_fifty_job_cap(self):
        scraper = OfficialSiteScraper()
        first = [
            {
                "title": f"法务-{index}",
                "url": f"https://jobs.example.com/job/legal-{index}",
            }
            for index in range(50)
        ]
        second = [
            {
                "title": f"合规-{index}",
                "url": f"https://jobs.example.com/job/compliance-{index}",
            }
            for index in range(50)
        ]
        self.assertEqual(len(scraper._merge_jobs(first, second)), 100)

    def test_each_keyword_search_keeps_only_its_first_thirty_jobs(self):
        scraper = OfficialSiteScraper()
        jobs = [
            {
                "title": f"职位-{index}",
                "url": f"https://jobs.example.com/job/{index}",
            }
            for index in range(35)
        ]

        limited = scraper._limit_search_results(jobs)

        self.assertEqual(len(limited), 30)
        self.assertEqual(limited[0]["title"], "职位-0")
        self.assertEqual(limited[-1]["title"], "职位-29")


if __name__ == "__main__":
    unittest.main()
