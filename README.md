# 招聘监测系统

这是一个使用 Flask、Playwright 和 SQLite 的招聘职位监测器。程序可以监测多个招聘网址，按规则或 Gemini 筛选职位、去重保存，并在发现新职位时汇总所有相关网址标签发送一封 SMTP 邮件通知。

## 本地安装

需要 Python 3.10 或更高版本：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Windows 推荐双击 `启动Web服务.bat`，然后访问：

```text
http://127.0.0.1:5000
```

也可以直接运行：

```powershell
python app.py
```

首次启动时，如果没有 `config.yaml`，程序会自动从不含个人信息的 `config.example.yaml` 创建一份。之后在网页中填写并保存配置即可。已经存在的本地配置不会被覆盖。

第二套独立配置可以双击 `启动Web服务-用户2(分身).bat`，访问 `http://127.0.0.1:5001`。它使用独立的 `config_user2.yaml`、`jobs_user2.db`、缓存和日志。

## 网页功能

- 管理多个目标招聘网址，并从网址编码中自动生成可编辑标签
- 标签前两个字相同时使用相同的填充颜色
- 点击开始监测前自动保存页面配置，逐个运行所有网址
- 单个网址失败时继续运行其余网址
- 职位卡片默认折叠，按发布日期降序排列
- 按网址标签关键词筛选职位，完整显示全部分页页码
- 测试 SMTP、运行一次无持久化检查和可选 Gemini 筛选

## 本地配置与数据

真实配置和运行数据只保存在本地，并已被 `.gitignore` 排除：

- `config.yaml`、`config_user*.yaml`
- `jobs*.db`
- `jobs_cache*.json`
- `app*.log`
- `.job-monitor-config*.b64`

仓库中只提交安全模板 `config.example.yaml`。配置文件采用 YAML 格式，网页保存时先写临时文件再原子替换，避免写入一半造成文件损坏。

主要配置字段可以参考 [config.example.yaml](config.example.yaml)。示例：

```yaml
job_sites:
  - https://jobs.bytedance.com/experienced/position/list?keywords=%E6%B3%95%E5%8A%A1
job_site_labels:
  'https://jobs.bytedance.com/experienced/position/list?keywords=%E6%B3%95%E5%8A%A1': 法务
job_keywords:
  - 法务
cities: []
exclude_keywords:
  - 实习
check_interval: 4

email:
  sender: your_email@example.com
  auth_code: ''
  receiver: your_email@example.com
  smtp_server: smtp.example.com
  smtp_port: 465

gemini_api_key: ''
gemini_model: gemini-3.5-flash-lite
ai_filter_prompt: ''
```

邮箱 465 端口使用 SSL，其他端口使用 STARTTLS。只有 `gemini_api_key` 和 `ai_filter_prompt` 都填写时才调用 Gemini。

## GitHub 自动运行

工作流位于 `.github/workflows/scraper_cron.yml`，默认每天 UTC 00:00，即新加坡/北京时间 08:00 运行，也支持在 Actions 页面手动运行。

### 推荐：网页一键同步

在 GitHub 创建一个只允许访问目标仓库的 Fine-grained Personal Access Token，并授予：

- Actions：只读，用于下载最新职位数据包
- Secrets：读写，用于更新 `JOB_MONITOR_CONFIG_B64`

重启本地 Web 服务后，在网页右侧的“GitHub 同步”中填写仓库地址和 Token，点击“保存连接”。之后可以：

- 点击“一键上传配置”：先保存当前网页配置，再将 `config.yaml` 和全部 `config_user*.yaml` 加密上传到 GitHub Secret。邮箱授权码和 Gemini Key 也包含在这个加密 Secret 中。
- 点击“一键下载职位数据”：下载最近一次成功 Actions 运行生成的数据包，校验后恢复到本地。覆盖前会在 `backups/github-sync-时间/` 备份现有数据库和缓存。

下载前必须停止本地监测。Token 只保存在已被 Git 忽略的本地 `github_sync.yaml` 中，接口只向浏览器返回掩码。该文件仍是明文敏感文件，只应保存在可信设备。

