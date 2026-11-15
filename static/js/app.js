// 招聘监测系统前端JavaScript

// 全局变量
let config = {};
let tags = {
    keywords: [],
    cities: [],
    exclude: []
};
let sites = [];
let expandedSiteIndex = null;
let sitesResizeObserver;
let currentPage = 1;
let currentRunsPage = 1;
let eventSource;
let jobsLoadController;
const excludedJobTags = {sites: new Set(), keywords: new Set()};
let jobTagOptions = {sites: [], keywords: []};
let jobSearchTimer;
const AI_PROVIDER_FIELDS = {
    gemini: {key: 'gemini_api_key', model: 'gemini_model', name: 'Gemini', defaultModel: 'gemini-3.5-flash-lite'},
    openai: {key: 'openai_api_key', model: 'openai_model', name: 'OpenAI', defaultModel: 'gpt-5.4-mini'},
    deepseek: {key: 'deepseek_api_key', model: 'deepseek_model', name: 'DeepSeek', defaultModel: 'deepseek-flash'}
};
let aiDraft = {};
let activeAiProvider = null;
let configReady = false;
let autosaveRevision = 0;
let savedRevision = 0;
let autosaveTimer;
let configSaveQueue = Promise.resolve(true);
let cancelAutosaveRetry;
const AUTOSAVE_RETRY_DELAYS = [2000, 5000, 10000, 20000];
let githubReady = false;
let githubRevision = 0;
let githubSavedRevision = 0;
let githubAutosaveTimer;
let githubSaveQueue = Promise.resolve(true);
let feedbackImages = [];
let feedbackSubmitting = false;
let monitorIsRunning = false;
let monitorTaskMessage = '';

function setAutosaveStatus(message, state = '', retryable = false) {
    document.querySelectorAll('.autosave-status').forEach(element => {
        element.textContent = message;
        element.dataset.state = state;
    });
    document.querySelectorAll('.autosave-retry').forEach(button => {
        button.hidden = state !== 'error' || !retryable;
    });
}

function interruptAutosaveRetry() {
    if (cancelAutosaveRetry) cancelAutosaveRetry();
}

function waitForAutosaveRetry(delay) {
    return new Promise(resolve => {
        const finish = retry => {
            clearTimeout(timer);
            if (cancelAutosaveRetry === cancel) cancelAutosaveRetry = null;
            resolve(retry);
        };
        const cancel = () => finish(false);
        const timer = setTimeout(() => finish(true), delay);
        cancelAutosaveRetry = cancel;
    });
}

function retryConfigSave() {
    return saveConfig({silent: true, commitPending: false});
}

async function requestConfigSave(configData) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(configData), signal: controller.signal,
        });
        let result;
        try {
            result = await response.json();
        } catch (error) {
            if (controller.signal.aborted) throw error;
        }
        let message = result?.message || '服务器返回异常，请稍后重试';
        if (response.status === 401) message = '登录已失效，请重新登录';
        else if (response.status === 403) message = '页面验证已失效，请刷新页面后重试';
        else if (response.status >= 500) message = '服务暂时不可用';
        if (!response.ok || result?.success !== true) {
            const retryable = response.status >= 500 || [408, 429].includes(response.status) || (response.ok && !result);
            throw Object.assign(new Error(message), {retryable});
        }
    } catch (error) {
        if (typeof error.retryable === 'boolean') throw error;
        throw Object.assign(new Error(controller.signal.aborted ? '保存请求超时' : '网络连接中断'), {retryable: true});
    } finally {
        clearTimeout(timeout);
    }
}

function scheduleAutosave() {
    if (!configReady) return;
    interruptAutosaveRetry();
    autosaveRevision++;
    setAutosaveStatus('修改待保存…', 'pending');
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => saveConfig({silent: true, commitPending: false}), 1200);
}

function syncSelectPicker(select) {
    const picker = select.closest('.ui-select');
    if (!picker) return;
    const selected = select.selectedOptions[0];
    picker.querySelector('.ui-select-value').textContent = selected?.textContent || '';
    picker.querySelector('.ui-select-trigger').setAttribute('aria-label',
        `${picker.dataset.label}: ${selected?.textContent || ''}`);
    picker.querySelectorAll('.ui-select-option').forEach((option, index) => {
        const active = select.options[index]?.selected || false;
        option.classList.toggle('selected', active);
        option.setAttribute('aria-selected', String(active));
    });
}

function enhanceSelect(select) {
    if (select.closest('.ui-select')) return;
    const picker = document.createElement('details');
    picker.className = `ui-select${select.classList.contains('site-mode-select') ? ' site-mode-picker' : ''}`;
    if (select.id) picker.id = `${select.id}Picker`;
    picker.dataset.label = select.getAttribute('aria-label') ||
        document.querySelector(`label[for="${select.id}"]`)?.textContent.trim() || select.title || '选择选项';
    const summary = document.createElement('summary');
    summary.className = 'ui-select-trigger';
    const value = document.createElement('span');
    value.className = 'ui-select-value';
    summary.appendChild(value);
    const menu = document.createElement('div');
    menu.className = 'ui-select-menu';
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', picker.dataset.label);
    Array.from(select.options).forEach(option => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'ui-select-option';
        item.setAttribute('role', 'option');
        item.tabIndex = -1;
        const label = document.createElement('span');
        label.className = 'ui-select-option-label';
        label.textContent = option.textContent;
        item.appendChild(label);
        item.addEventListener('click', () => {
            select.value = option.value;
            select.dispatchEvent(new Event('change', {bubbles: true}));
            picker.open = false;
            summary.focus();
        });
        menu.appendChild(item);
    });
    select.before(picker);
    picker.append(select, summary, menu);
    select.classList.add('ui-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    select.addEventListener('focus', () => summary.focus());
    select.addEventListener('change', () => syncSelectPicker(select));
    picker.addEventListener('toggle', () => {
        if (picker.open) {
            document.querySelectorAll('.ui-select[open]').forEach(other => {
                if (other !== picker) other.open = false;
            });
        }
    });
    summary.addEventListener('keydown', event => {
        if (event.key === 'Escape' && picker.open) {
            event.preventDefault();
            picker.open = false;
            return;
        }
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
        event.preventDefault();
        picker.open = true;
        const options = Array.from(menu.querySelectorAll('.ui-select-option'));
        options[Math.max(select.selectedIndex, 0)]?.focus();
    });
    menu.addEventListener('keydown', event => {
        const options = Array.from(menu.querySelectorAll('.ui-select-option'));
        const index = options.indexOf(document.activeElement);
        if (event.key === 'Escape') {
            event.preventDefault();
            picker.open = false;
            summary.focus();
        } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
                : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
            options[next]?.focus();
        }
    });
    picker.addEventListener('focusout', () => {
        setTimeout(() => {
            if (!picker.contains(document.activeElement)) picker.open = false;
        }, 0);
    });
    syncSelectPicker(select);
}

function rememberActiveAiFields() {
    if (!activeAiProvider) return;
    const fields = AI_PROVIDER_FIELDS[activeAiProvider];
    aiDraft[fields.key] = document.getElementById('aiApiKey').value;
    aiDraft[fields.model] = document.getElementById('aiModel').value;
}

function showAiProvider(provider) {
    rememberActiveAiFields();
    activeAiProvider = AI_PROVIDER_FIELDS[provider] ? provider : 'gemini';
    const fields = AI_PROVIDER_FIELDS[activeAiProvider];
    document.getElementById('aiProvider').value = activeAiProvider;
    syncSelectPicker(document.getElementById('aiProvider'));
    document.getElementById('aiApiKey').value = aiDraft[fields.key] || '';
    document.getElementById('aiModel').value = aiDraft[fields.model] || fields.defaultModel;
    document.getElementById('aiApiKeyLabel').textContent = `${fields.name} API Key`;
}

function collectAiSettings() {
    rememberActiveAiFields();
    return {
        ai_provider: activeAiProvider || 'gemini',
        gemini_api_key: aiDraft.gemini_api_key || '',
        gemini_model: aiDraft.gemini_model || AI_PROVIDER_FIELDS.gemini.defaultModel,
        openai_api_key: aiDraft.openai_api_key || '',
        openai_model: aiDraft.openai_model || AI_PROVIDER_FIELDS.openai.defaultModel,
        deepseek_api_key: aiDraft.deepseek_api_key || '',
        deepseek_model: aiDraft.deepseek_model || AI_PROVIDER_FIELDS.deepseek.defaultModel,
        ai_filter_prompt: document.getElementById('aiFilterPrompt').value,
        ai_filter_enabled: document.getElementById('aiFilterEnabled').checked,
    };
}

const cloudMode = Boolean(document.querySelector('meta[name="csrf-token"]'));
const nativeFetch = window.fetch.bind(window);
window.fetch = function(resource, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    if (cloudMode && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
        const headers = new Headers(options.headers || {});
        headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
        options = {...options, headers};
    }
    return nativeFetch(resource, options);
};

