# 招聘监测系统

这是一个使用 Flask、Playwright 和 SQLite 的招聘职位监测器。程序可以监测多个招聘网址，按规则或 Gemini 筛选职位、去重保存，并在发现新职位时发送一封汇总 SMTP 邮件通知。

## 从 GitHub 下载并在 Windows 上运行

GitHub 的 `Code → Download ZIP` 下载的是**源码**，不是已经封装好的 Windows 软件。因此第一次在新电脑使用时，需要先安装 Python 和浏览器抓取组件；完成一次安装后即可双击启动脚本。

### 第一次安装

1. 在 GitHub 页面点击 `Code → Download ZIP`，解压到任意本地目录（例如 `D:\job-monitor`）。
2. 安装 [Python 3.10 或更高版本](https://www.python.org/downloads/windows/)。安装界面请勾选 **Add python.exe to PATH**。
3. 在解压后的项目文件夹空白处按住 `Shift` 并右键，选择“在此处打开 PowerShell”。运行：

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   ```

   最后一条命令会下载 Playwright 专用 Chromium；招聘网站抓取依赖它，即使电脑中已安装 Chrome 也不能省略。

4. 双击 `启动Web服务-用户1(默认).bat`（或兼容脚本 `启动Web服务.bat`）。看到访问地址后，在浏览器打开：

   ```text
   http://127.0.0.1:5000
   ```

5. 第一次打开网页后，在“目标招聘网址”中添加网址、选择“关键词搜索”或“固定直抓”，填写关键词；如需邮件通知，再填写 SMTP 邮箱配置并点击“测试邮件发送”。

首次启动时，如果没有 `config.yaml`，程序会自动从不含个人信息的 `config.example.yaml` 创建一份。之后在网页中填写并保存配置即可；已有的本地配置不会被覆盖。

### 日常启动与第二套独立配置

依赖安装完成后，只需双击 `启动Web服务-用户1(默认).bat`，无需重复安装。也可以在项目目录运行：

```powershell
python app.py
```

第二套独立配置可以双击 `启动Web服务-用户2(分身).bat`，访问 `http://127.0.0.1:5001`。它使用独立的 `config_user2.yaml`、`jobs_user2.db`、缓存和日志。

### 常见启动问题

| 现象 | 处理方式 |
| --- | --- |
| `python` 不是内部或外部命令 | 重新安装 Python 并勾选“Add python.exe to PATH”，然后重新打开 PowerShell。 |
| `ModuleNotFoundError` | 在项目目录重新运行 `python -m pip install -r requirements.txt`。 |
| 提示找不到 Chromium | 运行 `python -m playwright install chromium`。 |
| 5000 端口被占用 | 关闭另一份本项目服务，或启动“用户2（分身）”并访问 5001 端口。 |

> 如果希望“解压后无需安装 Python 即可双击运行”，需要另行发布 Windows 便携版/安装包；当前 GitHub ZIP 是面向可自行安装依赖的源码版本。

## 功能全览

| 功能 | 用途 | 在网页中的位置 |
| --- | --- | --- |
| 多网址监测 | 同时抓取多个公司的招聘网站；一个网址失败不会中断其他网址 | 监测配置 → 目标招聘网址 |
| 两种抓取模式 | 对招聘首页按关键词搜索，或直接抓取已有筛选条件的固定网址 | 每个招聘网址右侧的模式选择 |
| 关键词、城市和排除条件 | 控制保留哪些职位 | 监测配置 |
| 稳定去重和网址标签 | 同一职位不会重复保存；保留首次命中的关键词标签 | 爬取的职位信息 |
| 定时监测与实时日志 | 按设定间隔循环抓取，并显示每个网址的处理进度 | 顶部监测状态、实时工作状态 |
| 职位浏览与筛选 | 折叠卡片、详情、分页、标题/描述搜索、网址标签筛选 | 爬取的职位信息 |
| 邮件通知 | 发现符合条件的新职位时发送汇总邮件；可先发测试邮件 | 监测配置 → 邮箱配置 |
| Gemini AI 筛选 | 通过自然语言要求进一步筛选规则命中的职位 | 监测配置 → Gemini API Key |
| GitHub 云端同步 | 一键上传私有配置到 GitHub Secret，下载云端运行产生的职位数据 | GitHub 同步 |
| 双配置分身 | 让两套网址、邮箱和职位数据互不影响地运行 | 用户1/用户2启动脚本 |

## 网页功能与使用方法

### 1. 添加和管理目标招聘网址

在“监测配置 → 目标招聘网址”中粘贴招聘网站网址，选择模式后点击“添加”。每行左侧会显示可编辑的网址标签；程序会优先从 URL 中的中文关键词自动生成标签，也可以直接点击标签手动改名。

| 模式 | 适用情形 | 程序行为 |
| --- | --- | --- |
| **按关键词搜索** | 招聘首页、未筛选的职位列表页 | 依次把“职位关键词”中的每一个词提交给该网站搜索。 |
| **固定网址直抓** | URL 已带部门、城市、岗位类别或关键词筛选 | 不提交全局职位关键词，只抓取这个 URL 当前返回的职位。 |

每个网址标签以前两个字决定颜色，前缀相同的标签会使用相同颜色。若同一职位同时被“法律”和“法务”等多个关键词搜到，职位只保留**第一次被抓到的关键词标签**，例如“腾讯法律”；后续命中不会变成“腾讯法律 / 腾讯法务”。

添加、编辑或删除网址后，点击“保存配置”。点击“启动监测”时程序也会自动先保存一次当前网页内容。

### 2. 设置职位筛选条件

“职位关键词”“城市”和“排除关键词”均以标签方式录入：输入内容后按 `Enter` 添加，点击标签上的删除按钮移除。

| 字段 | 如何生效 |
| --- | --- |
| 职位关键词 | 在“按关键词搜索”模式中逐词搜索；固定网址直抓不会使用它来改变网页 URL。 |
| 城市（可选） | 留空表示不限城市；填写后只保留地点匹配的职位。 |
| 排除关键词（可选） | 职位标题或描述含任意排除词时不保存，例如“实习”“兼职”。 |
| 监测间隔 | 以小时设置循环检查间隔。建议 2–4 小时，不要过于频繁。 |

### 3. 先做测试检查，再启动定时监测

配置完毕后，可按以下顺序操作：

1. 点击顶部“测试检查”。它会真实抓取并应用筛选规则，但**不会发送邮件、不会写入职位数据库或去重缓存**，适合确认网址和关键词是否可用。
2. 确认结果正常后，点击“启动监测”。程序会逐个抓取全部已保存网址，随后按设置的间隔重复检查。
3. 在“实时工作状态”中查看当前网址、进度和简洁日志，例如“正在抓取【B站】”“抓取完毕【B站】找到 n 个新职位”。
4. 需要停止时点击“停止监测”。程序会在当前抓取任务完成后安全退出循环。

### 4. 查看、搜索和清理职位

“爬取的职位信息”中的职位卡片默认折叠，按发布日期从新到旧排列。折叠时展示网址标签、职位名称和日期；点击卡片可展开岗位地点、描述、详情链接和发现时间。

- 顶部搜索框：按职位名称、公司或职位描述筛选。
- “按网址标签筛选”：输入如“腾讯”“B站法律”等字样，只显示标签中包含该词的职位。
- 底部分页：可查看所有页码；每页显示 10 条。
- “刷新”：重新读取本地数据库。
- “清除”：会要求二次确认，然后永久删除默认用户的**全部**职位记录和去重缓存；此操作不可恢复。

职位去重以规范化后的职位详情 URL 和职位信息为基础。同一职位再次出现在后续搜索结果中不会重复入库或重复通知。

### 5. 配置邮件通知

在“邮箱配置”填写发件邮箱、邮箱**授权码**、接收邮箱、SMTP 服务器和端口，然后：

1. 点击“保存配置”；
2. 点击“测试邮件发送”；
3. 收到测试邮件后再启动正式监测。

授权码不是邮箱登录密码。465 端口使用 SSL，其他端口使用 STARTTLS。正式监测时，只有新发现且通过筛选的职位才会进入通知；同一轮的新职位会汇总到一封邮件中。

### 6. 使用 Gemini AI 二次筛选（可选）

若仅靠关键词、城市和排除词不够精确，可填写 Gemini API Key 和“AI 筛选要求”，例如“只保留公司战略、商业分析或 CEO 办公室方向的职位”。两个字段都填写时才会调用 AI；任一字段留空时只使用普通规则筛选。API Key 只写入本机 `config.yaml`，不会提交到 GitHub 仓库。

### 7. 使用用户2分身

若要让两个人或两套求职条件相互隔离，启动 `启动Web服务-用户2(分身).bat`，访问 `http://127.0.0.1:5001`。它与默认用户分别使用：

- `config.yaml` / `config_user2.yaml`
- `jobs.db` / `jobs_user2.db`
- `jobs_cache.json` / `jobs_cache_user2.json`
- `app.log` / `app_user2.log`

两个分身可以同时运行；在同一台电脑上只需完成一次 Python 与 Playwright 的首次依赖安装。

## 已验证的关键词搜索站点

程序优先监听招聘官网自身的职位接口，并将结果统一保存为职位名称、地点、发布日期、岗位描述和详情链接。以下网址可直接在“目标招聘网址”中选择“关键词搜索”模式：

| 公司/平台 | 招聘网址 |
| --- | --- |
| 哔哩哔哩（B站） | `https://jobs.bilibili.com/social/positions?type=3` |
| Soul | `https://soulapp.jobs.feishu.cn/index` |
| 得物（Poizon） | `https://poizon.jobs.feishu.cn/index/position/list` |

B站会使用官网的 `name` 搜索参数，并解析其 `positionList` 职位接口。Soul 与得物使用飞书招聘门户的职位接口。招聘官网可能随时调整接口、页面结构或启用验证码；遇到这种情况请停止自动抓取，并遵守对应网站条款与访问频率限制。

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

工作流位于 `.github/workflows/scraper_cron.yml`，默认在 UTC 13:00（新加坡/北京时间每周二、周五 21:00）运行，也支持在 Actions 页面手动运行。

### 推荐：网页一键同步

在 GitHub 创建一个只允许访问目标仓库的 Fine-grained Personal Access Token，并授予：

- Actions：只读，用于下载最新职位数据包
- Secrets：读写，用于更新 `JOB_MONITOR_CONFIG_B64`

### 网页操作步骤

1. 将本项目推送到自己的 GitHub 仓库，并在 GitHub 仓库的 Actions 页面启用 `Daily Job Monitor`。
2. 创建只限该仓库的 Fine-grained Personal Access Token，并按上方要求授予权限。
3. 重启本地 Web 服务，在网页右侧“GitHub 同步”中填写 `owner/repository`（或完整仓库网址）和 Token，点击“保存连接”。Token 仅保存在本机的 `github_sync.yaml`，网页重新显示时会掩码处理。
4. 点击“一键上传配置”。程序会先保存当前网页配置，再把默认配置和所有用户分身配置加密写入 GitHub Actions Secret。
5. 在 GitHub 的 Actions 页面手动运行一次 `Daily Job Monitor`，或等待下一个定时运行。成功后，云端职位数据包才能下载。
6. 回到本地网页，先停止本地监测，再点击“一键下载职位数据”。程序会校验数据包、备份本地旧数据库和缓存，然后恢复云端数据。

两个同步按钮的作用如下：

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
