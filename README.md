# 招聘监测系统 Job Monitor

招聘监测系统帮助求职者集中跟踪多个公司的招聘页面：按关键词、城市和排除条件筛选职位，自动去重、保存和展示结果，并可通过邮件接收新职位汇总。项目使用 Python、Flask、Playwright 和 SQLite，支持本地运行、GitHub Actions 计划任务和多用户网页版。

## 直接使用网页版

**[打开招聘监测系统网页版](https://175.178.249.250/)**

不想安装或自行部署的话，可以直接打开网页版，使用邮箱验证码注册并登录，然后设置招聘网址、筛选条件和通知邮箱。每个账号有独立的配置和职位记录；可手动检查，也可以启用每周定时监测。邮件提醒依赖服务端发信配置，AI 筛选需自行配置相应服务商的 API Key。

## 选择使用方式

| 方式 | 适合谁 | 如何开始 |
| --- | --- | --- |
| 在线网页版 | 希望直接使用，无需安装或维护服务 | [打开网页版](https://175.178.249.250/)，注册并配置监测条件 |
| Windows 本地版 | 希望在自己电脑上运行和保存数据 | 按下方“从 GitHub 下载并在 Windows 上运行”安装并启动 |
| 自建多用户版 | 希望自行托管并管理多个账号 | 参考下方“多用户云端版”的部署说明 |
| GitHub Actions | 希望通过 GitHub 按计划执行 | 参考下方 GitHub Actions 配置说明 |

主要功能包括多网址监测、关键词搜索和固定网址直抓、职位筛选与去重、定时检查、邮件通知、Gemini/OpenAI/DeepSeek AI 辅助筛选，以及职位列表搜索和标签筛选。招聘网站页面变化、访问限制或验证码可能影响抓取效果。

## 多用户云端版

新用户以邮箱作为登录账号：先点击“发送验证码”，查收邮件并填写 6 位验证码，再设置密码完成注册。验证码 10 分钟有效，错误次数及重复发送均有限制；没有管理员发信配置或邮件发送失败时不能跳过验证。注册邮箱自动成为“邮件提醒”的默认接收地址，登录后可自行修改接收地址。旧账号仍可用原用户名登录，登录后可点击“验证并绑定邮箱”。

云端入口为 `cloud_portal.py`，与原来的本地 `app.py` 并存。访问云端网址后，用户先注册或登录；每个账号独立保存招聘网址、关键词、接收通知邮箱、所选 AI 服务商的 API Key、职位数据库和抓取日志。保存配置并启用每周计划后，`cloud_scheduler.py` 在云服务器上按北京时间运行，星期可以多选。服务器临时停机错过计划时，恢复后补抓一次。也可以登录后点“启动监测”立即排队抓取一次。单台 2 GB 内存服务器一次只执行一个账号的抓取任务，其他任务排队。

新云端账号默认不启用定时监测，也不预选星期；需要定时运行时请先选择星期并启用计划。手动启动一次抓取不会自动启用定时计划，已有账号保存的计划保持不变。

云端工作区的“运行记录”展示当前账号的历史任务，包含开始／结束时间、抓取网页、抓取职位数、实际新增入库数、任务类型、状态和邮件发送情况，支持分页。测试抓取不入库；升级前的旧记录若没有保存网页或新增数量，会显示“历史任务未记录”或“—”，不会用当前配置推断历史结果。本地工作区暂不持久化运行历史。

总览将当前状态、操作和三项摘要（今日新增、已收录职位、最近检查）集中展示。云端“立即检查”执行一次抓取；运行时才显示停止按钮。测试抓取放在“更多操作”中，本次日志与运行时长可展开“本次检查详情”查看；完整历史保留在“运行记录”。

云端工作区的“意见反馈”支持填写最多 5000 字的问题描述，并直接粘贴或选择截图（最多 3 张，每张 5 MB，支持 PNG、JPEG、GIF、WebP）。提交后管理员可在“管理员后台 → 用户反馈”查看账号、北京时间、文字及原图，并分页浏览。图片仅提交人和管理员可访问；反馈与图片保存在私有账号数据库中，删除账号时一并清理。部署时需安装 Pillow，并应用 Nginx 配置中 `/api/feedback` 的 16 MB 上传限制。

本地双用户脚本与 GitHub Actions 仍是独立的运行方式。云端新注册账号会获得安全模板内置的默认招聘网址、网址标签和抓取模式，但不会继承本地用户的关键词、城市、排除词、邮箱、API Key、职位记录或运行历史；用户可在网页中自由修改这些默认网址。云端页面隐藏 GitHub 同步功能，因为配置和职位已经直接保存在云服务器。

云端版的私有数据保存在 `cloud_data/`（或环境变量 `JOB_MONITOR_CLOUD_DATA_DIR` 指定的位置），包括 `accounts.sqlite3`、`password-vault.key`、管理员的 `admin-email.yaml`、各账号的 `config.yaml`、`jobs.db` 和 `jobs_cache.json`。整个目录必须随服务器一起备份，不能提交到 GitHub。密码同时保存 scrypt 登录哈希和由独立密钥加密的可逆副本；管理员发信邮箱授权码及用户的 AI API Key 存于权限受限的配置文件。

### 云端管理员与邮件提醒

管理员应先在 `/admin` 配置发件邮箱、授权码和 SMTP，并确认邮件可投递，再开放新用户注册。注册页说明邮箱用途；验证码仅以摘要存入账号数据库，不存明文，验证成功后即消费。管理员后台展示账号邮箱及其验证状态；旧账号未验证邮箱前仍保留原用户名。

普通用户在云端“邮件提醒”中只填写自己的接收邮箱；所有职位邮件均使用管理员统一设置的 SMTP 发件邮箱。用户保存配置后，旧的个人发件邮箱、SMTP 服务器和授权码字段会从其云端配置文件移除。云端抓取任务即使遇到旧配置，也只会使用管理员的发信设置。**本地双用户版和 GitHub Actions 的邮件配置方式不变。**

管理员登录后可从主界面进入 `/admin`：左侧“用户 / 用户名 / config.yaml”二级目录可查看该用户的注册时间和非密钥配置；“用户密码”页面会按公司内部管理要求解密并明文展示所有可用密码；发信邮箱设置在管理首页。页面不展示密码哈希、邮箱授权码或 API Key，管理员查看用户配置或密码都会留下审计记录。升级前已有账号的原密码无法从 scrypt 哈希恢复，会在该用户下一次成功登录时自动补录加密副本。必须将 `password-vault.key` 与数据库一同备份并严格限制读取权限；密钥丢失后，已有加密副本无法恢复。

删除用户：在管理员后台左侧选择该用户，在其配置详情页底部的“删除用户”区域输入该用户的完整账号，点击“永久删除该用户”。系统会删除该账号的密码、配置、职位、任务、缓存和日志，使其现有登录立即失效；其他账号和统一发信邮箱不受影响。操作无法撤销，当前登录的管理员账号不能删除。若该用户正在抓取，先点击“暂停并停止该用户监测”，等待任务结束并刷新页面后再删除；排队中的任务会随账号一起删除。

出于安全考虑，网页注册**不会自动获得管理员权限**。在服务器上确认目标用户名后执行：

```bash
JOB_MONITOR_CLOUD_DATA_DIR=/path/to/private/cloud_data .venv/bin/python cloud_admin.py promote USERNAME
```

仅通过服务器控制台授予管理员权限。管理员首次进入后台时填写发件邮箱、授权码、SMTP 服务器及端口；不要通过聊天、GitHub 或公开配置文件传递授权码。用户仍可在自己的页面保存接收邮箱并点击“测试邮件发送”。如果管理员未配置发件邮箱，测试和正式邮件无法发送；测试邮件每账号每小时最多三次。

Linux 部署需要 Python 3.10+、Playwright Chromium、Gunicorn、一个 HTTPS 反向代理和两个后台服务：Web 服务与调度服务。示例命令（在项目根目录）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install gunicorn
.venv/bin/python -m playwright install --with-deps chromium
JOB_MONITOR_HTTPS=1 .venv/bin/gunicorn --workers 1 --threads 20 --bind 127.0.0.1:8000 cloud_portal:app
# 另一个后台服务运行：
.venv/bin/python cloud_scheduler.py
```

反向代理必须将请求转发到 `127.0.0.1:8000` 并设置 Host、X-Forwarded-For、X-Forwarded-Proto；账号登录须使用 HTTPS。云端机器运行两个服务时需设置 `TZ=Asia/Shanghai`。建议只开放 SSH、HTTP、HTTPS；不要向公网开放 8000、5000 或 5001。开放注册意味着任何访客都能使用服务器资源，正式面向公众运行前应配置注册名额、备份与监控，并限制抓取进程访问内网地址。

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

4. 双击 `启动Web服务.bat`。看到访问地址后，在浏览器打开：

   ```text
   http://127.0.0.1:5000
   ```

5. 第一次打开网页后，在“目标招聘网址”中添加网址、选择“关键词搜索”或“固定直抓”，填写关键词；如需邮件通知，再填写 SMTP 邮箱配置并点击“测试邮件发送”。

首次启动时，如果没有 `config.yaml`，程序会自动从不含个人信息的 `config.example.yaml` 创建一份。之后在网页中填写并保存配置即可；已有的本地配置不会被覆盖。

### 日常启动与两套独立配置

依赖安装完成后，只需双击 `启动Web服务.bat`，无需重复安装。也可以在项目目录运行：

```powershell
python app.py
```

两套服务可同时打开，各自的网页、参数、职位数据和邮件通知均互不影响：

| 用户 | 启动脚本 | 网页地址 | 配置与运行数据 |
| --- | --- | --- | --- |
| 用户一 | `启动Web服务.bat` | `http://127.0.0.1:5000` | `config.yaml`、`jobs.db`、`jobs_cache.json`、`app.log` |
| 用户二 | PowerShell 中运行 `.\启动Web服务.bat user2` | `http://127.0.0.1:5001` | `config_user2.yaml`、`jobs_user2.db`、`jobs_cache_user2.json`、`app_user2.log` |

请分别在两个网页中填写网站、关键词、城市、排除词、邮箱和 Gemini 设置。用户一的修改不会写入用户二的文件，反之亦然。

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
| 定时监测与实时日志 | 按每周选定日期和时间抓取，漏跑时补抓一次 | 顶部监测状态、本地每周运行计划 |
| 职位浏览与筛选 | 折叠卡片、详情、分页、标题/描述搜索、公司与关键词标签筛选 | 职位列表 |
| 邮件通知 | 发现符合条件的新职位时发送汇总邮件；可先发测试邮件 | 监测配置 → 邮箱配置 |
| AI 筛选（Gemini、OpenAI、DeepSeek） | 每轮抓取后判断本轮新职位，只保存并邮件通知符合要求的职位 | 工作区 → AI 筛选 |
| GitHub 云端同步 | 当前用户一键上传自己的私有配置到独立 GitHub Secret，并下载自己的云端职位数据 | GitHub 同步 |
| 双配置分身 | 两套网址、关键词、邮箱、职位数据和云端 Action 均互不影响 | 用户1/用户2启动脚本 |

## 网页功能与使用方法

### 1. 添加和管理目标招聘网址

在“监测配置 → 目标招聘网址”中粘贴招聘网站网址，选择模式后点击“添加”。已有网址默认以紧凑标签排列；点击标签会在当前标签行的下方展开整行编辑气泡，显示网址、抓取方式及复制、删除操作，下方标签随之下移。同一时间只展开一条，点击展开标签右侧的箭头或按 Esc 可收起；屏幕宽度改变时会自动调整位置。展开后的标签可直接改名；程序也会优先从 URL 中的中文关键词自动生成标签。

| 模式 | 适用情形 | 程序行为 |
| --- | --- | --- |
| **按关键词搜索** | 招聘首页、未筛选的职位列表页 | 依次把“职位关键词”中的每一个词提交给该网站搜索。 |
| **固定网址直抓** | URL 已带部门、城市、岗位类别或关键词筛选 | 不提交全局职位关键词，只抓取这个 URL 当前返回的职位。 |

每个网址标签以前两个字决定颜色，前缀相同的标签会使用相同颜色。若同一职位同时被“法律”和“法务”等多个关键词搜到，职位只保留**第一次被抓到的关键词标签**，例如“腾讯法律”；后续命中不会变成“腾讯法律 / 腾讯法务”。

添加、编辑或删除网址后，页面会自动保存；可在“通知与运行计划”中查看保存状态。点击“启动监测”时程序也会先保存当前网页内容。

配置保存遇到网络中断、超时或服务临时不可用时，会自动重试；重试期间继续修改会改为保存最新内容。多次失败后可点击“重试保存”，网络恢复时也会重试尚未保存的更改。登录或页面验证失效时会明确提示重新登录或刷新；请确认状态显示已保存后再离开页面。

### 1.1 导出并交接招聘网站配置

若要把目标招聘网站交给另一台电脑或另一个 Agent，运行：

```powershell
python site_config_transfer.py export
```

这会生成被 Git 忽略的 `job-monitor-site-config.yaml`。文件只包含每个用户的招聘网址、网址标签、抓取模式，以及作为参考的搜索关键词；**不包含**邮箱、授权码、AI API Key、GitHub Token/设置、城市和排除词、数据库、缓存或已抓取职位。

将该文件复制到另一台电脑的项目目录后，可由 Agent 或在 PowerShell 中执行：

```powershell
# 把文件中的用户一网站合并到目标电脑的用户二；关键词需要明确选择才会导入
python site_config_transfer.py import --input job-monitor-site-config.yaml --source-profile user1 --target-profile user2 --include-keywords
```

导入默认只新增不存在的网址，并保留目标用户已有的网站标签、抓取模式和所有私有配置。若确认要用导出文件完全替换目标用户的网址列表，可额外加 `--replace`。导入后重启对应用户的 Web 服务即可看到更新。

### 2. 设置职位筛选条件

“职位关键词”“城市”和“排除关键词”均以标签方式录入：输入内容后按 `Enter` 添加，点击标签上的删除按钮移除。

| 字段 | 如何生效 |
| --- | --- |
| 职位关键词 | 在“按关键词搜索”模式中逐词搜索；固定网址直抓不会使用它来改变网页 URL。 |
| 城市（可选） | 留空表示不限城市；填写后只保留地点匹配的职位。 |
| 排除关键词（可选） | 职位标题或描述含任意排除词时不保存，例如“实习”“兼职”。 |
| 本地每周运行计划 | 周一到周日可多选，并设置 HH:MM；所有选定日期使用同一时间，按电脑本地时区运行。 |

### 3. 先做测试检查，再启动定时监测

配置完毕后，可按以下顺序操作：

1. 点击顶部“测试检查”。它会真实抓取并应用筛选规则，但**不会发送邮件、不会写入职位数据库或去重缓存**，适合确认网址和关键词是否可用。
2. 选择每周运行日期（可多选）和时间，勾选“启用本地定时监测”并保存。程序按计划逐个抓取全部网址，保存启用后自动开始等待。
3. 总览的状态说明显示当前任务，展开“本次检查详情”查看简洁日志，例如“正在抓取【B站】”“抓取完毕【B站】找到 n 个新职位”。
4. 需要停止时点击“停止监测”。程序会在当前抓取任务完成后安全退出循环。

本地计划会保存执行进度。电脑关机、休眠或程序关闭导致漏跑时，下次通过 BAT 启动程序会自动恢复已启用计划，并合并漏跑任务补抓一次；不会按漏跑次数连续抓取。首次保存或修改星期/时间从保存时起计时，不追溯旧计划。正常唤醒后通常在 15 秒内检查是否到期。

点击“停止监测”会持久保存暂停状态，重启不会自动启用；重新启用原计划时会检查漏跑。抓取返回失败也算该轮已尝试，不会每 15 秒重试；进程在抓取途中退出则重启后补跑。两套用户的计划和进度独立，GitHub Actions 仍由各自工作流中的 cron 决定运行时间。未在网页保存新计划的旧配置暂时兼容原来的间隔运行方式。

### 4. 查看、搜索和清理职位

“职位列表”中的职位卡片默认折叠，按发布日期从新到旧排列。卡片分别展示公司和命中的关键词；固定网址直抓不显示关键词，无法确认的关键词显示为 `-`。点击卡片可展开详情。

- 顶部搜索框：按职位名称、公司或职位描述筛选。
- 公司和关键词标签默认全选；点击标签上的 × 可排除，通过下拉菜单勾选、全选或清空。固定网址直抓的岗位不受关键词筛选影响。
- 底部分页：可查看所有页码；每页显示 10 条。
- “刷新”：重新读取本地数据库。
- “清除”：会要求二次确认，然后永久删除默认用户的**全部**职位记录和去重缓存；此操作不可恢复。

职位去重以规范化后的职位详情 URL 和职位信息为基础。同一职位再次出现在后续搜索结果中不会重复入库或重复通知。

### 5. 配置邮件通知

在“邮箱配置”填写发件邮箱、邮箱**授权码**、接收邮箱、SMTP 服务器和端口，然后：

1. 等待配置自动保存；
2. 点击“测试邮件发送”；
3. 收到测试邮件后再启动正式监测。

授权码不是邮箱登录密码。465 端口使用 SSL，其他端口使用 STARTTLS。正式监测时，只有新发现且通过筛选的职位才会进入通知；同一轮的新职位会汇总到一封邮件中。

### 6. 使用 AI 二次筛选（可选）

若仅靠关键词、城市和排除词不够精确，可打开工作区的“AI 筛选”页面，选择 Gemini、OpenAI 官方 API 或 DeepSeek，填写该服务商的 API Key、模型名称和“AI 筛选要求”（例如“只保留公司战略、商业分析或 CEO 办公室方向的职位”），开启开关并保存。模型留空时分别使用 `gemini-3.5-flash-lite`、`gpt-5.4-mini`、`deepseek-flash`；请确认所选模型在自己的 API 账号下可用。三个服务商的 Key 独立保存，切换不会清除其他 Key；关闭开关后仍保留设置，但只使用普通规则筛选。旧配置没有服务商字段时仍使用 Gemini；没有开关字段时仍沿用“当前服务商的 Key 和要求均已填写则启用”的行为。

AI 筛选启用后，程序先抓完本轮全部网址，对职位去重并排除已通知的职位；再以“[提示词] + [本次抓取的职位信息]”的形式，将新职位的完整结构（标题、公司、地点、描述、网址、标签、发布时间等）分批发送给所选服务商。只有模型明确选中的职位才写入数据库和本轮邮件。模型未选中的职位会记入当前筛选要求的去重缓存，不会在同样的设置下每轮重复调用；修改筛选要求或模型后可重新判断。已入库但邮件发送失败的职位，下次会继续尝试发送。API 失败、超时或响应无法解析时，不会将未审核职位当作通过。关闭 AI 开关则继续使用普通关键词、城市和排除词规则。

Key 保存在当前用户的私有配置文件中，页面只显示掩码；本地配置文件被 Git 忽略。这里的 OpenAI 选项连接官方 API，DeepSeek 选项连接 DeepSeek 官方 API，不接受任意自定义 API 地址。

### 7. 同时使用用户一和用户二

若要让两个人或两套求职条件相互隔离，请双击 `启动Web服务.bat` 启动用户一，并在项目目录的 PowerShell 中运行 `.\启动Web服务.bat user2` 启动用户二。两个网页可同时打开，端口分别为 5000 和 5001，并分别使用：

- `config.yaml` / `config_user2.yaml`
- `jobs.db` / `jobs_user2.db`
- `jobs_cache.json` / `jobs_cache_user2.json`
- `app.log` / `app_user2.log`

两个用户可以同时运行；在同一台电脑上只需完成一次 Python 与 Playwright 的首次依赖安装。每个网页里的“开始监测”“一键上传配置”“一键下载职位数据”都只影响当前用户。

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
openai_api_key: ''
openai_model: gpt-5.4-mini
deepseek_api_key: ''
deepseek_model: deepseek-flash
ai_provider: gemini
ai_filter_prompt: ''
ai_filter_enabled: false
```

邮箱 465 端口使用 SSL，其他端口使用 STARTTLS。新配置需要开启 `ai_filter_enabled`，且所选服务商的 API Key 和 `ai_filter_prompt` 都已填写，才会调用 AI；没有开关字段的旧配置保持原有行为。

## GitHub 自动运行：用户一和用户二完全分开

云端现在有两个独立工作流，默认均在 UTC 13:00（新加坡/北京时间每周二、周五 21:00）运行，也都可以在 GitHub Actions 页面分别手动运行：

| 用户 | 工作流文件与名称 | 私有配置 Secret | 独立缓存与下载构件 |
| --- | --- | --- | --- |
| 用户一 | `.github/workflows/user1_monitor.yml` / `User 1 Job Monitor` | `JOB_MONITOR_USER1_CONFIG_B64` | `job-monitor-state-user1` |
| 用户二 | `.github/workflows/user2_monitor.yml` / `User 2 Job Monitor` | `JOB_MONITOR_USER2_CONFIG_B64` | `job-monitor-state-user2` |

每条工作流只恢复并运行自己的配置，只读写自己的数据库与去重缓存；两个工作流可同时运行，互不取消、互不覆盖。旧的 `Daily Job Monitor`、旧 Secret `JOB_MONITOR_CONFIG_B64` 和旧构件不会再被新工作流使用。

### 推荐：分别在两个本地网页一键同步

先在 GitHub 创建一个仅限目标仓库的 Fine-grained Personal Access Token，并授予：

- Actions：只读，用于下载当前用户的最新职位数据包；
- Secrets：读写，用于更新当前用户的配置 Secret。

然后按下面顺序做一次初始化：

1. 双击 BAT 启动用户一，再通过 `.\启动Web服务.bat user2` 启动用户二，分别打开 `http://127.0.0.1:5000` 与 `http://127.0.0.1:5001`。
2. 分别填写各自的网站、关键词、邮箱和其他参数，等待自动保存完成。
3. 在两个网页的“GitHub 同步”中填写同一个 `owner/repository` 和 Token，等待连接设置自动保存。Token 只保存在本机、被忽略的 `github_sync.yaml`，网页只显示掩码。
4. 在用户一网页点击“一键上传配置”，只会上传 `config.yaml` 至 `JOB_MONITOR_USER1_CONFIG_B64`；在用户二网页点击同一按钮，只会上传 `config_user2.yaml` 至 `JOB_MONITOR_USER2_CONFIG_B64`。邮箱授权码和 AI API Key 随各自的 GitHub Secret 上传；Base64 编码本身不提供加密。
5. 打开 GitHub Actions，分别启用并手动运行一次 `User 1 Job Monitor` 与 `User 2 Job Monitor`，或等待下一个定时运行。
6. 要同步云端数据回电脑时，停止对应网页的本地监测，再在该用户自己的网页点击“一键下载职位数据”。用户一只能下载 `job-monitor-state-user1`，用户二只能下载 `job-monitor-state-user2`；文件名不匹配会被程序拒绝，而不会覆盖另一人的数据。

下载前必须停止该用户的本地监测。覆盖前会在 `backups/github-sync-时间/` 备份该用户已有的数据库和缓存。

### 手动创建两个配置 Secret（可选）

如果不使用网页上传，请先在本地完成两套配置，然后分别运行：

```powershell
# 用户一
python github_config.py export --config config.yaml --include-sensitive-values --output .job-monitor-config-user1.b64

# 用户二
python github_config.py export --config config_user2.yaml --include-sensitive-values --output .job-monitor-config-user2.b64
```

进入 GitHub：`Settings → Secrets and variables → Actions → New repository secret`，新建：

| Secret 名称 | 内容 |
| --- | --- |
| `JOB_MONITOR_USER1_CONFIG_B64` | `.job-monitor-config-user1.b64` 的完整单行内容 |
| `JOB_MONITOR_USER2_CONFIG_B64` | `.job-monitor-config-user2.b64` 的完整单行内容 |

`--include-sensitive-values` 会把对应用户的邮箱授权码和 AI API Key 一并放进该用户的 GitHub Secret。Base64 只是编码；导出文件本身仍是敏感文件，设置完成后应删除。若故意不包含敏感字段，可额外设置相互独立的可选 Secret：用户一的 `JOB_MONITOR_USER1_EMAIL_AUTH_CODE` / `GEMINI_API_KEY_USER1` / `OPENAI_API_KEY_USER1` / `DEEPSEEK_API_KEY_USER1`，以及用户二的 `JOB_MONITOR_USER2_EMAIL_AUTH_CODE` / `GEMINI_API_KEY_USER2` / `OPENAI_API_KEY_USER2` / `DEEPSEEK_API_KEY_USER2`。

每个工作流的 Actions 缓存用于持久化去重状态，不是永久数据库；长期停用后缓存可能被 GitHub 清理，下一次运行会重新建立状态。工作流不会提交或推送配置、数据库、缓存或日志。Fork 不会继承上游仓库的 Secrets 和 Actions 缓存，Fork 用户需要自行设置两套 Secret。

构件包含职位数据库和去重缓存。公开仓库的 Actions 数据包不应被当作私密存储；如职位明细、网址标签或监测方向需要保密，请使用私有 GitHub 仓库。

## 首次上传 GitHub 前检查

当前目录中的真实配置可能包含邮箱地址、授权码和 API Key。初始化 Git 仓库后，先确认这些文件确实被忽略：

```powershell
git check-ignore config.yaml config_user2.yaml jobs.db jobs_user2.db jobs_cache.json jobs_cache_user2.json app.log app_user2.log github_sync.yaml backups
git status --short
```

不要使用 `git add -f` 强制添加这些文件。如果敏感配置曾经被提交，即使后来删除也仍可能存在于 Git 历史中，需要清理历史并立即更换相关密钥。

## 命令行

```powershell
# 测试抓取与筛选，不发邮件、不写数据库
python main.py --test --once

# 正式检查一次
python main.py --once

# 持续监测（也可运行 .\启动Web服务.bat cli）
python main.py

# 使用用户二分身（必须同时指定 JOB_PROFILE；网页模式可用 BAT 的 user2 参数）
$env:JOB_PROFILE = 'user2'
python main.py --config config_user2.yaml --once

# 所有本地分身各检查一次
python run_all_profiles.py --once
```

## 验证与排错

数据库的职位发现时间统一使用程序运行环境的本地时间；写入、最近 N 小时查询、旧职位清理和今日统计采用同一口径。云端服务及 GitHub Actions 设置 `TZ=Asia/Shanghai`。已有发现时间不会被批量改写。

`import database` 不会创建数据库；默认数据库在首次实际调用时打开。旧数据库的职位身份升级由 `PRAGMA user_version` 记录，仅执行一次，合并重复记录前自动生成数据库备份。维护代码可使用 `JobDatabase(path, migrate_legacy=False)` 暂停旧数据自动升级；修改身份算法时需同步提升迁移版本。备份同样含有私有职位信息，应与数据库一起妥善保管。

本地和云端的事件接口按客户端事件编号返回有限批次，浏览器每约 2 秒自动续接并补收事件。多个标签页各自保留读取位置；云端每次响应立即结束，空闲页面不会持续占用 Gunicorn 工作线程。配置校验与抓取流程由各运行入口共用。双用户 GitHub 工作流保留独立计划和 Secrets，执行步骤统一在 `monitor_profile.yml`；Windows 使用 `启动Web服务.bat` 启动服务。

```powershell
python -m py_compile app.py main.py database.py scraper.py matcher.py notifier.py config_bootstrap.py github_config.py github_sync.py
python -m unittest discover -v
```

如果抓取时报浏览器不存在，再运行：

```powershell
python -m playwright install chromium
```

招聘网站可能调整页面结构或启用验证码。遇到验证码时应停止自动抓取，并遵守网站条款与访问频率限制。
