"""招聘网址标签的解析与兼容工具。"""

import re
from typing import Dict, Iterable
from urllib.parse import parse_qs, unquote_plus, urlparse


PREFERRED_QUERY_KEYS = (
    'keywords',
    'keyword',
    'query',
    'q',
    'key',
    'search',
    'position',
)
CHINESE_PATTERN = re.compile(r'[\u3400-\u9fff]+')
KNOWN_SITE_LABELS = {
    'jobs.bytedance.com': '字节',
    'zhaopin.meituan.com': '美团',
    'talent.quark.cn': '夸克',
    'careers.tencent.com': '腾讯',
    'talent.ele.me': '饿了么',
    'careers.aliyun.com': '阿里云',
    'job.xiaohongshu.com': '小红书',
    'careers.pddglobalhr.com': '拼多多',
}
SITE_MODE_SEARCH = 'search'
SITE_MODE_FIXED = 'fixed'
VALID_SITE_MODES = (SITE_MODE_SEARCH, SITE_MODE_FIXED)


def _label_from_value(value: str) -> str:
    """从已经或尚未 URL 解码的参数值中提取中文片段。"""
    decoded = unquote_plus(str(value or '')).strip()
    fragments = CHINESE_PATTERN.findall(decoded)
    return ' '.join(fragments)[:100]


def derive_site_label(url: str) -> str:
    """从 URL 查询参数自动生成标签；没有中文参数时使用域名。"""
    parsed = urlparse(str(url or '').strip())
    query = parse_qs(parsed.query, keep_blank_values=False)

    # 优先读取常见的职位搜索字段。
    lowered = {key.lower(): values for key, values in query.items()}
    for key in PREFERRED_QUERY_KEYS:
        for value in lowered.get(key, []):
            label = _label_from_value(value)
            if label:
                return label

    # 再检查其他参数，例如业务线、职位类别等自定义字段。
    for values in query.values():
        for value in values:
            label = _label_from_value(value)
            if label:
                return label

    host = parsed.netloc.lower()
    if host == 'app.mokahr.com':
        lowered_url = str(url or '').lower()
        if 'moonshot' in lowered_url:
            return '月之暗面'
        if '/zphz/' in lowered_url:
            return '智谱'
        return 'Moka'

    known_label = KNOWN_SITE_LABELS.get(host)
    if known_label:
        return known_label

    return host or '招聘网址'


def build_site_labels(job_sites, configured_labels=None) -> Dict[str, str]:
    """为 URL 列表补齐标签，并保留用户手动编辑的非空标签。"""
    configured_labels = configured_labels if isinstance(configured_labels, dict) else {}
    result = {}
    for url in job_sites if isinstance(job_sites, list) else []:
        if not isinstance(url, str) or not url.strip():
            continue
        clean_url = url.strip()
        manual_label = configured_labels.get(clean_url, '')
        manual_label = str(manual_label).strip()[:100] if manual_label is not None else ''
        result[clean_url] = manual_label or derive_site_label(clean_url)
    return result


def get_site_label(config: dict, url: str) -> str:
    """读取单个网址的显示标签，兼容没有标签字段的旧配置。"""
    labels = config.get('job_site_labels', {}) if isinstance(config, dict) else {}
    return build_site_labels([url], labels).get(url, derive_site_label(url))


def normalize_site_mode(value) -> str:
    """Return a supported site mode; old configurations default to search."""
    mode = str(value or '').strip().lower()
    return mode if mode in VALID_SITE_MODES else SITE_MODE_SEARCH


def build_site_modes(job_sites, configured_modes=None) -> Dict[str, str]:
    """Fill the per-URL capture mode map while preserving old configs."""
    configured_modes = configured_modes if isinstance(configured_modes, dict) else {}
    result = {}
    for url in job_sites if isinstance(job_sites, list) else []:
        if not isinstance(url, str) or not url.strip():
            continue
        clean_url = url.strip()
        result[clean_url] = normalize_site_mode(configured_modes.get(clean_url))
    return result


def get_site_mode(config: dict, url: str) -> str:
    """Read one URL capture mode, defaulting legacy rows to keyword search."""
    modes = config.get('job_site_modes', {}) if isinstance(config, dict) else {}
    return build_site_modes([url], modes).get(url, SITE_MODE_SEARCH)


def combine_site_keyword_label(site_label: str, keyword: str) -> str:
    """组合职位标签，例如“美团”与“法务”组合为“美团法务”。

    兼容旧配置：若网址标签已经以该关键词结尾，则不重复追加。
    """
    base = str(site_label or '').strip() or '招聘网址'
    search_keyword = str(keyword or '').strip()
    if not search_keyword or base.endswith(search_keyword):
        return base[:100]
    return f'{base}{search_keyword}'[:100]


def combine_site_keyword_labels(
    site_label: str,
    keywords: Iterable[str],
) -> str:
    """Build one searchable label string for every keyword that found a job."""
    if isinstance(keywords, str):
        keywords = [keywords]

    labels = []
    for keyword in keywords or []:
        label = combine_site_keyword_label(site_label, keyword)
        if label and label not in labels:
            labels.append(label)

    if not labels:
        labels.append(combine_site_keyword_label(site_label, ''))
    return ' / '.join(labels)[:500]