function escapeHtml(value) {
    const element = document.createElement('div');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

function syncWorkspacePage() {
    const section = window.location.hash.slice(1) || 'overview';
    const aiPage = section === 'ai-filter';
    const jobsPage = section === 'jobs';
    const activityPage = section === 'activity';
    const feedbackPage = section === 'feedback' && cloudMode;
    document.getElementById('overviewPage').hidden = aiPage || jobsPage || activityPage || feedbackPage;
    document.getElementById('jobsPage').hidden = !jobsPage;
    document.getElementById('activityPage').hidden = !activityPage;
    document.getElementById('aiFilterPage').hidden = !aiPage;
    if (document.getElementById('feedbackPage')) document.getElementById('feedbackPage').hidden = !feedbackPage;
    document.querySelectorAll('.sidebar-nav .nav-link').forEach(link => {
        link.classList.toggle('active', link.hash === `#${section}`);
    });
    if (window.matchMedia('(max-width: 780px)').matches) {
        document.querySelector('.sidebar-nav .nav-link.active')?.scrollIntoView({block: 'nearest', inline: 'nearest'});
    }
    const labels = {overview: '概览', jobs: '职位列表', sources: '监测来源', activity: '运行记录', schedule: '每周计划', settings: '监测配置', 'ai-filter': 'AI 筛选', feedback: '意见反馈'};
    document.getElementById('workspaceBreadcrumb').textContent = labels[section] || '概览';
    if (activityPage && cloudMode) loadRuns(currentRunsPage);
    if (aiPage || jobsPage || activityPage || feedbackPage || section === 'overview') {
        window.scrollTo(0, 0);
    } else if (section !== 'overview') {
        requestAnimationFrame(() => document.getElementById(section)?.scrollIntoView());
    }
}

function setFeedbackStatus(message, state = '') {
    const status = document.getElementById('feedbackStatus');
    status.textContent = message;
    status.dataset.state = state;
}

function renderFeedbackImages() {
    const container = document.getElementById('feedbackImages');
    container.replaceChildren();
    feedbackImages.forEach((item, index) => {
        const preview = document.createElement('div');
        preview.className = 'feedback-image-preview';
        const picture = document.createElement('img');
        picture.src = item.url;
        picture.alt = `反馈截图 ${index + 1}`;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-outline-secondary';
        remove.textContent = '移除';
        remove.setAttribute('aria-label', `移除截图 ${index + 1}`);
        remove.addEventListener('click', () => {
            URL.revokeObjectURL(item.url);
            feedbackImages.splice(index, 1);
            renderFeedbackImages();
        });
        preview.append(picture, remove);
        container.append(preview);
    });
}

function addFeedbackImages(files) {
    if (feedbackSubmitting) return;
    const images = Array.from(files);
    if (feedbackImages.length + images.length > 3) {
        setFeedbackStatus('每次最多添加 3 张图片', 'error');
        return;
    }
    if (images.some(file => !['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.type))) {
        setFeedbackStatus('请使用 PNG、JPEG、GIF 或 WebP 图片', 'error');
        return;
    }
    if (images.some(file => file.size > 5 * 1024 * 1024 || file.size === 0)) {
        setFeedbackStatus('每张图片不能超过 5 MB，且不能为空', 'error');
        return;
    }
    feedbackImages.push(...images.map(file => ({file, url: URL.createObjectURL(file)})));
    renderFeedbackImages();
    setFeedbackStatus('');
}

async function submitFeedback(event) {
    event.preventDefault();
    if (feedbackSubmitting) return;
    const text = document.getElementById('feedbackText');
    const content = text.value.trim();
    if (!content) {
        setFeedbackStatus('请填写问题描述', 'error');
        return;
    }
    const data = new FormData();
    data.append('content', content);
    feedbackImages.forEach(item => data.append('images', item.file));
    feedbackSubmitting = true;
    document.getElementById('feedbackFields').disabled = true;
    setFeedbackStatus('正在提交…', 'pending');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
        const response = await fetch('/api/feedback', {method: 'POST', body: data, signal: controller.signal});
        const result = await response.json().catch(() => null);
        if (response.status === 401) throw new Error('登录已失效，请重新登录后提交');
        if (response.status === 403) throw new Error('页面验证已失效，请刷新页面后重试');
        if (response.status === 413) throw new Error('图片总大小过大，请减少图片或压缩后重试');
        if (!response.ok || !result?.success) throw new Error(result?.message || '提交失败，请稍后重试');
        text.value = '';
        feedbackImages.forEach(item => URL.revokeObjectURL(item.url));
        feedbackImages = [];
        renderFeedbackImages();
        setFeedbackStatus(`反馈已提交（编号 ${result.data.id}），感谢你的反馈。`, 'saved');
    } catch (error) {
        setFeedbackStatus(error.name === 'AbortError' ? '提交超时，请稍后重试' : (error.message || '网络连接异常，请稍后重试'), 'error');
    } finally {
        clearTimeout(timeout);
        feedbackSubmitting = false;
        document.getElementById('feedbackFields').disabled = false;
    }
}

function initFeedbackForm() {
    const form = document.getElementById('feedbackForm');
    if (!form) return;
    form.addEventListener('submit', submitFeedback);
    const input = document.getElementById('feedbackImageInput');
    document.getElementById('feedbackAddImages').addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
        addFeedbackImages(input.files);
        input.value = '';
    });
    document.getElementById('feedbackText').addEventListener('paste', event => {
        const files = Array.from(event.clipboardData?.files || []);
        if (!files.length) return;
        event.preventDefault();
        const pastedText = event.clipboardData.getData('text/plain');
        if (pastedText) event.target.setRangeText(pastedText, event.target.selectionStart, event.target.selectionEnd, 'end');
        addFeedbackImages(files);
    });
}

function runTime(value) {
    if (!value) return '—';
    const date = new Date(`${value.replace(' ', 'T')}Z`);
    return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(date);
}

function runSites(sites, status) {
    if (sites === null) {
        if (status === 'queued') return '等待开始';
        if (status === 'running' || status === 'cancel_requested') return '运行中，结束后显示';
        return '历史任务未记录';
    }
    if (!sites.length) return status === 'queued' ? '等待开始' : '未抓取网页';
    const failedCount = sites.filter(site => site.status !== 'completed').length;
    return `<button type="button" class="run-sites-toggle" aria-expanded="false" onclick="toggleRunSites(this)"><span class="run-sites-chevron" aria-hidden="true">›</span>${sites.length} 个网页${failedCount ? `<span class="run-sites-warning">${failedCount} 个失败</span>` : ''}</button>`;
}

function toggleRunSites(button) {
    const row = button.closest('tr').nextElementSibling;
    if (!row?.classList.contains('run-sites-row')) return;
    const expanded = button.getAttribute('aria-expanded') !== 'true';
    row.hidden = !expanded;
    button.setAttribute('aria-expanded', String(expanded));
}

function runSiteList(sites) {
    const lines = sites.map(site => {
        const url = String(site.url || '');
        const safeUrl = safeHttpUrl(url);
        const name = String(site.label || (safeUrl !== '#' ? new URL(safeUrl).hostname : '未知网页'));
        const failed = site.status !== 'completed';
        const state = failed ? '失败' : `${site.scraped_jobs ?? 0}`;
        const description = `${name} · ${failed ? '抓取失败' : `抓取 ${state} 个职位`}`;
        const attributes = `class="run-site${failed ? ' run-site-failed' : ''}" title="${escapeHtml(description + '\n' + url).replace(/"/g, '&quot;')}"`;
        const content = `<strong>${escapeHtml(name)}</strong><span class="run-site-count">${escapeHtml(state)}</span>${safeUrl !== '#' ? '<span class="run-site-open" aria-hidden="true">↗</span>' : ''}`;
        return safeUrl !== '#' ?
            `<a ${attributes} href="${escapeHtml(safeUrl).replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHtml(description + '，打开网页').replace(/"/g, '&quot;')}">${content}</a>` :
            `<div ${attributes}>${content}</div>`;
    }).join('');
    return `<div class="run-sites-list">${lines}</div>`;
}

function renderRun(run) {
    const statusLabels = {
        queued: '排队中', running: '运行中', cancel_requested: '停止中',
        completed: '已完成', failed: '失败', cancelled: '已取消',
    };
    const failedSites = run.site_results?.filter(site => site.status === 'failed').length || 0;
    const allSitesFailed = run.status === 'completed' && failedSites > 0 && failedSites === run.site_results.length;
    const partial = run.status === 'completed' && failedSites > 0 && !allSitesFailed;
    const statusText = allSitesFailed ? '网页全部失败' : partial ? '已完成 · 部分网页失败' : (statusLabels[run.status] || '未知');
    const statusClass = allSitesFailed ? 'failed' : ['completed', 'failed', 'cancelled'].includes(run.status) ? run.status : 'active';
    const count = value => value === null || value === undefined ? '—' : escapeHtml(value);
    const mail = run.kind === 'test' ? '不发送' : run.notification_sent === true ? '已发送' :
        run.notification_sent === false && run.matched_jobs > 0 ? '未发送' : '—';
    return `<tr>
        <td class="run-time">${runTime(run.started_at)}</td>
        <td class="run-time">${runTime(run.finished_at)}</td>
        <td>${runSites(run.site_results, run.status)}</td>
        <td>${count(run.scraped_jobs)}</td>
        <td>${count(run.new_jobs)}</td>
        <td>${run.kind === 'test' ? '测试抓取' : '正式监测'}</td>
        <td><span class="run-status run-status-${statusClass}">${statusText}</span></td>
        <td>${mail}</td>
    </tr>${run.site_results?.length ? `<tr class="run-sites-row" hidden><td colspan="8"><div class="run-sites-panel"><span class="run-sites-heading">本次抓取网页</span>${runSiteList(run.site_results)}</div></td></tr>` : ''}`;
}

