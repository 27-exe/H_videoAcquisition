# 🎬 iwara/hanime1 自动化视频爬取与分发系统

一个功能完整的自动化媒体爬取、处理与分发系统。支持从 **iwara.tv** 和 **hanime1.me** 自动爬取视频资源，通过 **Aria2** 高效下载，并自动生成预览图、排行榜，最终推送至 Telegram 频道。

**一些特性**：
- 🔒 Cloudflare 防护绕过（基于 Playwright 无头浏览器）
- 📊 去重与数据库持久化
- 🎨 自动化媒体处理（缩略图、预览图、排行榜生成）
- ⏰ 支持定时任务

---

## 🛠️ 核心功能详解

### 1️⃣ 目前适配站点爬取
- **iwara.tv**
  - 支持登录认证(应对未来登录限制,现在非必须)
  - 自动提取视频元数据、下载链接、排行数据
  - 支持多关键词搜索与排序
  
- **hanime1.me**
  - 无需登录
  - 支持类型/标签筛选
  - 实时排行数据爬取

### 2️⃣ Cloudflare 绕过
- 使用 **Camoufox**（基于 Firefox）+ **Playwright** 无头浏览器
- 自动处理 CF 验证（3s challenge）


### 3️⃣ 下载管理
- **Aria2 RPC 集成**：
  

### 4️⃣ 自动化媒体处理
- **FFmpeg 异步任务执行**
  - 视频封面提取（第7秒帧）
  - Telegram 标准缩略图生成（320px）
  - 预览图生成与排名水印叠加
  
- **文件名清理**
  - 自动移除特殊字符
  - Emoji 支持
  - 长度限制处理

### 5️⃣ 数据持久化
- **SQLite 数据库**（`bot.db`）
  - `iwara_info` 表：跟踪已爬取视频（URL、标题、频道消息ID）
  - `hanime1_info` 表：hanime1 视频记录（视频ID、标题、频道消息ID）
  - 自动去重，防止重复爬取与推送

### 6️⃣ 灵活任务调度
- **APScheduler** 定时任务框架
  - iwara：每天 12:00 执行
  - hanime1：每隔 2 天 16:00 执行
  - 支持动态启停与暂停恢复
  - 错过任务自动补偿（最多30分钟延迟容差）

### 7️⃣ Telegram 集成
- **Telethon MTProto 客户端**
  - 视频分发至专用频道
  - 预览图与排行榜发布至第二频道
  - Top 5 排行榜自动生成
  - 支持长视频直接上传（Telegram 文件限制内）

### 8️⃣ 管理员控制接口
- 通过 Telegram 指令完全控制系统状态
- 支持手动触发任务、查询状态、远程关闭

---


### 🔧 核心依赖

| 模块 | 用途 | 关键版本 |
|------|------|--------|
| **telethon** | Telegram 客户端 | 1.42.0 |
| **aioaria2** | Aria2 下载管理 | 1.3.6 |
| **playwright** + **camoufox** | 无头浏览器 + CF绕过 | 1.58.0 + 0.4.11 |
| **lxml** | HTML 解析 | 6.0.2 |
| **pillow** | 图像处理 | 12.1.0 |
| **aiosqlite** | 异步数据库 | 0.22.1 |
| **apscheduler** | 任务调度 | 3.11.2 |

---

## 🚀 快速开始

### 前置环境要求

```bash
# 系统要求
- Python 3.10+
- FFmpeg（视频处理）
- Aria2（下载引擎）
```

#### Windows 用户
```powershell
# 安装 FFmpeg
# 1. 下载并安装：https://ffmpeg.org/download.html
# 2. 添加到 PATH 环境变量

# 安装 Aria2
# 1. 下载：https://github.com/aria2/aria2/releases
# 2. 添加到 PATH 环境变量
```

#### Linux/macOS 用户
```bash
# Ubuntu/Debian
sudo apt-get install ffmpeg aria2

# macOS
brew install ffmpeg aria2
```

### 1️⃣ 克隆与初始化

```bash
git clone <repo_url>
cd videoAcquisition

# 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Linux/macOS
# 或 venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 2️⃣ 配置文件设置

#### 2.1 Telegram 凭据 (`config/token.json`)

从 [BotFather](https://t.me/BotFather) 申请你的 bot 和 API 凭据

```json
{
  "api_id": 123456,
  "api_hash": "abcdef1234567890abcdef1234567890",
  "bot_token": "123456:ABCdefGHIjklmnOPQRstUVWxyz"
}
```

#### 2.2 机器人配置 (`config/bot_cfg.json`)

```json
{
  "bot_username": "@your_bot_username",
  "admin_id": 123456789
}
```

你可以通过 `https://t.me/userinfobot` 获取你的 User ID

#### 2.3 Aria2 配置 (`config/aria2.json`)

```json
{
  "aria2_rpc_uri": "http://127.0.0.1:6800/jsonrpc",
  "aria2_rpc_secret": "your_aria2_rpc_token"
}
```


#### 2.4 iwara 爬虫配置 (`config/iwara.yaml`)

