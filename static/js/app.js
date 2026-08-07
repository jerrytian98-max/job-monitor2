// 招聘监测系统前端JavaScript

// 全局变量
let config = {};
let tags = {
    keywords: [],
    cities: [],
    exclude: []
};
let sites = [];
let currentPage = 1;
let jobModal;
let eventSource;
let jobsFoundCount = 0;
let jobsAddedCount = 0;
let jobsLoadController;

function escapeHtml(value) {
    const element = document.createElement('div');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    loadConfig();
    loadGithubSyncSettings();
    loadJobs();
    startStatusPolling();
    startEventStream();  // 启动实时事件流

    // 初始化模态框
    jobModal = new bootstrap.Modal(document.getElementById('jobModal'));
    
    // 绑定表单提交事件（防止页面刷新）
    const configForm = document.getElementById('configForm');
    if (configForm) {
        configForm.addEventListener('submit', function(e) {
            e.preventDefault();
            saveConfig();
        });
    }
    
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
                    handleJobFoundEvent(data);
                    break;
                case 'heartbeat':
                    // 心跳，忽略
                    break;
            }
        } catch (e) {
            console.error('解析事件失败:', e);
        }
    };

    eventSource.onerror = function() {
        console.error('EventSource连接断开，5秒后重连...');
        eventSource.close();
        setTimeout(startEventStream, 5000);
    };
}

// 处理状态事件
function handleStatusEvent(event) {
    const message = event.data.message;
    const type = event.data.type;

    // 更新当前任务
    const currentTask = document.getElementById('currentTask');
    if (currentTask) currentTask.textContent = message;

    // 工作日志只保留每个网址的开始和完成记录。
    if (event.data.work_log) {
        addLog(message, type);
    }

    // 更新状态徽章
    const badge = document.getElementById('liveStatusBadge');
    if (badge) {
        if (type === 'error') {
            badge.className = 'badge bg-danger ms-2';
        } else if (type === 'success') {
            badge.className = 'badge bg-success ms-2';
        } else {
            badge.className = 'badge bg-primary ms-2';
        }
    }
}

// 处理职位发现事件
function handleJobFoundEvent(event) {
    const job = event.data;

    // 更新统计
    jobsFoundCount++;
    const jobsFoundEl = document.getElementById('jobsFound');
    if (jobsFoundEl) jobsFoundEl.textContent = jobsFoundCount;

    // 后端在推送事件前已经完成筛选和入库，无需再次抓取职位 URL。
    jobsAddedCount++;
    const jobsAddedEl = document.getElementById('jobsAdded');
    if (jobsAddedEl) jobsAddedEl.textContent = jobsAddedCount;
    loadJobs(1);
    updateStatus();

}

// 添加工作日志
function addLog(message, type) {
    const logContainer = document.getElementById('workLog');
    if (!logContainer) return;

    // 如果是初始状态，清空
    if (logContainer.querySelector('.text-muted') &&
        logContainer.textContent.trim() === '等待启动监测...') {
        logContainer.innerHTML = '';
    }

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
            
            // 填充 Gemini API Key
            const geminiApiKey = document.getElementById('geminiApiKey');
            if (geminiApiKey) geminiApiKey.value = config.gemini_api_key || '';
            const aiFilterPrompt = document.getElementById('aiFilterPrompt');
            if (aiFilterPrompt) aiFilterPrompt.value = config.ai_filter_prompt || '';
            const geminiModel = document.getElementById('geminiModel');
            if (geminiModel) geminiModel.value = config.gemini_model || 'gemini-3.5-flash-lite';
            
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
            const checkInterval = document.getElementById('checkInterval');
            if (checkInterval) checkInterval.value = config.check_interval || 2;
            
            showMessage('配置加载成功', 'success');
        }
    } catch (error) {
        console.error('加载配置失败:', error);
        showMessage('加载配置失败', 'danger');
    }
}