async function loadRuns(page = currentRunsPage) {
    if (!cloudMode) return;
    const body = document.getElementById('runsTableBody');
    const pagination = document.getElementById('runsPagination');
    body.innerHTML = '<tr><td colspan="8" class="run-empty">正在加载运行记录…</td></tr>';
    try {
        const response = await fetch(`/api/runs?page=${page}`);
        if (!response.ok) throw new Error('读取运行记录失败');
        const result = await response.json();
        if (!result.success) throw new Error(result.message || '读取运行记录失败');
        currentRunsPage = result.pagination.page;
        body.innerHTML = result.data.length ? result.data.map(renderRun).join('') :
            '<tr><td colspan="8" class="run-empty">暂无运行记录。启动一次监测后，这里会显示运行详情。</td></tr>';
        const {total, total_pages: totalPages} = result.pagination;
        pagination.hidden = totalPages <= 1;
        pagination.replaceChildren();
        if (totalPages > 1) {
            const summary = document.createElement('span');
            summary.textContent = `共 ${total} 次 · 第 ${currentRunsPage} / ${totalPages} 页`;
            const previous = document.createElement('button');
            previous.type = 'button';
            previous.className = 'btn btn-outline-secondary';
            previous.textContent = '上一页';
            previous.disabled = currentRunsPage === 1;
            previous.addEventListener('click', () => loadRuns(currentRunsPage - 1));
            const next = document.createElement('button');
            next.type = 'button';
            next.className = 'btn btn-outline-secondary';
            next.textContent = '下一页';
            next.disabled = currentRunsPage === totalPages;
            next.addEventListener('click', () => loadRuns(currentRunsPage + 1));
            pagination.append(summary, previous, next);
        }
    } catch (error) {
        body.innerHTML = `<tr><td colspan="8" class="run-empty">${escapeHtml(error.message)}</td></tr>`;
        pagination.hidden = true;
    }
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('select').forEach(enhanceSelect);
    document.addEventListener('pointerdown', event => {
        document.querySelectorAll('.ui-select[open]').forEach(picker => {
            if (!picker.contains(event.target)) picker.open = false;
        });
        const more = document.querySelector('.monitor-more[open]');
        if (more && !more.contains(event.target)) more.open = false;
    });
    window.addEventListener('hashchange', syncWorkspacePage);
    syncWorkspacePage();
    initFeedbackForm();
    loadConfig();
    if (!cloudMode) loadGithubSyncSettings();
    loadJobs();
    startStatusPolling();
    startEventStream();  // 启动实时事件流

    
    // 绑定表单提交事件（防止页面刷新）
    const configForm = document.getElementById('configForm');
    if (configForm) {
        configForm.addEventListener('submit', function(e) {
            e.preventDefault();
            scheduleAutosave();
        });
    }
    document.getElementById('aiProvider')?.addEventListener('change', event => showAiProvider(event.target.value));
    document.getElementById('aiFilterForm')?.addEventListener('submit', function(event) {
        event.preventDefault();
        scheduleAutosave();
    });
    const handleConfigChange = event => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        if (target.id === 'githubRepository' || target.id === 'githubToken') {
            scheduleGithubAutosave();
            return;
        }
        if (target.id === 'newSiteUrl' || target.id === 'newSiteMode') return;
        if (target.form?.id === 'configForm' || target.closest('#aiFilterForm')) scheduleAutosave();
    };
    document.addEventListener('input', handleConfigChange);
    document.addEventListener('change', handleConfigChange);
    window.addEventListener('beforeunload', event => {
        if (autosaveRevision !== savedRevision || githubRevision !== githubSavedRevision ||
            document.getElementById('feedbackText')?.value.trim() || feedbackImages.length) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
    window.addEventListener('online', () => {
        if (configReady && autosaveRevision !== savedRevision) retryConfigSave();
    });
    
    const newSiteUrl = document.getElementById('newSiteUrl');
    if (newSiteUrl) {
        newSiteUrl.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                addSite();
            }
        });
    }
});

// 启动Server-Sent Events
function startEventStream() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/api/events');

    eventSource.onmessage = function(event) {
        try {
            const data = JSON.parse(event.data);

            switch(data.type) {
                case 'status':
                    handleStatusEvent(data);
                    break;
                case 'job_found':
                    handleJobFoundEvent();
                    break;
                case 'heartbeat':
                    // 心跳，忽略
                    break;
            }
        } catch (e) {
            console.error('解析事件失败:', e);
        }
    };

    // The server returns finite batches. Native EventSource reconnects with
    // Last-Event-ID, preserving each tab's cursor and replaying missed events.
    eventSource.onerror = function() {};
}

// 处理状态事件
function handleStatusEvent(event) {
    const message = event.data.message;
    const type = event.data.type;

    monitorTaskMessage = message;
    const detail = document.getElementById('statusDetail');
    if (detail && monitorIsRunning) {
        detail.textContent = message;
        detail.dataset.state = type === 'error' ? 'error' : '';
    }

    // 工作日志只保留每个网址的开始和完成记录。
    if (event.data.work_log) {
        addLog(message, type);
    }

}

// 处理职位发现事件
function handleJobFoundEvent() {
    // 后端在推送事件前已经完成筛选和入库，无需再次抓取职位 URL。
    loadJobs(1);
    updateStatus();

}

// 添加工作日志
function addLog(message, type) {
    const logContainer = document.getElementById('workLog');
    if (!logContainer) return;

    logContainer.querySelector('.log-empty')?.remove();

    const time = new Date().toLocaleTimeString();
    const logClass = `log-${type}`;

    const entry = document.createElement('div');
    entry.className = 'log-entry mb-2';
    const timestamp = document.createElement('span');
    timestamp.className = 'log-time';
    timestamp.textContent = `[${time}] `;
    const text = document.createElement('span');
    text.className = logClass;
    text.textContent = message;
    entry.append(timestamp, text);
    logContainer.appendChild(entry);

    // 自动滚动到底部
    logContainer.scrollTop = logContainer.scrollHeight;
}

// 加载配置
async function loadConfig() {
    const sections = ['configForm', 'notificationScheduleCard', 'aiFilterForm']
        .map(id => document.getElementById(id)).filter(Boolean);
    sections.forEach(element => { element.inert = true; });
    try {
        const response = await fetch('/api/config');
        const result = await response.json();
        
        if (result.success) {
            config = result.data;
            
            // 填充表单
            renderTags('keywords', config.job_keywords || []);
            renderTags('cities', config.cities || []);
            renderTags('exclude', config.exclude_keywords || []);
            renderSites(
                config.job_sites || [],
                config.job_site_labels || {},
                config.job_site_modes || {}
            );
            
            // 每个服务商的密钥和模型独立保留；浏览器只收到密钥掩码。
            aiDraft = {...config};
            activeAiProvider = null;
            showAiProvider(config.ai_provider || 'gemini');
            const aiFilterPrompt = document.getElementById('aiFilterPrompt');
            if (aiFilterPrompt) aiFilterPrompt.value = config.ai_filter_prompt || '';
            const activeKey = AI_PROVIDER_FIELDS[activeAiProvider].key;
            document.getElementById('aiFilterEnabled').checked = config.ai_filter_enabled ?? Boolean(config[activeKey] && config.ai_filter_prompt);
            
            // 填充邮箱配置
            if (config.email) {
                const emailSender = document.getElementById('emailSender');
                if (emailSender) emailSender.value = config.email.sender || '';
                
                const emailAuthCode = document.getElementById('emailAuthCode');
                if (emailAuthCode) emailAuthCode.value = config.email.auth_code || '';
                
                const emailReceiver = document.getElementById('emailReceiver');
                if (emailReceiver) emailReceiver.value = config.email.receiver || '';
                
                const smtpServer = document.getElementById('smtpServer');
                if (smtpServer) smtpServer.value = config.email.smtp_server || 'smtp.gmail.com';
                
                const smtpPort = document.getElementById('smtpPort');
                if (smtpPort) smtpPort.value = config.email.smtp_port || 587;
            }
            
            // 填充监测间隔
            const schedule = config.local_schedule || (cloudMode
                ? {days: [], time: '21:00', enabled: false}
                : {days: [1, 4], time: '21:00', enabled: true});
            document.querySelectorAll('[name="scheduleDay"]').forEach(input => {
                input.checked = schedule.days.includes(Number(input.value));
            });
            document.getElementById('scheduleTime').value = schedule.time;
            document.getElementById('scheduleEnabled').checked = schedule.enabled;
            configReady = true;
            sections.forEach(element => { element.inert = false; });
            setAutosaveStatus('配置已同步', 'saved');
        } else {
            throw new Error(result.message || '加载配置失败');
        }
    } catch (error) {
        console.error('加载配置失败:', error);
        setAutosaveStatus('配置加载失败，请刷新页面重试', 'error');
        showMessage('加载配置失败', 'danger');
    }
}

// 保存配置
function saveConfig(options = {}) {
    clearTimeout(autosaveTimer);
    interruptAutosaveRetry();
    const save = configSaveQueue.then(() => saveConfigNow(options), () => saveConfigNow(options));
    configSaveQueue = save;
    return save;
}

async function saveConfigNow(options = {}) {
    const silent = options.silent === true;
    if (!configReady) {
        setAutosaveStatus('配置尚未加载，请刷新页面重试', 'error');
        return false;
    }
    const revision = autosaveRevision;
    setAutosaveStatus('正在保存…', 'pending');

    try {
        // A user may type a tag and click Save without pressing Enter first.
        // Commit that pending text so cities/exclusions are not silently lost.
        if (options.commitPending !== false) commitPendingTagInputs();

        // 收集表单数据
        const configData = {
            job_keywords: currentTagValues('keywords'),
            cities: currentTagValues('cities'),
            exclude_keywords: currentTagValues('exclude'),
            job_sites: sites.map(site => site.url),
            job_site_labels: Object.fromEntries(
                sites.map(site => [
                    site.url,
                    site.label.trim() || deriveSiteLabel(site.url)
                ])
            ),
            job_site_modes: Object.fromEntries(
                sites.map(site => [site.url, site.mode === 'fixed' ? 'fixed' : 'search'])
            ),
            ...collectAiSettings(),
            email: cloudMode ? {
                receiver: document.getElementById('emailReceiver')?.value || ''
            } : {
                sender: document.getElementById('emailSender')?.value || '',
                auth_code: document.getElementById('emailAuthCode')?.value || '',
                receiver: document.getElementById('emailReceiver')?.value || '',
                smtp_server: document.getElementById('smtpServer')?.value || 'smtp.gmail.com',
                smtp_port: parseInt(document.getElementById('smtpPort')?.value) || 587
            },
            check_interval: config.check_interval ?? 4,
            local_schedule: {
                days: Array.from(document.querySelectorAll('[name="scheduleDay"]:checked'), input => Number(input.value)),
                time: document.getElementById('scheduleTime').value,
                enabled: document.getElementById('scheduleEnabled').checked
            }
        };

        for (let attempt = 0; ; attempt++) {
            try {
                await requestConfigSave(configData);
                break;
            } catch (error) {
                if (revision !== autosaveRevision) return false;
                const delay = AUTOSAVE_RETRY_DELAYS[attempt];
                if (!error.retryable || delay === undefined) throw error;
                setAutosaveStatus(`保存暂时失败，${delay / 1000} 秒后自动重试…`, 'pending');
                if (!await waitForAutosaveRetry(delay) || revision !== autosaveRevision) return false;
                setAutosaveStatus('正在重新保存…', 'pending');
            }
        }
        savedRevision = revision;
        if (revision === autosaveRevision) setAutosaveStatus('所有更改已自动保存', 'saved');
        if (!silent) showMessage('配置保存成功', 'success');
        return true;
    } catch (error) {
        console.error('保存配置失败:', error);
        if (revision === autosaveRevision) setAutosaveStatus(`未保存：${error.message || '保存失败'}`, 'error', error.retryable === true);
        if (!silent) showMessage(error.message || '保存配置失败', 'danger');
        return false;
    }
}

