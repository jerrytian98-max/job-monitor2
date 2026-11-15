"""Shared Web configuration validation and safe browser representation."""

import copy
from urllib.parse import urlparse

from local_schedule import validate_schedule
from site_labels import build_site_labels, build_site_modes

SECRET_MASK = '••••••••'
JOBS_PER_PAGE = 24
AI_PROVIDER_FIELDS = {
    'gemini': ('gemini_api_key', 'gemini_model', 'gemini-3.5-flash-lite'),
    'openai': ('openai_api_key', 'openai_model', 'gpt-5.4-mini'),
    'deepseek': ('deepseek_api_key', 'deepseek_model', 'deepseek-flash'),
}


def normalize_ai_config(config: dict) -> dict:
    provider = str(config.get('ai_provider') or 'gemini').strip().lower()
    if provider not in AI_PROVIDER_FIELDS:
        raise ValueError('AI 服务商只能是 Gemini、OpenAI 或 DeepSeek')
    config['ai_provider'] = provider
    for key_field, model_field, default_model in AI_PROVIDER_FIELDS.values():
        config[key_field] = str(config.get(key_field) or '').strip()
        config[model_field] = str(config.get(model_field) or '').strip() or default_model
    config['ai_filter_prompt'] = str(config.get('ai_filter_prompt') or '').strip()
    active_key = config[AI_PROVIDER_FIELDS[provider][0]]
    enabled = config.get('ai_filter_enabled', bool(active_key and config['ai_filter_prompt']))
    if not isinstance(enabled, bool):
        raise ValueError('AI 筛选开关必须是布尔值')
    if enabled and not (active_key and config['ai_filter_prompt']):
        raise ValueError('启用 AI 筛选前请填写当前服务商的 API Key 和筛选要求')
    config['ai_filter_enabled'] = enabled
    return config


def redact_ai_keys(config: dict) -> dict:
    for key_field, _, _ in AI_PROVIDER_FIELDS.values():
        if config.get(key_field):
            config[key_field] = SECRET_MASK
    return config


def preserve_ai_keys(config: dict, existing: dict) -> dict:
    for key_field, model_field, _ in AI_PROVIDER_FIELDS.values():
        if key_field not in config or config[key_field] == SECRET_MASK:
            config[key_field] = existing.get(key_field, '')
        if model_field not in config and model_field in existing:
            config[model_field] = existing[model_field]
    if 'ai_provider' not in config and 'ai_provider' in existing:
        config['ai_provider'] = existing['ai_provider']
    return config


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError('配置必须是 JSON 对象')
    required = ('job_keywords', 'cities', 'exclude_keywords', 'job_sites', 'email', 'check_interval')
    missing = [field for field in required if field not in config]
    if missing:
        raise ValueError(f"缺少字段: {', '.join(missing)}")
    normalized = dict(config)
    for field in ('job_keywords', 'cities', 'exclude_keywords', 'job_sites'):
        value = normalized.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f'{field} 必须是字符串列表')
        normalized[field] = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    for url in normalized['job_sites']:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError(f'无效的招聘网址: {url}')
    raw_labels = normalized.get('job_site_labels', {})
    if raw_labels is not None and not isinstance(raw_labels, dict):
        raise ValueError('job_site_labels 必须是网址到标签的映射')
    normalized['job_site_labels'] = build_site_labels(normalized['job_sites'], raw_labels)
    raw_modes = normalized.get('job_site_modes', {})
    if raw_modes is not None and not isinstance(raw_modes, dict):
        raise ValueError('job_site_modes 必须是网址到抓取方式的映射')
    invalid = [url for url, mode in (raw_modes or {}).items()
               if url in normalized['job_sites'] and str(mode or '').strip().lower() not in ('', 'search', 'fixed')]
    if invalid:
        raise ValueError('抓取方式只能是 search 或 fixed')
    normalized['job_site_modes'] = build_site_modes(normalized['job_sites'], raw_modes)
    try:
        interval = float(normalized.get('check_interval', 2))
    except (TypeError, ValueError):
        raise ValueError('监测间隔必须是数字')
    if not 1 / 60 <= interval <= 168:
        raise ValueError('监测间隔必须在 1 分钟到 168 小时之间')
    normalized['check_interval'] = interval
    if 'local_schedule' in normalized:
        normalized['local_schedule'] = validate_schedule(normalized['local_schedule'])
    email = normalized.get('email', {})
    if not isinstance(email, dict):
        raise ValueError('email 必须是对象')
    normalized_email = {
        'sender': str(email.get('sender', '')).strip(),
        'auth_code': str(email.get('auth_code', '')).strip(),
        'receiver': str(email.get('receiver', '')).strip(),
        'smtp_server': str(email.get('smtp_server', '')).strip(),
    }
    try:
        normalized_email['smtp_port'] = int(email.get('smtp_port', 587))
    except (TypeError, ValueError):
        raise ValueError('SMTP 端口必须是整数')
    if not 1 <= normalized_email['smtp_port'] <= 65535:
        raise ValueError('SMTP 端口超出有效范围')
    normalized['email'] = normalized_email
    return normalize_ai_config(normalized)


def redact_config(config: dict) -> dict:
    public = copy.deepcopy(config)
    public['job_site_labels'] = build_site_labels(public.get('job_sites', []), public.get('job_site_labels', {}))
    public['job_site_modes'] = build_site_modes(public.get('job_sites', []), public.get('job_site_modes', {}))
    redact_ai_keys(public)
    email = public.get('email')
    if isinstance(email, dict) and email.get('auth_code'):
        email['auth_code'] = SECRET_MASK
    return public


def preserve_masked_secrets(config: dict, existing: dict) -> dict:
    preserve_ai_keys(config, existing)
    email = config.get('email')
    existing_email = existing.get('email', {})
    if isinstance(email, dict) and email.get('auth_code') == SECRET_MASK:
        email['auth_code'] = existing_email.get('auth_code', '')
    return config


def update_ai_filter_config(existing: dict, submitted: dict) -> dict:
    if not isinstance(submitted, dict):
        raise ValueError('AI 筛选配置格式错误')
    enabled = submitted.get('ai_filter_enabled')
    prompt = submitted.get('ai_filter_prompt')
    if not isinstance(enabled, bool) or not isinstance(prompt, str):
        raise ValueError('AI 筛选配置格式错误')
    updated = dict(existing)
    for key_field, model_field, _ in AI_PROVIDER_FIELDS.values():
        if key_field in submitted:
            key = submitted[key_field]
            if not isinstance(key, str):
                raise ValueError('AI 筛选配置格式错误')
            updated[key_field] = existing.get(key_field, '') if key == SECRET_MASK else key.strip()
        if model_field in submitted:
            model = submitted[model_field]
            if not isinstance(model, str):
                raise ValueError('AI 筛选配置格式错误')
            updated[model_field] = model.strip()
    if 'ai_provider' in submitted:
        updated['ai_provider'] = submitted['ai_provider']
    updated.update(ai_filter_prompt=prompt.strip(), ai_filter_enabled=enabled)
    return normalize_ai_config(updated)


def unique_site_labels_by_domain(config: dict) -> dict:
    sites = config.get('job_sites', []) if isinstance(config, dict) else []
    labels = build_site_labels(sites, config.get('job_site_labels', {}) if isinstance(config, dict) else {})
    grouped = {}
    for url in sites:
        domain = urlparse(url).netloc.lower()
        label = labels.get(url, '').strip()
        if domain and label:
            grouped.setdefault(domain, set()).add(label)
    return {domain: next(iter(values)) for domain, values in grouped.items() if len(values) == 1}