// 保存配置
async function saveConfig(options = {}) {
    const silent = options.silent === true;
    const reload = options.reload !== false;

    try {
        // 收集表单数据
        const configData = {
            job_keywords: tags.keywords,
            cities: tags.cities,
            exclude_keywords: tags.exclude,
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
            gemini_api_key: document.getElementById('geminiApiKey')?.value || '',
            ai_filter_prompt: document.getElementById('aiFilterPrompt')?.value || '',
            gemini_model: document.getElementById('geminiModel')?.value || 'gemini-3.5-flash-lite',
            email: {
                sender: document.getElementById('emailSender')?.value || '',
                auth_code: document.getElementById('emailAuthCode')?.value || '',
                receiver: document.getElementById('emailReceiver')?.value || '',
                smtp_server: document.getElementById('smtpServer')?.value || 'smtp.gmail.com',
                smtp_port: parseInt(document.getElementById('smtpPort')?.value) || 587
            },
            check_interval: parseFloat(document.getElementById('checkInterval')?.value) || 2
        };

        const response = await fetch('/api/config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(configData)
        });
        
        const result = await response.json();
        
        if (result.success) {
            if (!silent) {
                showMessage('配置保存成功', 'success');
            }
            if (reload) {
                // 重新加载配置以确认保存成功
                setTimeout(loadConfig, 1000);
            }
            return true;
        } else {
            showMessage(result.message || '保存失败', 'danger');
            return false;
        }
    } catch (error) {
        console.error('保存配置失败:', error);
        showMessage('保存配置失败', 'danger');
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

async function saveGithubSyncSettings(options = {}) {
    const silent = options.silent === true;
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
        if (tokenInput) tokenInput.value = result.data.token || '';
        setGithubSyncStatus(`已连接 ${result.data.repository}。`, 'success');
        if (!silent) showMessage(result.message, 'success');
        return true;
    } catch (error) {
        console.warn('保存 GitHub 同步设置失败:', error);
        setGithubSyncStatus(error.message || '保存 GitHub 连接失败', 'danger');
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
    }
}

// 删除标签
function removeTag(type, text) {
    const index = tags[type].indexOf(text);
    if (index > -1) {
        tags[type].splice(index, 1);
        renderTags(type, tags[type]);
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
            'careers.pddglobalhr.com': '拼多多'
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
    const length = Array.from(String(label || '').trim()).length;
    const width = Math.max(72, Math.min(180, 38 + Math.max(length, 2) * 15));

    input.style.setProperty('--site-label-bg', color.background);
    input.style.setProperty('--site-label-text', color.text);
    input.style.setProperty('--site-label-border', color.border);
    input.style.width = `${width}px`;
}

// 渲染可编辑的网址与标签列表。
function renderSites(siteList, siteLabelMap = null, siteModeMap = null) {
    sites = normalizeSites(siteList, siteLabelMap, siteModeMap);
    const container = document.getElementById('sitesList');
    if (!container) return;

    container.innerHTML = '';
    
    sites.forEach((site, index) => {
        const siteDiv = document.createElement('div');
        siteDiv.className = 'site-url';

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
        });

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
        duplicate.title = '复制到下一行';
        const duplicateIcon = document.createElement('i');
        duplicateIcon.className = 'bi bi-plus-circle';
        duplicate.appendChild(duplicateIcon);
        duplicate.addEventListener('click', () => duplicateSite(index));

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-link delete-btn';
        remove.setAttribute('aria-label', `删除网址 ${index + 1}`);
        const removeIcon = document.createElement('i');
        removeIcon.className = 'bi bi-x-circle';
        remove.appendChild(removeIcon);
        remove.addEventListener('click', () => removeSite(index));

        actions.append(duplicate, remove);
        siteDiv.append(labelInput, urlInput, modeSelect, actions);
        container.appendChild(siteDiv);
    });
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
        showMessage(
            `已添加${mode === 'fixed' ? '固定直抓' : '关键词搜索'}网址，自动标签：${label}`,
            'success'
        );
    }
}