function setGithubSyncStatus(message, type = 'muted') {
    const status = document.getElementById('githubSyncStatus');
    if (!status) return;
    status.textContent = message;
    status.className = `form-text mt-3 text-${type}`;
}

function setGithubSyncButtonBusy(buttonId, busy, busyText) {
    const button = document.getElementById(buttonId);
    if (!button) return;
    if (!button.dataset.originalHtml) {
        button.dataset.originalHtml = button.innerHTML;
    }
    button.disabled = busy;
    button.innerHTML = busy
        ? `<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>${escapeHtml(busyText)}`
        : button.dataset.originalHtml;
}

async function loadGithubSyncSettings() {
    const card = document.getElementById('githubSyncCard');
    if (card) card.inert = true;
    try {
        const response = await fetch('/api/github-sync/settings');
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.message || '读取 GitHub 设置失败');
        }
        const repository = document.getElementById('githubRepository');
        const token = document.getElementById('githubToken');
        if (repository) repository.value = result.data.repository || '';
        if (token) token.value = result.data.token || '';
        githubReady = true;
        if (card) card.inert = false;
        if (result.data.repository && result.data.has_token) {
            setGithubSyncStatus(
                `已连接 ${result.data.repository}，可以一键同步。`,
                'success'
            );
        }
    } catch (error) {
        console.warn('读取 GitHub 同步设置失败:', error);
        setGithubSyncStatus(error.message || '读取 GitHub 设置失败', 'danger');
    }
}

function scheduleGithubAutosave() {
    if (!githubReady) return;
    githubRevision++;
    setGithubSyncStatus('GitHub 设置待自动保存…');
    clearTimeout(githubAutosaveTimer);
    githubAutosaveTimer = setTimeout(() => saveGithubSyncSettings({silent: true}), 1200);
}

function saveGithubSyncSettings(options = {}) {
    clearTimeout(githubAutosaveTimer);
    const save = githubSaveQueue.then(() => saveGithubSyncSettingsNow(options), () => saveGithubSyncSettingsNow(options));
    githubSaveQueue = save;
    return save;
}

async function saveGithubSyncSettingsNow(options = {}) {
    const silent = options.silent === true;
    const revision = githubRevision;
    const repository = document.getElementById('githubRepository')?.value.trim() || '';
    const token = document.getElementById('githubToken')?.value.trim() || '';

    try {
        const response = await fetch('/api/github-sync/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({repository, token})
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.message || '保存 GitHub 连接失败');
        }
        const tokenInput = document.getElementById('githubToken');
        githubSavedRevision = revision;
        if (revision === githubRevision) {
            if (tokenInput) tokenInput.value = result.data.token || '';
            setGithubSyncStatus(`已保存 ${result.data.repository} 的连接设置。`, 'success');
        }
        if (!silent) showMessage(result.message, 'success');
        return true;
    } catch (error) {
        console.warn('保存 GitHub 同步设置失败:', error);
        if (revision === githubRevision) setGithubSyncStatus(`未保存：${error.message || 'GitHub 连接保存失败'}`, 'danger');
        if (!silent) showMessage(error.message || '保存 GitHub 连接失败', 'danger');
        return false;
    }
}

async function uploadConfigToGithub() {
    setGithubSyncButtonBusy('uploadGithubConfigBtn', true, '正在上传...');
    try {
        const configSaved = await saveConfig({silent: true, reload: false});
        if (!configSaved) throw new Error('本地配置保存失败，已停止上传');
        const settingsSaved = await saveGithubSyncSettings({silent: true});
        if (!settingsSaved) throw new Error('请先正确填写 GitHub 仓库和 Token');

        setGithubSyncStatus('正在加密并上传本地配置...', 'primary');
        const response = await fetch('/api/github-sync/upload-config', {
            method: 'POST'
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.message || '上传配置失败');
        }
        setGithubSyncStatus(
            `配置已同步到 ${result.data.repository}。`,
            'success'
        );
        showMessage(result.message, 'success');
    } catch (error) {
        console.warn('一键上传配置失败:', error);
        setGithubSyncStatus(error.message || '上传配置失败', 'danger');
        showMessage(error.message || '上传配置失败', 'danger');
    } finally {
        setGithubSyncButtonBusy('uploadGithubConfigBtn', false, '');
    }
}

async function downloadJobsFromGithub() {
    setGithubSyncButtonBusy('downloadGithubJobsBtn', true, '正在下载...');
    try {
        const settingsSaved = await saveGithubSyncSettings({silent: true});
        if (!settingsSaved) throw new Error('请先正确填写 GitHub 仓库和 Token');

        setGithubSyncStatus('正在下载并校验 GitHub 职位数据...', 'primary');
        const response = await fetch('/api/github-sync/download-jobs', {
            method: 'POST'
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.message || '下载职位数据失败');
        }
        const backupMessage = result.data.backup_directory
            ? '，本地旧数据已备份'
            : '';
        setGithubSyncStatus(
            `${result.message}${backupMessage}。`,
            'success'
        );
        showMessage(result.message, 'success');
        await loadJobs(1);
        await updateStatus();
    } catch (error) {
        console.warn('一键下载职位数据失败:', error);
        setGithubSyncStatus(error.message || '下载职位数据失败', 'danger');
        showMessage(error.message || '下载职位数据失败', 'danger');
    } finally {
        setGithubSyncButtonBusy('downloadGithubJobsBtn', false, '');
    }
}

// 渲染标签
function renderTags(type, tagList) {
    tags[type] = tagList;
    const container = document.getElementById(`${type}Input`);
    if (!container) return;

    const input = container.querySelector('input');
    
    // 清除现有标签（保留输入框）
    const existingTags = container.querySelectorAll('.tag');
    existingTags.forEach(tag => tag.remove());
    
    // 添加新标签
    tagList.forEach(tagText => {
        const tag = createTagElement(tagText, type);
        container.insertBefore(tag, input);
    });
}

// 创建标签元素
function createTagElement(text, type) {
    const tag = document.createElement('div');
    tag.className = 'tag';
    const label = document.createElement('span');
    label.textContent = text;
    const close = document.createElement('span');
    close.className = 'close-btn';
    close.textContent = '×';
    close.addEventListener('click', () => removeTag(type, text));
    tag.append(label, close);
    return tag;
}

// 添加标签
function addTag(event, type) {
    event.preventDefault();
    const input = event.target;
    const value = input.value.trim();
    
    if (value && !tags[type].includes(value)) {
        tags[type].push(value);
        const tag = createTagElement(value, type);
        const container = document.getElementById(`${type}Input`);
        container.insertBefore(tag, input);
        input.value = '';
        scheduleAutosave();
    }
}

function currentTagValues(type) {
    const input = document.querySelector(`#${type}Input input`);
    const pending = (input?.value || '').split(/[，,、;；\n]+/).map(value => value.trim()).filter(Boolean);
    return Array.from(new Set([...tags[type], ...pending]));
}

function commitPendingTagInputs() {
    ['keywords', 'cities', 'exclude'].forEach(type => {
        const input = document.querySelector(`#${type}Input input`);
        if (!input) return;

        const values = input.value
            .split(/[，,、;；\n]+/)
            .map(value => value.trim())
            .filter(Boolean);
        values.forEach(value => {
            if (!tags[type].includes(value)) tags[type].push(value);
        });
        if (values.length) {
            input.value = '';
            renderTags(type, tags[type]);
        }
    });
}

// 删除标签
function removeTag(type, text) {
    const index = tags[type].indexOf(text);
    if (index > -1) {
        tags[type].splice(index, 1);
        renderTags(type, tags[type]);
        scheduleAutosave();
    }
}

function extractChineseLabel(value) {
    const fragments = String(value || '').match(/[\u3400-\u9fff]+/g);
    return fragments ? fragments.join(' ').slice(0, 100) : '';
}

// 从 URL 编码后的查询参数中提取中文关键词；没有中文时返回空字符串。
function deriveChineseSiteLabel(url) {
    try {
        const parsed = new URL(url);
        const preferredKeys = ['keywords', 'keyword', 'query', 'q', 'key', 'search', 'position'];

        for (const key of preferredKeys) {
            for (const value of parsed.searchParams.getAll(key)) {
                const label = extractChineseLabel(value);
                if (label) return label;
            }
        }

        for (const [, value] of parsed.searchParams.entries()) {
            const label = extractChineseLabel(value);
            if (label) return label;
        }

        return '';
    } catch (_) {
        return '';
    }
}

// 从 URL 编码后的查询参数自动识别中文标签。
function deriveSiteLabel(url) {
    const chineseLabel = deriveChineseSiteLabel(url);
    if (chineseLabel) return chineseLabel;

    try {
        const parsed = new URL(url);
        const knownLabels = {
            'jobs.bytedance.com': '字节',
            'zhaopin.meituan.com': '美团',
            'talent.quark.cn': '夸克',
            'careers.tencent.com': '腾讯',
            'talent.ele.me': '饿了么',
            'careers.aliyun.com': '阿里云',
            'job.xiaohongshu.com': '小红书',
            'careers.pddglobalhr.com': '拼多多',
            'jobs.bilibili.com': 'B站'
        };
        if (parsed.hostname === 'app.mokahr.com') {
            const lowerUrl = url.toLowerCase();
            if (lowerUrl.includes('moonshot')) return '月之暗面';
            if (lowerUrl.includes('/zphz/')) return '智谱';
            return 'Moka';
        }
        return knownLabels[parsed.hostname] || parsed.hostname || '招聘网址';
    } catch (_) {
        return '招聘网址';
    }
}