```yaml
name: "iwara_spider"
base_url: "https://www.iwara.tv/"
page: 0
keywords: "trending"  # 可选值: trending, views, likes, newest

# 代理配置（可选，理论上不需要）
proxy_url: "http://127.0.0.1:7890"
proxy_name: "username"      #  用户名
proxy_pass: "password"      # 密码

# iwara 登录凭据（可选）
username: "your_iwara_username"
password: "your_iwara_password"

# 浏览器 UA
headers:
  accept: '*/*'
  accept-language: 'zh-CN,zh;q=0.9'
  user-agent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# Telegram 频道
video_channel: "@channel_name"     # 视频存放频道
pic_channel: "@channel_name"       # 预览图和排行榜频道
```

#### 2.5 hanime1 爬虫配置 (`config/hanime1.yaml`)

```yaml
name: "hanime1_spider"
base_url: "https://hanime1.me/"
page: 1
keywords: "2.5D"  # 可选值请根据hanime1的分类自行查找

# 代理配置
proxy_url: "http://127.0.0.1:7890"
proxy_name: "username"
proxy_pass: "password"

# 浏览器 UA
headers:
  User-Agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Telegram 频道
video_channel: "@channel_name"
pic_channel: "@channel_name"
```

### 3️⃣ 创建 Telegram 频道

1. 创建两个私密频道：
   - `video_channel`：存放源视频
   - `pic_channel`：存放预览图和排行榜

2. 将机器人添加为管理员

3. 在配置文件中填入频道 ID 或 `@channel_name`

### 4️⃣ 运行机器人

```bash
python main.py
```


---

## ⌨️ 管理指令

所有命令需要管理员权限（在 `bot_cfg.json` 配置的 `admin_id`）
此为预设,你可以在 `bot_command.py` 中自定义指令回复

| 指令 | 作用 | 返回值 |
|------|------|--------|
| `/start` | 测试机器人可用性 | "你发现我啦！" |
| `/help` | 获取帮助 | 机器人信息 |
| `/update` | 启动所有定时任务调度器 | "全部开始更新" |
| `/i_resume` | 恢复 iwara 定时任务 | "更新 iwara" |
| `/h_resume` | 恢复 hanime1 定时任务 | "更新 hanime1" |
| `/i_stop` | 暂停 iwara 定时任务 | "暂停更新 iwara" |
| `/h_stop` | 暂停 hanime1 定时任务 | "暂停更新 hanime1" |
| `/run_iwara` | 立即执行一次 iwara 爬取任务 | 爬取结果 |
| `/run_hanime1` | 立即执行一次 hanime1 爬取任务 | 爬取结果 |
| `/bye` | 安全关闭机器人 | "正在关闭机器人..." |

### 命令示例

```
💬 发送给机器人：
  /update

🤖 机器人响应：
  全部开始更新
  
📅 自动执行：
  - iwara 每天 12:00
  - hanime1 每隔 2 天 16:00
```

---

## 📁 项目结构

```
videoAcquisition/
├── main.py                       # 程序入口
├── requirements.txt              # 依赖清单
├── bot.db                        # SQLite 数据库（自动创建）
├── bot.log                       # 日志文件
│
├── config/                       # 配置目录
│   ├── token.json               # Telegram API 凭据
│   ├── bot_cfg.json             # 机器人配置
│   ├── aria2.json               # Aria2 RPC 配置
│   ├── hanime1.yaml             # hanime1 爬虫配置
│   ├── iwara.yaml               # iwara 爬虫配置
│   └── auth/                    # 认证文件目录
│       └── iwara_auth.json      # iwara 登录状态（自动生成）
│
├── command/                      # Telegram 指令处理
│   └── bot_command.py           # 命令路由与处理器
│
├── spiders/                      # 爬虫模块
│   ├── base_spider.py           # 爬虫基类与数据结构
│   ├── hanime1/
│   │   ├── crawler.py           # hanime1 爬虫实现
│   │   └── tasks.py             # hanime1 完整流程任务
│   └── iwara/
│       ├── crawler.py           # iwara 爬虫实现
│       └── tasks.py             # iwara 完整流程任务
│
├── pipelines/                    # 处理管道
│   ├── load.py                  # 配置加载（JSON/YAML）
│   ├── data_base.py             # 数据库操作（去重、记录）
│   ├── aria2_download.py        # Aria2 下载管理
│   └── telegram_send.py         # Telegram 上传与发布
│
├── scheduled/                    # 任务调度
│   └── task.py                  # APScheduler 定时任务配置
│
├── utils/                        # 工具函数
│   ├── logging_setup.py         # 日志配置
│   ├── request_utils.py         # HTTP 请求 + 浏览器引擎
│   ├── parse_utils.py           # HTML 解析与数据提取
│   └── pic_utils.py             # FFmpeg 视频处理
│
├── download/                     # 下载目录（自动创建）
│   ├── hanime1/
│   │   ├── video/               # 视频文件
│   │   ├── cover/               # 封面与缩略图
│   │   └── preview/             # 预览图
│   └── iwara/
│       ├── video/
│       ├── cover/
│       └── preview/
│
├── temp/                         # 临时文件
├── error_shot/                   # 错误截图
└── SimHei.ttf                    # 字体文件（水印文字）
```