// 删除网站
function removeSite(index) {
    sites.splice(index, 1);
    renderSites(sites);
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
    renderSites(sites);

    // 优先选中搜索关键词参数，便于直接替换；没有参数时选中整个 URL。
    requestAnimationFrame(() => {
        const editors = document.querySelectorAll('.site-url-input');
        const editor = editors[index + 1];
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
async function startMonitor() {
    try {
        // “开始监测”始终先保存当前页面，避免刚添加的网址尚未写入配置。
        const saved = await saveConfig({silent: true, reload: false});
        if (!saved) return;

        const workLog = document.getElementById('workLog');
        if (workLog) workLog.innerHTML = '';

        const response = await fetch('/api/monitor/start', {
            method: 'POST'
        });
        const result = await response.json();
        
        if (result.success) {
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
async function stopMonitor() {
    try {
        const response = await fetch('/api/monitor/stop', {
            method: 'POST'
        });
        const result = await response.json();
        
        if (result.success) {
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
    showMessage('正在执行测试检查...', 'info');
    
    try {
        const response = await fetch('/api/test/check', {
            method: 'POST'
        });
        const result = await response.json();
        
        if (result.success) {
            const count = result.data?.matched_jobs || 0;
            showMessage(`测试检查完成，找到 ${count} 个符合条件的职位`, 'success');
        } else {
            showMessage(result.message || '测试失败', 'danger');
        }
    } catch (error) {
        console.error('测试检查失败:', error);
        showMessage('测试检查失败', 'danger');
    }
}

// 更新状态
async function updateStatus() {
    try {
        const response = await fetch('/api/monitor/status');
        const result = await response.json();
        
        if (result.success) {
            const status = result.data;
            const isRunning = status.is_monitoring;
            
            // 更新状态显示
            const statusDiv = document.getElementById('monitorStatus');
            const statusText = document.getElementById('statusText');
            const statusDetail = document.getElementById('statusDetail');

            if (statusDiv && statusText && statusDetail) {
                if (status.status === 'stopping') {
                    statusDiv.className = 'monitor-status running';
                    statusText.textContent = '监测正在停止';
                    statusDetail.textContent = '当前页面抓取完成后将安全退出';
                } else if (isRunning) {
                    statusDiv.className = 'monitor-status running';
                    statusText.textContent = '监测运行中';
                    statusDetail.textContent = '系统正在自动监测招聘网站';
                } else {
                    statusDiv.className = 'monitor-status stopped';
                    statusText.textContent = '监测已停止';
                    statusDetail.textContent = '点击启动按钮开始监测';
                }
            }
            
            // 更新统计数据
            const totalJobs = document.getElementById('totalJobs');
            if (totalJobs) totalJobs.textContent = status.total_jobs_found || 0;

            const newJobsToday = document.getElementById('newJobsToday');
            if (newJobsToday) newJobsToday.textContent = status.new_jobs_today || 0;
            
            // 更新时间
            const lastCheck = document.getElementById('lastCheck');
            if (lastCheck) {
                if (status.last_check) {
                    const lastCheckDate = new Date(status.last_check);
                    const now = new Date();
                    const diff = Math.floor((now - lastCheckDate) / 1000 / 60); // 分钟
                    lastCheck.textContent = diff < 60 ? `${diff}分钟前` : lastCheckDate.toLocaleTimeString();
                } else {
                    lastCheck.textContent = '--';
                }
            }
            
            const runningTime = document.getElementById('runningTime');
            if (runningTime) runningTime.textContent = status.elapsed_time || '--';
            
            // 更新按钮状态
            updateButtons(isRunning);
        }
    } catch (error) {
        console.error('获取状态失败:', error);
    }
}

// 更新按钮状态
function updateButtons(isRunning) {
    const startBtn = document.getElementById('startBtn');
    const stopBtn = document.getElementById('stopBtn');
    
    if (startBtn && stopBtn) {
        if (isRunning) {
            startBtn.disabled = true;
            stopBtn.disabled = false;
        } else {
            startBtn.disabled = false;
            stopBtn.disabled = true;
        }
    }

    const badge = document.getElementById('liveStatusBadge');
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    if (badge) {
        badge.className = `badge ${isRunning ? 'bg-success' : 'bg-secondary'} ms-2`;
        badge.textContent = isRunning ? '运行中' : '待机';
    }
    if (progressBar && progressText) {
        progressBar.style.width = isRunning ? '100%' : '0%';
        progressText.textContent = isRunning ? '监测中' : '0%';
        progressBar.classList.toggle('progress-bar-animated', isRunning);
    }
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
        showMessage('请先在配置中填写接收邮箱并保存', 'warning');
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

// 加载职位列表
async function loadJobs(page = 1) {
    if (jobsLoadController) {
        jobsLoadController.abort();
    }
    const controller = new AbortController();
    jobsLoadController = controller;

    try {
        currentPage = page;
        const keyword = document.getElementById('searchInput')?.value || '';
        const siteLabel = document.getElementById('siteLabelFilter')?.value || '';

        const params = new URLSearchParams({
            page: String(page),
            keyword,
            site_label: siteLabel
        });
        const response = await fetch(`/api/jobs?${params.toString()}`, {
            signal: controller.signal
        });
        const result = await response.json();

        if (result.success) {
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

    if (!container || !paginationNav || !pagination) return;

    if (!jobs || jobs.length === 0) {
        container.innerHTML = `
            <div class="text-center py-5">
                <i class="bi bi-inbox" style="font-size: 64px; color: #ccc;"></i>
                <p class="mt-3 text-muted">暂无职位信息</p>
                <p class="text-muted small">启动监测后，发现的职位将显示在这里</p>
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
    container.innerHTML = orderedJobs.map(job => {
        const title = escapeHtml(displayJobTitle(job));
        const company = escapeHtml(job.company || '未知公司');
        const city = escapeHtml(job.city || '未知');
        const publishTime = escapeHtml(job.publish_time || '未知');
        const sourceSite = escapeHtml(job.source_site || '未知');
        const rawSiteLabel = String(job.site_label || job.source_site || '未知网址').trim();
        const siteLabel = escapeHtml(rawSiteLabel);
        const salary = escapeHtml(job.salary || '面议');
        const description = escapeHtml(job.description || '暂无描述');
        const foundTime = escapeHtml(job.found_time || '未知');
        const date = escapeHtml(displayJobDate(job));
        const jobUrl = escapeHtml(safeHttpUrl(job.url));
        const labelStyle = siteLabelStyle(rawSiteLabel);
        return `
        <article class="job-item">
            <button type="button" class="job-summary" aria-expanded="false"
                    onclick="toggleJobCard(this)">
                <span class="job-summary-left">
                    <span class="job-site-label" style="${labelStyle}" title="${siteLabel}">
                        ${siteLabel}
                    </span>
                    <span class="job-title" title="${title}">
                        ${title}
                        ${isNewJob(job.found_time) ? '<span class="badge-new">新</span>' : ''}
                    </span>
                </span>
                <time class="job-date">${date}</time>
            </button>
            <div class="job-details" hidden>
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
                <div class="job-description">${description}</div>
                <a class="job-url" href="${jobUrl}" target="_blank" rel="noopener noreferrer">
                    <i class="bi bi-box-arrow-up-right"></i> 打开职位页面
                </a>
            </div>
        </article>`;
    }).join('');

    const totalPages = pageInfo.total_pages || 1;
    paginationNav.style.display = totalPages > 1 ? 'block' : 'none';
    const pageButtons = Array.from({length: totalPages}, (_, index) => {
        const pageNumber = index + 1;
        const isCurrent = pageNumber === currentPage;
        return `
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
                <i class="bi bi-chevron-left"></i>
            </button>
        </li>
        ${pageButtons}
        <li class="page-item ${currentPage >= totalPages ? 'disabled' : ''}">
            <button type="button" class="page-link" aria-label="下一页"
                    ${currentPage >= totalPages ? 'disabled' : ''}
                    onclick="loadJobs(${currentPage + 1})">
                <i class="bi bi-chevron-right"></i>
            </button>
        </li>
    `;
}

function toggleJobCard(summaryButton) {
    const card = summaryButton.closest('.job-item');
    const details = card?.querySelector('.job-details');
    if (!card || !details) return;

    const willExpand = summaryButton.getAttribute('aria-expanded') !== 'true';
    summaryButton.setAttribute('aria-expanded', String(willExpand));
    details.hidden = !willExpand;
    card.classList.toggle('expanded', willExpand);
}

// 判断是否为新职位
function isNewJob(foundTime) {
    if (!foundTime) return false;
    const jobTime = new Date(String(foundTime).replace(' ', 'T'));
    const now = new Date();
    const diff = (now - jobTime) / (1000 * 60 * 60); // 小时
    return diff < 24;
}

// 显示职位详情
function showJobDetail(jobId) {
    fetch(`/api/jobs/${jobId}`).then(response => response.json()).then(result => {
        if (result.success) {
            const job = result.data;
            if (job) {
                document.getElementById('jobModalTitle').textContent = job.title;
                document.getElementById('jobModalBody').innerHTML = `
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-building"></i> 公司名称
                        </div>
                        <div>${escapeHtml(job.company || '未知公司')}</div>
                    </div>
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-currency-yen"></i> 薪资待遇
                        </div>
                        <div class="text-success fw-bold">${escapeHtml(job.salary || '面议')}</div>
                    </div>
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-geo-alt"></i> 工作地点
                        </div>
                        <div>${escapeHtml(job.city || '未知')}</div>
                    </div>
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-globe"></i> 信息来源
                        </div>
                        <div>${escapeHtml(job.source_site || '未知')}</div>
                    </div>
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-clock"></i> 发布时间
                        </div>
                        <div>${escapeHtml(job.publish_time || '未知')}</div>
                    </div>
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-file-text"></i> 职位描述
                        </div>
                        <div style="line-height: 1.8;">${escapeHtml(job.description || '暂无描述')}</div>
                    </div>
                    <div class="job-detail-section">
                        <div class="job-detail-label">
                            <i class="bi bi-clock-history"></i> 发现时间
                        </div>
                        <div>${escapeHtml(job.found_time || '未知')}</div>
                    </div>
                `;
                const link = document.getElementById('jobModalLink');
                try {
                    const parsed = new URL(job.url);
                    link.href = ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '#';
                } catch (_) {
                    link.href = '#';
                }
                jobModal.show();
            }
        }
    }).catch(error => {
        console.error('获取职位详情失败:', error);
        showMessage('获取职位详情失败', 'danger');
    });
}

// 搜索职位
function searchJobs() {
    loadJobs(1);
}

function filterJobsBySiteLabel() {
    loadJobs(1);
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