// URL 关键词变化时，只替换标签中与旧 URL 对应的部分，保留手动添加的前后缀。
function updateLabelForChangedUrl(label, previousUrlLabel, nextUrlLabel) {
    const currentLabel = String(label || '');
    const previousPart = String(previousUrlLabel || '').trim();
    const nextPart = String(nextUrlLabel || '').trim();

    if (!nextPart) return currentLabel;
    if (!currentLabel) return nextPart;
    if (previousPart === nextPart) return currentLabel;

    const relatedPartIndex = previousPart ? currentLabel.indexOf(previousPart) : -1;
    if (relatedPartIndex !== -1) {
        return (
            currentLabel.slice(0, relatedPartIndex)
            + nextPart
            + currentLabel.slice(relatedPartIndex + previousPart.length)
        );
    }

    if (currentLabel.includes(nextPart)) return currentLabel;
    return `${currentLabel}${nextPart}`;
}

function normalizeSites(siteList, siteLabelMap = null, siteModeMap = null) {
    return (Array.isArray(siteList) ? siteList : []).map(site => {
        if (typeof site === 'string') {
            const manualLabel = siteLabelMap && typeof siteLabelMap === 'object'
                ? String(siteLabelMap[site] || '').trim()
                : '';
            const configuredMode = siteModeMap && typeof siteModeMap === 'object'
                ? String(siteModeMap[site] || '').trim().toLowerCase()
                : '';
            return {
                url: site,
                label: manualLabel || deriveSiteLabel(site),
                urlLabel: deriveChineseSiteLabel(site),
                mode: configuredMode === 'fixed' ? 'fixed' : 'search'
            };
        }
        const url = String(site?.url || '').trim();
        const label = String(site?.label || '').trim();
        return {
            url,
            label: label || deriveSiteLabel(url),
            urlLabel: deriveChineseSiteLabel(url),
            mode: String(site?.mode || '').trim().toLowerCase() === 'fixed'
                ? 'fixed'
                : 'search'
        };
    }).filter(site => site.url);
}

const SITE_LABEL_COLORS = [
    {background: '#e8edff', text: '#4454b8', border: '#cbd4ff'},
    {background: '#e5f5ed', text: '#277653', border: '#bee6d1'},
    {background: '#fff0dc', text: '#a65d0b', border: '#f6d7ad'},
    {background: '#fbe7f0', text: '#a13f6d', border: '#f1c7da'},
    {background: '#ece8fb', text: '#624db0', border: '#d8cff4'},
    {background: '#e2f3f6', text: '#207386', border: '#bce2e9'},
    {background: '#f8eadf', text: '#885734', border: '#ead0bc'},
    {background: '#f6e7e7', text: '#98454d', border: '#e9c6c9'}
];

// 标签前两个字决定颜色：前缀相同的标签始终使用同一种颜色。
function getSiteLabelColor(label) {
    const prefix = Array.from(String(label || '').trim()).slice(0, 2).join('');
    if (!prefix) {
        return {background: '#e9ecef', text: '#495057', border: '#ced4da'};
    }

    const hash = Array.from(prefix).reduce(
        (value, character) => ((value * 31) + character.codePointAt(0)) >>> 0,
        0
    );
    return SITE_LABEL_COLORS[hash % SITE_LABEL_COLORS.length];
}

function updateSiteLabelAppearance(input, label) {
    const color = getSiteLabelColor(label);

    input.style.setProperty('--site-label-bg', color.background);
    input.style.setProperty('--site-label-text', color.text);
    input.style.setProperty('--site-label-border', color.border);
}

// 渲染可编辑的网址与标签列表。
function positionSiteEditor() {
    if (expandedSiteIndex === null) return;
    const container = document.getElementById('sitesList');
    const chip = container.querySelector(`.site-chip[data-site-index="${expandedSiteIndex}"]`);
    const panel = container.querySelector(`.site-editor[data-site-index="${expandedSiteIndex}"]`);
    if (!chip || !panel || !container.clientWidth) return;
    panel.hidden = true;
    const rowTop = chip.offsetTop;
    const row = Array.from(container.querySelectorAll('.site-chip')).filter(item => Math.abs(item.offsetTop - rowTop) < 2);
    row[row.length - 1].after(panel);
    panel.hidden = false;
    const anchor = chip.getBoundingClientRect().left + chip.getBoundingClientRect().width / 2 - panel.getBoundingClientRect().left;
    panel.style.setProperty('--site-bubble-anchor', `${Math.max(16, Math.min(panel.clientWidth - 16, anchor))}px`);
}

function expandSiteEditor(index) {
    const container = document.getElementById('sitesList');
    expandedSiteIndex = index !== null && sites[index] ? index : null;
    container.querySelectorAll('.site-editor').forEach(panel => { panel.hidden = true; });
    container.querySelectorAll('.site-chip').forEach(chip => {
        const siteIndex = Number(chip.dataset.siteIndex);
        const active = siteIndex === expandedSiteIndex;
        const toggle = chip.querySelector('.site-chip-toggle');
        chip.classList.toggle('is-expanded', active);
        chip.querySelector('.site-label-input').hidden = !active;
        toggle.textContent = active ? '⌃' : (sites[siteIndex].label || deriveSiteLabel(sites[siteIndex].url));
        toggle.setAttribute('aria-expanded', String(active));
        toggle.setAttribute('aria-label', `${active ? '收起' : '展开'} ${sites[siteIndex].label || '招聘网址'}`);
        toggle.title = active ? '收起网址设置' : sites[siteIndex].label;
    });
    positionSiteEditor();
}

function renderSites(siteList, siteLabelMap = null, siteModeMap = null) {
    sites = normalizeSites(siteList, siteLabelMap, siteModeMap);
    const container = document.getElementById('sitesList');
    if (!container) return;

    container.innerHTML = '';
    if (expandedSiteIndex !== null && !sites[expandedSiteIndex]) expandedSiteIndex = null;
    
    sites.forEach((site, index) => {
        const siteDiv = document.createElement('div');
        siteDiv.className = 'site-url site-editor';
        siteDiv.id = `site-editor-${index}`;
        siteDiv.dataset.siteIndex = String(index);
        siteDiv.hidden = true;
        siteDiv.setAttribute('role', 'group');
        siteDiv.setAttribute('aria-label', `${site.label}的网址设置`);

        const chip = document.createElement('div');
        chip.className = 'site-chip';
        chip.dataset.siteIndex = String(index);
        updateSiteLabelAppearance(chip, site.label);
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'site-chip-toggle';
        toggle.setAttribute('aria-controls', siteDiv.id);
        toggle.addEventListener('click', () => expandSiteEditor(expandedSiteIndex === index ? null : index));

        const labelInput = document.createElement('input');
        labelInput.type = 'text';
        labelInput.className = 'site-label-input';
        labelInput.maxLength = 100;
        labelInput.value = site.label;
        labelInput.title = '点击编辑标签';
        labelInput.setAttribute('aria-label', `编辑网址 ${index + 1} 的标签`);
        updateSiteLabelAppearance(labelInput, site.label);
        labelInput.addEventListener('input', () => {
            sites[index].label = labelInput.value;
            updateSiteLabelAppearance(labelInput, labelInput.value);
            updateSiteLabelAppearance(chip, labelInput.value);
            siteDiv.setAttribute('aria-label', `${labelInput.value || '招聘网址'}的网址设置`);
        });
        chip.append(labelInput, toggle);

        const urlInput = document.createElement('input');
        urlInput.type = 'text';
        urlInput.className = 'site-url-input';
        urlInput.value = site.url;
        urlInput.title = '点击编辑招聘网址';
        urlInput.spellcheck = false;
        urlInput.setAttribute('aria-label', `编辑网址 ${index + 1} 的URL`);
        urlInput.addEventListener('input', () => {
            const editedUrl = urlInput.value.trim();
            sites[index].url = editedUrl;

            try {
                const parsed = new URL(editedUrl);
                if (!['http:', 'https:'].includes(parsed.protocol)) return;
                const nextUrlLabel = deriveChineseSiteLabel(editedUrl);
                if (!nextUrlLabel) return;

                const updatedLabel = updateLabelForChangedUrl(
                    sites[index].label,
                    sites[index].urlLabel,
                    nextUrlLabel
                );
                sites[index].urlLabel = nextUrlLabel;
                sites[index].label = updatedLabel;
                labelInput.value = updatedLabel;
                updateSiteLabelAppearance(labelInput, updatedLabel);
                updateSiteLabelAppearance(chip, updatedLabel);
            } catch (_) {
                // 输入尚未完成时保留当前标签，待 URL 合法后再自动更新。
            }
        });

        const modeSelect = document.createElement('select');
        modeSelect.className = 'site-mode-select';
        modeSelect.title = '选择该网址的抓取方式';
        modeSelect.setAttribute('aria-label', `选择网址 ${index + 1} 的抓取方式`);
        const searchOption = new Option('关键词搜索', 'search');
        const fixedOption = new Option('固定直抓', 'fixed');
        modeSelect.append(searchOption, fixedOption);
        modeSelect.value = site.mode === 'fixed' ? 'fixed' : 'search';
        modeSelect.addEventListener('change', () => {
            sites[index].mode = modeSelect.value === 'fixed' ? 'fixed' : 'search';
        });

        const actions = document.createElement('div');
        actions.className = 'site-actions';

        const duplicate = document.createElement('button');
        duplicate.type = 'button';
        duplicate.className = 'btn btn-link duplicate-btn';
        duplicate.setAttribute('aria-label', `在网址 ${index + 1} 下方复制一条`);
        duplicate.title = '复制此网址';
        const duplicateIcon = document.createElement('span');
        duplicateIcon.textContent = '+';
        duplicateIcon.setAttribute('aria-hidden', 'true');
        duplicate.appendChild(duplicateIcon);
        duplicate.addEventListener('click', () => duplicateSite(index));

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-link delete-btn';
        remove.title = '删除此网址';
        remove.setAttribute('aria-label', `删除网址 ${index + 1}`);
        const removeIcon = document.createElement('span');
        removeIcon.textContent = '×';
        removeIcon.setAttribute('aria-hidden', 'true');
        remove.appendChild(removeIcon);
        remove.addEventListener('click', () => removeSite(index));

        actions.append(duplicate, remove);
        siteDiv.append(urlInput, modeSelect, actions);
        container.append(chip, siteDiv);
        enhanceSelect(modeSelect);
    });
    expandSiteEditor(expandedSiteIndex);
    container.onkeydown = event => {
        if (event.key !== 'Escape' || expandedSiteIndex === null || event.target.closest('.ui-select[open]')) return;
        const toggle = container.querySelector(`.site-chip[data-site-index="${expandedSiteIndex}"] .site-chip-toggle`);
        expandSiteEditor(null);
        toggle?.focus();
    };
    if (!sitesResizeObserver) {
        let previousWidth = container.clientWidth;
        sitesResizeObserver = new ResizeObserver(() => {
            if (container.clientWidth === previousWidth) return;
            previousWidth = container.clientWidth;
            positionSiteEditor();
        });
        sitesResizeObserver.observe(container);
    }
}