---

## 🔍 工作流程详解

### iwara 爬取流程

```
1. 加载配置 (iwara.yaml)
   ↓
2. 初始化爬虫 + 数据库连接
   ↓
3. 登录认证（如果配置了用户名密码）
   ↓
4. 爬取页面 (使用 Playwright + CF绕过)
   ├─ 获取视频标题、URL、下载链接
   ├─ 提取排行数据（观看数、上传日期）
   └─ 数据库去重（跳过已爬取的视频）
   ↓
5. 批量下载 (Aria2)
   ├─ 支持断点续传与重试
   └─ 检查本地是否已存在（跳过重复下载）
   ↓
6. 媒体处理 (FFmpeg 异步)
   ├─ 提取封面（第7秒帧）
   ├─ 生成缩略图（320px for Telegram）
   └─ 生成预览图 + 排名水印
   ↓
7. 上传到 Telegram
   ├─ 视频上传至 video_channel
   ├─ 预览图上传至 pic_channel
   └─ 保存消息 ID 到数据库
   ↓
8. 生成排行榜
   └─ Top 5 排行榜发布至 pic_channel
```



---

## ⚙️ 高级配置

### 代理配置

如果目标网站有地区限制，配置 http 代理：

```yaml
proxy_url: "http://127.0.0.1:1080"      # http 地址
proxy_name: "username"                    # 用户名
proxy_pass: "password"                    # 密码
```


### 日志级别调整

编辑 `main.py` 中的日志配置：

```python
setup_logging(log_file='bot.log', level=logging.DEBUG)  # 详细日志
# 或
setup_logging(log_file='bot.log', level=logging.WARNING)  # 仅警告
```

### 并发数限制

```python
# request_utils.py
MAX_CONCURRENT_BROWSERS = 3  # 同时开启的浏览器实例数（建议 2-4）

# aria2_download.py
"max-connection-per-server": "16"  # Aria2 单个文件最大连接数
```

---

## 🐛 常见问题

### Q1: "403 Forbidden" 错误
- **原因**：Cloudflare 验证失败
- **解决**：
  - 检查代理配置
  - 更新 User-Agent
  - 增加 `max_retries` 参数

### Q2: 视频下载失败
     iwara的下载很不稳定,如遇没有速度导致的失败直接终止任务再次进行爬取.
     第二次访问不会访问下载过的,有助于缓解.一直失败建议等待十分钟后再次尝试.
- **原因**：网络不稳定或下载链接失效
- **解决**：
    - 检查 Aria2 日志
    - 增加重试次数
    - 更换代理节点
  

### Q3: FFmpeg 找不到
- **原因**：FFmpeg 未安装或不在 PATH
- **解决**：
  ```bash
  # 检查是否安装
  ffmpeg -version
  
  # 添加到 PATH
  export PATH=$PATH:/usr/local/bin  # Linux/macOS
  setx PATH "%PATH%;C:\ffmpeg\bin"  # Windows
  ```

### Q4: 数据库锁定错误
- **原因**：多进程同时操作数据库
- **解决**：
  - 检查是否有多个 Python 进程运行
  - 删除 `bot.db-journal` 文件
  - 重启程序

### Q5: Telegram 消息未发送
- **原因**：
  - Bot Token 无效
  - 频道设置错误
  - 网络连接问题
- **解决**：
  - 验证 `token.json` 配置
  - 确认机器人是否具有频道管理员权限
  - 检查网络连接和代理

---

## 📊 监控与日志

### 日志文件

程序自动生成 `bot.log` 文件，包含：
- 爬虫执行状态
- 下载进度
- 上传结果
- 错误信息与堆栈跟踪


### 错误截图

爬虫遇到 CF 验证或页面加载异常时，会自动保存浏览器截图至 `error_shot/` 目录，便于调试。

---

## 🤝 贡献与反馈

- 发现 BUG？提交 Issue
- 有改进建议？提交 Pull Request
- 需要技术支持？查看日志并提供完整错误堆栈


---

## ⚠️ 免责声明

1. **法律合规**：本程序仅供技术交流与学术研究使用。使用者必须遵守以下规定：
   - 遵守当地法律法规
   - 尊重网站的 Robots.txt 和 Terms of Service
   - 不进行商业性爬取与传播

2. **道德责任**：
   - 不传播非法内容
   - 不用于骚扰或侵犯他人隐私
   - 尊重内容创作者的版权

3. **责任限制**：
   - 因使用本程序而产生的任何法律后果，由使用者自行承担
   - 开发者不承担因程序 BUG 或误用而导致的任何直接或间接损失
   - 对因网站反爬虫措施更新而导致功能失效不负责

4. **账号安全**：
   - 定期更改密码
   - 避免使用主号进行爬虫操作
   - 监控账号登录日志


**最后更新**：2026-02-22  


如有问题，请通过日志定位错误，或提交 Issue 反馈！🚀