GitHub Actions 至少需要成功运行一次，才能一键下载职位数据。职位数据包保留 30 天，每次成功运行都会生成新的数据包。

### 手动生成私有配置 Secret

先在本地网页中完成所有配置，然后运行：

```powershell
python github_config.py export
```

程序会把 `config.yaml` 和所有 `config_user*.yaml` 打包到：

```text
.job-monitor-config.b64
```

默认不会把邮箱授权码和 Gemini API Key 放进这个文件。

进入 GitHub 仓库：

```text
Settings → Secrets and variables → Actions → New repository secret
```

新建以下 Secret：

| Secret 名称 | 是否必需 | 内容 |
| --- | --- | --- |
| `JOB_MONITOR_CONFIG_B64` | 必需 | `.job-monitor-config.b64` 的完整单行内容 |
| `JOB_EMAIL_AUTH_CODE` | 使用邮件时必需 | SMTP 邮箱授权码 |
| `GEMINI_API_KEY` | 使用 Gemini 时必需 | Gemini API Key |

如果不同分身必须使用不同的邮箱授权码或 API Key，可以运行：

```powershell
python github_config.py export --include-sensitive-values
```

此时敏感值也会被放入 `JOB_MONITOR_CONFIG_B64`。Base64 只是编码而不是加密，所以生成文件仍应视为敏感文件；它已被 Git 忽略，设置 Secret 后可以删除。若配置中的敏感字段为空，独立的邮箱或 Gemini Secret 会作为云端运行的后备值。

### 启用工作流

打开仓库的 Actions 页面，启用 `Daily Job Monitor`。Fork 不会继承上游仓库的 Secrets，Fork 用户必须设置自己的 Secrets 后才能运行。

每次运行时，工作流会：

1. 在临时运行器中从 `JOB_MONITOR_CONFIG_B64` 恢复配置。
2. 从该仓库自己的 Actions 缓存恢复数据库和去重缓存。
3. 逐个运行默认配置和所有分身配置。
4. 将更新后的数据库和去重缓存保存回 Actions 缓存。
5. 上传一个可供本地“一键下载”的 `job-monitor-state` 数据包。

工作流只有仓库内容读取权限，不会提交或推送配置、数据库、缓存或日志。Fork 的 Secrets 和 Actions 缓存均与上游仓库分离。

Actions 缓存用于轻量级运行状态，不是永久数据库；每天运行会持续刷新缓存。如果仓库长期停用导致缓存被 GitHub 清理，下一次运行会重新建立去重状态。

公开仓库的 Actions 数据包不应被当作私密存储。如果职位明细或监测方向需要保密，请使用私有 GitHub 仓库。

## 首次上传 GitHub 前检查

当前目录中的真实配置可能包含邮箱地址、授权码和 API Key。初始化 Git 仓库后，先确认这些文件确实被忽略：

```powershell
git check-ignore config.yaml config_user2.yaml jobs.db jobs_cache.json app.log github_sync.yaml backups
git status --short
```

不要使用 `git add -f` 强制添加这些文件。如果敏感配置曾经被提交，即使后来删除也仍可能存在于 Git 历史中，需要清理历史并立即更换相关密钥。

## 命令行

```powershell
# 测试抓取与筛选，不发邮件、不写数据库
python main.py --test --once

# 正式检查一次
python main.py --once

# 持续监测
python main.py

# 使用指定分身
python main.py --config config_user2.yaml --once

# 所有本地分身各检查一次
python run_all_profiles.py --once
```

## 验证与排错

```powershell
python -m py_compile app.py main.py database.py scraper.py matcher.py notifier.py config_bootstrap.py github_config.py github_sync.py
python -m unittest discover -v
```

如果抓取时报浏览器不存在，再运行：

```powershell
python -m playwright install chromium
```

招聘网站可能调整页面结构或启用验证码。遇到验证码时应停止自动抓取，并遵守网站条款与访问频率限制。