// 添加网站
function addSite() {
    const input = document.getElementById('newSiteUrl');
    const url = input.value.trim();
    const mode = document.getElementById('newSiteMode')?.value === 'fixed'
        ? 'fixed'
        : 'search';
    
    let parsed;
    try {
        parsed = new URL(url);
    } catch (_) {
        showMessage('请输入有效的招聘网址', 'warning');
        return;
    }

    if (!['http:', 'https:'].includes(parsed.protocol)) {
        showMessage('招聘网址必须以 http:// 或 https:// 开头', 'warning');
        return;
    }

    if (sites.some(site => site.url === url)) {
        showMessage('这个招聘网址已经在列表中', 'warning');
        return;
    }

    if (url) {
        const label = deriveSiteLabel(url);
        sites.push({url, label, mode});
        renderSites(sites);
        input.value = '';
        scheduleAutosave();
        showMessage(
            `已添加${mode === 'fixed' ? '固定直抓' : '关键词搜索'}网址，自动标签：${label}`,
            'success'
        );
    }
}

// 删除网站
function removeSite(index) {
    if (expandedSiteIndex === index) expandedSiteIndex = null;
    else if (expandedSiteIndex !== null && expandedSiteIndex > index) expandedSiteIndex--;
    sites.splice(index, 1);
    renderSites(sites);
    scheduleAutosave();
}

function duplicateSite(index) {
    const source = sites[index];
    if (!source) return;

    sites.splice(index + 1, 0, {
        url: source.url,
        label: source.label,
        urlLabel: source.urlLabel,
        mode: source.mode
    });
    expandedSiteIndex = index + 1;
    renderSites(sites);
    scheduleAutosave();

    // 优先选中搜索关键词参数，便于直接替换；没有参数时选中整个 URL。
    requestAnimationFrame(() => {
        const editor = document.querySelector(`.site-editor[data-site-index="${index + 1}"] .site-url-input`);
        if (!editor) return;

        editor.focus();
        const match = editor.value.match(/[?&](?:keywords?|query|q)=/i);
        if (match && match.index !== undefined) {
            const start = match.index + match[0].length;
            const nextParameter = editor.value.indexOf('&', start);
            editor.setSelectionRange(
                start,
                nextParameter === -1 ? editor.value.length : nextParameter
            );
        } else {
            editor.select();
        }
    });
}

// 启动监测
function startMonitor() {
    clearTimeout(autosaveTimer);
    interruptAutosaveRetry();
    const start = configSaveQueue.then(startMonitorNow, startMonitorNow);
    configSaveQueue = start;
    return start;
}

async function startMonitorNow() {
    try {
        // “开始监测”始终先保存当前页面，避免刚添加的网址尚未写入配置。
        const saved = await saveConfigNow({silent: true});
        if (!saved) return;

        const workLog = document.getElementById('workLog');
        if (workLog) workLog.innerHTML = '';
        monitorTaskMessage = '';

        const response = await fetch('/api/monitor/start', {
            method: 'POST'
        });
        const result = await response.json();
        
        if (result.success) {
            if (!cloudMode) {
                document.getElementById('scheduleEnabled').checked = true;
                scheduleAutosave();
            }
            showMessage(result.message, 'success');
            updateButtons(true);
            setTimeout(updateStatus, 1000);
        } else {
            showMessage(result.message || '启动失败', 'danger');
        }
    } catch (error) {
        console.error('启动监测失败:', error);
        showMessage('启动监测失败', 'danger');
    }
}

// 停止监测
function stopMonitor() {
    clearTimeout(autosaveTimer);
    interruptAutosaveRetry();
    const stop = configSaveQueue.then(stopMonitorNow, stopMonitorNow);
    configSaveQueue = stop;
    return stop;
}

async function stopMonitorNow() {
    try {
        const response = await fetch('/api/monitor/stop', {
            method: 'POST'
        });
        const result = await response.json();
        
        if (result.success) {
            document.getElementById('scheduleEnabled').checked = false;
            scheduleAutosave();
            showMessage(result.message, 'success');
            updateButtons(false);
            setTimeout(updateStatus, 1000);
        } else {
            showMessage(result.message || '停止失败', 'danger');
        }
    } catch (error) {
        console.error('停止监测失败:', error);
        showMessage('停止监测失败', 'danger');
    }
}

// 测试检查
async function testCheck() {
    try {
        if (!await saveConfig({silent: true})) {
            showMessage('配置尚未保存，请检查保存状态', 'warning');
            return;
        }
        showMessage('正在执行测试检查...', 'info');
        const response = await fetch('/api/test/check', {
            method: 'POST'
        });
        const result = await response.json();
        
        if (result.success) {
            const count = result.data?.matched_jobs || 0;
            showMessage(result.data?.queued ? '测试抓取已排队，完成后会显示在工作日志' : `测试检查完成，找到 ${count} 个符合条件的职位`, 'success');
        } else {
            showMessage(result.message || '测试失败', 'danger');
        }
    } catch (error) {
        console.error('测试检查失败:', error);
        showMessage('测试检查失败', 'danger');
    }
}

