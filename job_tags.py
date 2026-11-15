import json
import math
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from job_identity import job_identity_hash
from site_labels import build_site_labels, build_site_modes


def captured_tags(job):
    mode = str(job.get('capture_mode') or 'unknown')
    if job.get('_fixed_url_mode') or mode == 'fixed':
        return '', 'fixed'
    terms = job.get('_search_keywords')
    keyword = str(job.get('matched_keyword') or job.get('_search_keyword') or
                  (terms[0] if isinstance(terms, list) and terms else '') or '').strip()
    return keyword, 'search' if keyword else mode


def legacy_tag_context(config, db_path):
    keywords = {str(value).strip() for value in config.get('job_keywords', []) if str(value).strip()}
    for url in config.get('job_sites', []):
        if config.get('job_site_modes', {}).get(url) == 'fixed':
            continue
        for key, values in parse_qs(urlparse(url).query).items():
            if key.lower() in ('keyword', 'keywords', 'q', 'query', 'positionname'):
                keywords.update(value.strip() for value in values if value.strip())
    path = Path(db_path)
    cache = {}
    if path.stem == 'jobs' or path.stem.startswith('jobs_'):
        try:
            cache = json.loads(path.with_name(path.stem.replace('jobs', 'jobs_cache', 1) + '.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    cache = cache if isinstance(cache, dict) else {}
    for entry in list(cache.values()):
        original = entry.get('job') if isinstance(entry, dict) else None
        if isinstance(original, dict) and original.get('url') and original.get('title'):
            cache.setdefault(job_identity_hash(original), entry)
    return keywords, cache


def recover_job_tags(job, keywords, cache):
    entry = cache.get(job.get('job_hash'), {})
    original = entry.get('job', {}) if isinstance(entry, dict) else {}
    if isinstance(original, dict):
        keyword, mode = captured_tags(original)
        if mode in ('fixed', 'search'):
            return keyword, mode
    label = str(job.get('site_label') or '').split(' / ', 1)[0].strip()
    company = str(job.get('company') or '').strip()
    suffix = label[len(company):].strip(' -·_：:') if company and label.startswith(company) else label
    if suffix and suffix in keywords:
        return suffix, 'search'
    return '', 'unknown'


def recruitment_site_resolver(config):
    """Resolve jobs to current configured labels, never to scraped company names."""
    sites = config.get('job_sites', [])
    labels = build_site_labels(sites, config.get('job_site_labels'))
    modes = build_site_modes(sites, config.get('job_site_modes'))
    entries = [(urlparse(url), labels[url], modes[url]) for url in sites if url in labels]
    keywords = {str(value).strip() for value in config.get('job_keywords', []) if str(value).strip()}
    generic_parts = {'apply', 'social-recruitment', 'jobs', 'index', 'position', 'list', 'detail',
                     'experienced', 'off-campus', 'zh', 'zh-cn', 'en'}

    def resolve(job):
        source = str(job.get('source_site') or '').strip().lower()
        host = urlparse(source if '://' in source else 'https://' + source).netloc
        detail = urlparse(str(job.get('url') or ''))
        candidates = [entry for entry in entries if entry[0].netloc.lower() == (host or detail.netloc.lower())]
        if not candidates:
            return ''
        stored = str(job.get('site_label') or '').split(' / ', 1)[0].strip()
        terms = keywords | {str(job.get('matched_keyword') or '').strip()}
        matches = [entry for entry in candidates if stored == entry[1] or
                   (stored.startswith(entry[1]) and stored[len(entry[1]):] in terms)]
        if matches:
            longest = max(len(entry[1]) for entry in matches)
            values = {entry[1] for entry in matches if len(entry[1]) == longest}
            if len(values) == 1:
                return next(iter(values))
        # Shared ATS hosts (e.g. Moka) identify the employer in the URL path.
        parts = set(detail.path.lower().strip('/').split('/')) - generic_parts - {''}
        routed = [entry for entry in candidates if parts &
                  (set(entry[0].path.lower().strip('/').split('/')) - generic_parts - {''})]
        if len({entry[1] for entry in routed}) == 1:
            return routed[0][1]
        # Historical search keywords may no longer appear in the current config.
        prefixes = [entry[1] for entry in candidates if stored.startswith(entry[1])]
        if prefixes:
            return max(prefixes, key=len)
        values = {entry[1] for entry in candidates}
        if len(values) == 1:
            return next(iter(values))
        mode = str(job.get('capture_mode') or '')
        by_mode = {entry[1] for entry in candidates if entry[2] == mode}
        return next(iter(by_mode)) if len(by_mode) == 1 else ''

    return resolve, list(dict.fromkeys(labels.values()))


def job_listing(database, config, arguments, per_page):
    from web_config import unique_site_labels_by_domain

    labels = unique_site_labels_by_domain(config)
    database.backfill_site_labels(labels)
    database.backfill_job_tags(config)
    keyword = arguments.get('keyword', '').strip()[:200]
    label = arguments.get('site_label', '').strip()[:100]
    resolve_site, site_options = recruitment_site_resolver(config)
    excluded_sites = list(dict.fromkeys(arguments.getlist('exclude_site') or arguments.getlist('exclude_company')))[:1000]
    excluded_sites = [value for value in excluded_sites if value in site_options]
    if site_options and set(site_options).issubset(excluded_sites):
        excluded_sites.append('')
    options = {
        'excluded_sites': excluded_sites,
        'site_tag_resolver': resolve_site,
        'excluded_keywords': list(dict.fromkeys(value[:200] for value in arguments.getlist('exclude_keyword')))[:1000],
    }
    total = database.count_jobs(keyword, label, **options)
    pages = math.ceil(total / per_page) if total else 0
    try:
        page = max(1, int(arguments.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    page = min(page, pages) if pages else 1
    jobs = database.search_jobs(keyword, label, limit=per_page, offset=(page - 1) * per_page, **options)
    for job in jobs:
        job['recruitment_site_tag'] = resolve_site(job)
        job['company_tag'] = job['recruitment_site_tag']
        job['site_label'] = job.get('site_label') or labels.get(str(job.get('source_site') or '').lower(), '') or job.get('source_site') or '未知网址'
    filters = {'sites': site_options, 'keywords': database.get_job_tag_options()['keywords']}
    return {'success': True, 'data': jobs, 'filters': filters,
            'pagination': {'page': page, 'per_page': per_page, 'total': total, 'total_pages': pages}}