function monitorDate(value) {
    if (!value) return null;
    const normalized = String(value).replace(' ', 'T');
    const date = new Date(cloudMode && !/(Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? `${normalized}Z` : normalized);
    return Number.isNaN(date.getTime()) ? null : date;
}

function updateLastCheck(value) {
    const element = document.getElementById('lastCheck');
    if (!element) return;
    const date = monitorDate(value);
    if (!date) {
        element.textContent = '尚未检查';
        element.removeAttribute('title');
        return;
    }
    const options = cloudMode ? {timeZone: 'Asia/Shanghai'} : {};
    element.title = new Intl.DateTimeFormat('zh-CN', {...options, dateStyle: 'medium', timeStyle: 'medium'}).format(date);
    const minutes = Math.floor((Date.now() - date.getTime()) / 60000);
    if (minutes >= 0 && minutes < 1) element.textContent = '刚刚';
    else if (minutes >= 1 && minutes < 60) element.textContent = `${minutes} 分钟前`;
    else element.textContent = new Intl.DateTimeFormat('zh-CN', {
        ...options, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
}

function updateMonitorRuntime(status) {
    const element = document.getElementById('runningTime');
    if (!element) return;
    if (status.elapsed_time) {
        element.textContent = status.elapsed_time;
        return;
    }
    const started = monitorDate(status.running_time);
    if (!started) {
        element.textContent = '等待开始';
        return;
    }
    const seconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
    element.textContent = `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

async function updateStatus() {
    try {
        const response = await fetch('/api/monitor/status');
        const result = await response.json();
        
        if (result.success) {
            const status = result.data;
            const isRunning = status.is_monitoring;
            if (isRunning && !monitorIsRunning) monitorTaskMessage = '';
            
            // 更新状态显示
            const statusDiv = document.getElementById('monitorStatus');
            const statusText = document.getElementById('statusText');
            const statusDetail = document.getElementById('statusDetail');

            if (statusDiv && statusText && statusDetail) {
                statusDetail.dataset.state = '';
                if (status.status === 'stopping') {
                    statusDiv.className = 'monitor-status running';
                    statusText.textContent = '正在停止检查';
                    statusDetail.textContent = '当前页面抓取完成后将安全退出';
                } else if (isRunning) {
                    statusDiv.className = 'monitor-status running';
                    const queued = cloudMode && !status.running_time;
                    statusText.textContent = queued ? '等待检查' : (cloudMode ? '正在检查职位' : '监测运行中');
                    statusDetail.textContent = monitorTaskMessage || (queued ? '任务已排队，轮到后会自动开始。' : '正在检查招聘网页，请稍候。');
                } else if (cloudMode && status.scheduled) {
                    statusDiv.className = 'monitor-status scheduled';
                    statusText.textContent = '定时监测已开启';
                    statusDetail.textContent = '按每周计划自动检查，也可立即检查一次。';
                } else {
                    statusDiv.className = 'monitor-status stopped';
                    statusText.textContent = '暂未运行';
                    statusDetail.textContent = status.last_check ? '随时可以检查新职位，或开启每周定时监测。' : '设置好招聘网址后，即可检查新职位。';
                }
            }
            
            // 更新统计数据
            const totalJobs = document.getElementById('totalJobs');
            if (totalJobs) totalJobs.textContent = Number(status.total_jobs_found || 0).toLocaleString('zh-CN');

            const newJobsToday = document.getElementById('newJobsToday');
            if (newJobsToday) newJobsToday.textContent = Number(status.new_jobs_today || 0).toLocaleString('zh-CN');
            
            // 更新时间
            updateLastCheck(status.last_check);
            updateMonitorRuntime(status);
            
            // 更新按钮状态
            updateButtons(isRunning);
            if (status.status === 'stopping') document.getElementById('stopBtn').disabled = true;
        }
    } catch (error) {
        console.error('获取状态失败:', error);
    }
}

// 更新按钮状态
function updateButtons(isRunning) {
    monitorIsRunning = Boolean(isRunning);
    const startBtn = document.getElementById('startBtn');
    const stopBtn = document.getElementById('stopBtn');
    
    if (startBtn && stopBtn) {
        stopBtn.hidden = !isRunning;
        if (isRunning) {
            startBtn.disabled = true;
            stopBtn.disabled = false;
        } else {
            startBtn.disabled = false;
            stopBtn.disabled = true;
        }
    }

    const testButton = document.getElementById('testCheckBtn');
    if (testButton) testButton.disabled = Boolean(isRunning);
    const runtime = document.getElementById('monitorRuntime');
    if (runtime) runtime.hidden = !isRunning;
}

// 开始轮询状态
function startStatusPolling() {
    updateStatus(); // 立即更新一次
    setInterval(updateStatus, 5000); // 每5秒更新一次
}

// 测试邮件
async function testEmail() {
    const receiver = document.getElementById('emailReceiver')?.value;
    if (!receiver) {
        showMessage('请先填写接收邮箱', 'warning');
        return;
    }
    if (!await saveConfig({silent: true})) {
        showMessage('配置尚未保存，请检查保存状态', 'warning');
        return;
    }
    showMessage('正在发送测试邮件...', 'info');
    try {
        const response = await fetch('/api/email/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ email: receiver })
        });
        const result = await response.json();
        
        if (result.success) {
            showMessage(result.message, 'success');
        } else {
            showMessage(result.message || '测试邮件发送失败', 'danger');
        }
    } catch (error) {
        console.error('测试邮件失败:', error);
        showMessage('测试邮件失败', 'danger');
    }
}

function renderJobTagFilters(options) {
    jobTagOptions = options || {sites: [], keywords: []};
    const focused = document.activeElement;
    const focusedKind = focused?.dataset.jobFilterKind;
    const focusedValue = focused?.dataset.jobFilterValue;
    for (const kind of ['sites', 'keywords']) {
        const container = document.getElementById(kind === 'sites' ? 'siteTagFilters' : 'keywordTagFilters');
        const optionContainer = document.getElementById(kind === 'sites' ? 'siteTagOptions' : 'keywordTagOptions');
        if (!container) continue;
        container.replaceChildren();
        optionContainer.replaceChildren();
        const available = jobTagOptions[kind] || [];
        const selected = available.filter(value => !excludedJobTags[kind].has(value));
        for (const value of selected) {
            const chip = document.createElement('span');
            chip.className = `job-filter-chip ${kind === 'sites' ? 'company-chip' : 'keyword-chip'}`;
            if (kind === 'sites') updateSiteLabelAppearance(chip, value);
            const label = document.createElement('span');
            label.textContent = value || '-';
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.textContent = '×';
            remove.setAttribute('aria-label', `排除${kind === 'sites' ? '招聘网址' : '关键词'}：${value || '未记录关键词'}`);
            remove.addEventListener('click', () => {
                excludedJobTags[kind].add(value);
                renderJobTagFilters(jobTagOptions);
                loadJobs(1);
            });
            chip.append(label, remove);
            container.append(chip);
        }
        if (!selected.length) {
            const empty = document.createElement('span');
            empty.className = 'job-filter-empty';
            empty.textContent = available.length ? '未选择任何标签' : '暂无标签';
            container.append(empty);
        }
        for (const value of available) {
            const option = document.createElement('label');
            option.className = 'job-filter-option';
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.checked = !excludedJobTags[kind].has(value);
            checkbox.dataset.jobFilterKind = kind;
            checkbox.dataset.jobFilterValue = value;
            checkbox.addEventListener('change', () => {
                if (checkbox.checked) excludedJobTags[kind].delete(value);
                else excludedJobTags[kind].add(value);
                renderJobTagFilters(jobTagOptions);
                loadJobs(1);
            });
            const label = document.createElement('span');
            label.textContent = value || '-';
            option.append(checkbox, label);
            optionContainer.append(option);
            if (focusedKind === kind && focusedValue === value) checkbox.focus();
        }
        if (!available.length) {
            const empty = document.createElement('span');
            empty.className = 'job-filter-option-empty';
            empty.textContent = '暂无可选项';
            optionContainer.append(empty);
        }
    }
}

function setJobTagSelection(kind, selected) {
    excludedJobTags[kind] = new Set(selected ? [] : jobTagOptions[kind]);
    renderJobTagFilters(jobTagOptions);
    loadJobs(1);
}

function toggleJobTagMenu(kind) {
    const target = document.querySelector(`.job-filter-select[data-kind="${kind}"]`);
    const menu = target.querySelector('.job-filter-menu');
    const opening = menu.hidden;
    closeJobTagMenus();
    menu.hidden = !opening;
    target.querySelector('.job-filter-toggle').setAttribute('aria-expanded', String(opening));
}

function closeJobTagMenus() {
    document.querySelectorAll('.job-filter-select').forEach(select => {
        select.querySelector('.job-filter-menu').hidden = true;
        select.querySelector('.job-filter-toggle').setAttribute('aria-expanded', 'false');
    });
}

document.addEventListener('click', event => {
    if (!event.target.closest('.job-filter-select')) closeJobTagMenus();
});
document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const menu = document.querySelector('.job-filter-menu:not([hidden])');
    if (!menu) return;
    const toggle = menu.closest('.job-filter-select').querySelector('.job-filter-toggle');
    closeJobTagMenus();
    toggle.focus();
});

async function loadJobs(page = 1) {
    if (jobsLoadController) {
        jobsLoadController.abort();
    }
    const controller = new AbortController();
    jobsLoadController = controller;

    try {
        currentPage = page;
        const keyword = document.getElementById('searchInput')?.value || '';

        const params = new URLSearchParams({
            page: String(page),
            keyword
        });
        for (const value of excludedJobTags.sites) params.append('exclude_site', value);
        for (const value of excludedJobTags.keywords) params.append('exclude_keyword', value);
        const response = await fetch(`/api/jobs?${params.toString()}`, {
            signal: controller.signal
        });
        const result = await response.json();

        if (result.success) {
            renderJobTagFilters(result.filters);
            currentPage = result.pagination?.page || page;
            renderJobs(result.data, result.pagination || {});
        } else {
            showMessage(result.message || '加载职位失败', 'danger');
        }
    } catch (error) {
        if (error.name === 'AbortError') return;
        console.error('加载职位失败:', error);
        showMessage('加载职位失败', 'danger');
    } finally {
        if (jobsLoadController === controller) {
            jobsLoadController = null;
        }
    }
}

function jobDateValue(job) {
    const publishDate = String(job.publish_time || '').trim();
    const publishTimestamp = Date.parse(publishDate.replace(' ', 'T'));
    if (publishDate && publishDate !== '未知' && !Number.isNaN(publishTimestamp)) {
        return publishTimestamp;
    }

    const foundDate = String(job.found_time || '').trim();
    const foundTimestamp = Date.parse(foundDate.replace(' ', 'T'));
    return Number.isNaN(foundTimestamp) ? 0 : foundTimestamp;
}

function displayJobDate(job) {
    const rawDate = String(job.publish_time || '').trim();
    if (!rawDate || rawDate === '未知') return '未知';
    const datePart = rawDate.match(/\d{4}-\d{1,2}-\d{1,2}/);
    return datePart ? datePart[0] : rawDate;
}

function displayJobTitle(job) {
    const originalTitle = String(job.title || '未知职位').trim();
    let title = originalTitle
        .replace(/\s*职位\s*ID\s*[:：].*$/i, '')
        .trim();

    const cities = (
        '北京|上海|深圳|广州|杭州|成都|南京|武汉|西安|苏州|' +
        '重庆|天津|厦门|珠海|长沙|合肥|郑州|青岛|济南|东莞|' +
        '佛山|无锡|宁波|全国|远程'
    );
    const cityEmploymentSuffix = new RegExp(
        `(?:${cities})(?:[、,/／](?:${cities}))*\\s*(?:正式|实习|兼职).*$`
    );
    title = title.replace(cityEmploymentSuffix, '').trim();

    // 兼容没有明确城市文本的招聘卡片。
    title = title
        .replace(/\s*(?:正式|实习|兼职)(?:职能|运营|产品|研发|销售|设计|市场|技术|支持).*$/, '')
        .replace(/[\s,，、\-–—]+$/, '')
        .trim();

    return title || originalTitle;
}

function safeHttpUrl(value) {
    try {
        const parsed = new URL(String(value || ''));
        return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '#';
    } catch (_) {
        return '#';
    }
}

function siteLabelStyle(label) {
    const color = getSiteLabelColor(label);
    return [
        `--site-label-bg:${color.background}`,
        `--site-label-text:${color.text}`,
        `--site-label-border:${color.border}`
    ].join(';');
}

// 渲染职位列表
function renderJobs(jobs, pageInfo = {}) {
    const container = document.getElementById('jobsList');
    const paginationNav = document.getElementById('paginationNav');
    const pagination = document.getElementById('pagination');
    const resultCount = document.getElementById('jobsResultCount');
    const pageSummary = document.getElementById('jobsPageSummary');

    if (!container || !paginationNav || !pagination) return;

    if (!jobs || jobs.length === 0) {
        if (resultCount) resultCount.textContent = '0 个职位';
        container.innerHTML = `
            <div class="text-center py-5">
                <i class="bi bi-inbox" style="font-size: 64px; color: #ccc;"></i>
                <p class="mt-3 text-muted">暂无符合条件的职位</p>
                <p class="text-muted small">可以调整筛选标签，或检查新的职位。</p>
            </div>
        `;
        paginationNav.style.display = 'none';
        return;
    }

    // 接口已按日期排序；前端再次排序，保证任何来源的数据都保持日期降序。
    const orderedJobs = [...jobs].sort((left, right) => {
        const dateDifference = jobDateValue(right) - jobDateValue(left);
        if (dateDifference !== 0) return dateDifference;
        return Number(right.id || 0) - Number(left.id || 0);
    });

    // 渲染默认折叠的职位卡片
    container.innerHTML = orderedJobs.map((job, index) => {
        const title = escapeHtml(displayJobTitle(job));
        const company = escapeHtml(job.company || '未知公司');
        const city = escapeHtml(job.city || '未知');
        const publishTime = escapeHtml(job.publish_time || '未知');
        const sourceSite = escapeHtml(job.source_site || '未知');
        const siteTag = String(job.recruitment_site_tag || '').trim();
        const keywordTag = String(job.matched_keyword || '').trim();
        const salary = escapeHtml(job.salary || '面议');
        const description = escapeHtml(job.description || '暂无描述');
        const foundTime = escapeHtml(job.found_time || '未知');
        const date = escapeHtml(displayJobDate(job));
        const jobUrl = escapeHtml(safeHttpUrl(job.url));
        const labelStyle = siteLabelStyle(siteTag);
        return `
        <article class="job-item">
            <button type="button" class="job-summary" aria-expanded="false" aria-controls="job-detail-${index}"
                    onclick="toggleJobCard(this)">
                <span class="job-summary-left">
                    <span class="job-card-labels">
                        <span class="job-site-label company-chip" style="${labelStyle}" title="${escapeHtml(siteTag)}">${escapeHtml(siteTag || '-')}</span>
                        ${job.capture_mode === 'fixed' ? '' : `<span class="job-keyword-label" title="${escapeHtml(keywordTag || '无法确认关键词')}">${escapeHtml(keywordTag || '-')}</span>`}
                    </span>
                    <span class="job-summary-copy">
                        <span class="job-company-line">${city}</span>
                        <span class="job-title" title="${title}">${title}</span>
                        <span class="job-summary-tags">
                            <span class="job-summary-salary">${salary}</span>
                            ${isNewJob(job.found_time) ? '<span class="badge-new">今日新增</span>' : ''}
                        </span>
                    </span>
                </span>
                <span class="job-summary-meta"><time class="job-date">${date}</time><span class="job-summary-action">查看详情 <i class="bi bi-arrow-down-right" aria-hidden="true"></i></span></span>
            </button>
            <div class="job-details" id="job-detail-${index}" hidden>
                <div class="job-detail-heading">
                    <div><span class="job-detail-eyebrow">${escapeHtml(siteTag || '招聘岗位')} · 岗位详情</span><h2>${title}</h2></div>
                    <button type="button" class="job-detail-close" onclick="closeJobDetails(true)" aria-label="收起职位详情">×</button>
                </div>
                <div class="job-detail-content">
                    <div class="job-detail-main">
                        <h3>职位描述</h3>
                        <div class="job-description">${description}</div>
                        <a class="job-url" href="${jobUrl}" target="_blank" rel="noopener noreferrer">
                            打开职位页面 <i class="bi bi-arrow-up-right" aria-hidden="true"></i>
                        </a>
                    </div>
                <div class="job-detail-grid">
                    <div class="job-detail-field">
                        <span class="job-detail-field-label">公司</span>
                        <span class="job-detail-field-value">${company}</span>
                    </div>
                    <div class="job-detail-field">
                        <span class="job-detail-field-label">地点</span>
                        <span class="job-detail-field-value">${city}</span>
                    </div>
                    <div class="job-detail-field">
                        <span class="job-detail-field-label">薪资</span>
                        <span class="job-detail-field-value">${salary}</span>
                    </div>
                    <div class="job-detail-field">
                        <span class="job-detail-field-label">来源网站</span>
                        <span class="job-detail-field-value">${sourceSite}</span>
                    </div>
                    <div class="job-detail-field">
                        <span class="job-detail-field-label">发布时间</span>
                        <span class="job-detail-field-value">${publishTime}</span>
                    </div>
                    <div class="job-detail-field">
                        <span class="job-detail-field-label">发现时间</span>
                        <span class="job-detail-field-value">${foundTime}</span>
                    </div>
                    </div>
                </div>
            </div>
        </article>`;
    }).join('');

    if (resultCount) resultCount.textContent = `共 ${pageInfo.total ?? jobs.length} 个职位`;
    if (pageSummary) {
        const page = pageInfo.page || currentPage;
        const perPage = pageInfo.per_page || jobs.length;
        const start = (page - 1) * perPage + 1;
        pageSummary.textContent = `第 ${start}–${start + jobs.length - 1} 条 · 每页 ${perPage} 条`;
    }

    const totalPages = pageInfo.total_pages || 1;
    paginationNav.style.display = totalPages > 1 ? 'flex' : 'none';
    const pageNumbers = new Set([1, totalPages]);
    for (let pageNumber = currentPage - 2; pageNumber <= currentPage + 2; pageNumber++) {
        if (pageNumber >= 1 && pageNumber <= totalPages) pageNumbers.add(pageNumber);
    }
    let previousPageNumber = 0;
    const pageButtons = [...pageNumbers].sort((left, right) => left - right).map(pageNumber => {
        const isCurrent = pageNumber === currentPage;
        const gap = previousPageNumber && pageNumber - previousPageNumber > 1 ?
            '<li class="page-item pagination-gap" aria-hidden="true">…</li>' : '';
        previousPageNumber = pageNumber;
        return `${gap}
            <li class="page-item ${isCurrent ? 'active' : ''}">
                <button type="button" class="page-link"
                        ${isCurrent ? 'aria-current="page"' : ''}
                        onclick="loadJobs(${pageNumber})">
                    ${pageNumber}
                </button>
            </li>
        `;
    }).join('');

    pagination.innerHTML = `
        <li class="page-item ${currentPage === 1 ? 'disabled' : ''}">
            <button type="button" class="page-link" aria-label="上一页"
                    ${currentPage === 1 ? 'disabled' : ''}
                    onclick="loadJobs(${currentPage - 1})">
                ‹
            </button>
        </li>
        ${pageButtons}
        <li class="page-item ${currentPage >= totalPages ? 'disabled' : ''}">
            <button type="button" class="page-link" aria-label="下一页"
                    ${currentPage >= totalPages ? 'disabled' : ''}
                    onclick="loadJobs(${currentPage + 1})">
                ›
            </button>
        </li>
    `;
}

function closeJobDetails(restoreFocus = false) {
    const container = document.getElementById('jobsList');
    const card = container?.querySelector('.job-item.expanded');
    const panel = container?.querySelector('.job-expanded-panel');
    if (!card || !panel) return;
    const summary = card.querySelector('.job-summary');
    const details = panel.querySelector('.job-details');
    details.hidden = true;
    card.append(details);
    panel.remove();
    card.classList.remove('expanded');
    summary.setAttribute('aria-expanded', 'false');
    if (restoreFocus) summary.focus();
}

function positionJobDetails() {
    const container = document.getElementById('jobsList');
    const card = container?.querySelector('.job-item.expanded');
    const panel = container?.querySelector('.job-expanded-panel');
    if (!card || !panel) return;
    panel.hidden = true;
    const cardTop = card.getBoundingClientRect().top;
    const row = [...container.querySelectorAll('.job-item')]
        .filter(item => Math.abs(item.getBoundingClientRect().top - cardTop) < 2);
    row.at(-1).after(panel);
    panel.hidden = false;
}

function toggleJobCard(summaryButton) {
    const card = summaryButton.closest('.job-item');
    const details = card?.querySelector('.job-details');
    const willExpand = summaryButton.getAttribute('aria-expanded') !== 'true';
    closeJobDetails();
    if (!willExpand || !card || !details) return;
    const panel = document.createElement('section');
    panel.className = 'job-expanded-panel';
    panel.setAttribute('aria-label', '职位详情');
    panel.append(details);
    card.after(panel);
    details.hidden = false;
    card.classList.add('expanded');
    summaryButton.setAttribute('aria-expanded', 'true');
    positionJobDetails();
}

window.addEventListener('resize', positionJobDetails);

// 判断是否为新职位
function isNewJob(foundTime) {
    if (!foundTime) return false;
    const jobTime = new Date(String(foundTime).replace(' ', 'T'));
    const now = new Date();
    const diff = (now - jobTime) / (1000 * 60 * 60); // 小时
    return diff < 24;
}


// 搜索职位
function searchJobs() {
    clearTimeout(jobSearchTimer);
    jobSearchTimer = setTimeout(() => loadJobs(1), 250);
}

// 清除职位记录
async function clearJobs() {
    if (confirm('确定要清除所有职位记录吗？此操作不可恢复。')) {
        try {
            const response = await fetch('/api/jobs/clear?all=true', {
                method: 'POST'
            });
            const result = await response.json();

            if (result.success) {
                showMessage(result.message, 'success');
                loadJobs(); // 刷新列表
                updateStatus(); // 刷新统计数据
            } else {
                showMessage(result.message || '清除失败', 'danger');
            }
        } catch (error) {
            console.error('清除职位失败:', error);
            showMessage('清除职位失败', 'danger');
        }
    }
}

// 显示消息
function showMessage(message, type = 'info') {
    const container = document.getElementById('messageContainer');
    if (!container) return;
    
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show position-fixed`;
    alertDiv.style.cssText = 'top: 20px; right: 20px; z-index: 9999; max-width: 400px;';
    const text = document.createElement('span');
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.setAttribute('data-bs-dismiss', 'alert');
    alertDiv.append(text, close);
    
    container.appendChild(alertDiv);
    
    // 3秒后自动消失
    setTimeout(() => {
        alertDiv.remove();
    }, 3000);
}
