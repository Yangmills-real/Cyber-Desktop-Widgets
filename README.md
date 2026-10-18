桌面悬浮窗小工具集
> 三个基于 **Python + tkinter** 的 Windows 桌面小窗：双时区时钟、远程流量监控、财经新闻阅读。
> belike：<img width="790" height="296" alt="image" src="https://github.com/user-attachments/assets/7821f36a-7d31-4854-8712-dc98b0f0d337" /><img width="497" height="219" alt="image" src="https://github.com/user-attachments/assets/b11f485d-0c05-4d02-8049-c549fb781a28" /><img width="628" height="710" alt="image" src="https://github.com/user-attachments/assets/d0ebe745-882e-46a5-ba5d-82469441f97c" />
>
> 其中**时钟完全离线，不需要服务器**；另外两个通过 SSH 读取你 VPS 上的数据。
三个工具都做到了：
无边框 + 始终置顶，鼠标在窗口任意位置按住即可拖动
一键沉底 —— 调 `SetWindowPos(HWND_BOTTOM)` 真正压到所有窗口之下，而不是只 `lower()`
全局热键唤醒 —— 基于 `RegisterHotKey`，窗口沉底、失去焦点时也能一键召回
SSH 长连接 —— `paramiko.Transport` 只在启动时握手认证一次，不会反复登录触发 Fail2ban
无安装程序、无注册表、无后台服务，关掉窗口就彻底退出
---
目录
章节	内容
1. 快速开始	装依赖、跑起来
2. 配置	把 `YOUR_` 占位符换成你自己的
3. 私钥放哪里	为什么要放 `~/.ssh/`
4. 打包成 exe	文件夹模式打包 + 分发方式
5. 全局快捷键	三个工具的键位分配
6. 常见问题	弹杀软、启动即退、日期不上色…
7. 发布前自检	别把密钥推上去
8. 说明	设计取舍与已验证环境
---
包含内容
文件	功能	需要 VPS	主要依赖
`TimeClock.py`	双时区时钟：大字北京时间 + 小字美西时间，按中国/美国节假日给日期上色，带番茄钟休息提醒	❌	`tzdata`、`chinesecalendar`（可选）
`traffic_monitor.py`	远程流量监控：SSH 读服务器 `/proc/net/dev`，显示上下行总量与连接状态	✅	`paramiko`
`news_reader.py`	报纸风财经新闻阅读窗：SSH 读服务器上的文本文件，每 5 分钟自动刷新	✅	`paramiko`
---
1. 快速开始
```bash
pip install -r requirements.txt
python TimeClock.py
```
`requirements.txt` 里只有三样东西：
```text
paramiko>=3.0      # traffic_monitor.py / news_reader.py（SSH 连接）
tzdata             # TimeClock.py（Windows 不带 IANA 时区库）
chinesecalendar    # TimeClock.py 可选（中国节假日，缺了只是不上色）
```
> 💡 只跑时钟的话其实只需要 `tzdata`；只跑另外两个的话只需要 `paramiko`。
---
2. 配置
`traffic_monitor.py` 和 `news_reader.py` 顶部的「配置区」里，所有 `YOUR_` 开头的值都是占位符，
必须换成你自己的，否则程序启动时会弹窗提示「配置还没改」并退出。
占位符	该填什么	例子
`YOUR_SERVER_IP`	你的 VPS 公网 IP	`203.0.113.10`
`YOUR_SSH_PORT`	SSH 端口，改成数字时去掉引号	`"YOUR_SSH_PORT"` → `22`
`YOUR_SSH_USER`	登录用户名	`root` / `ubuntu`
`KEY_NAME`	私钥文件名	`id_ed25519`
`KEY_FILE_HINT`	私钥路径，默认 `~/.ssh/id_ed25519`，一般不用改	—
`NEWS_PATH`	新闻窗要读的远程文件路径（仅 `news_reader.py`）	`/home/me/daily_news.txt`
改完之后大概长这样：
```python
HOST = "203.0.113.10"
PORT = 22
USER = "ubuntu"
KEY_NAME = "id_ed25519"
```
---
3. 私钥放哪里
不要把私钥写进代码，也不要打包进 exe。
程序会按下面的顺序查找，命中第一个就用：
顺序	位置	适用场景
1	exe（或脚本）同级目录下的 `KEY_NAME`	想做成绿色便携版
2	同级目录下的 `key/` 子目录	想稍微藏一下
3	`~/.ssh/KEY_NAME`	推荐，也就是 `C:\Users\<你>\.ssh\id_ed25519`
4	配置区 `KEY_FILE_HINT` 指定的路径	兜底
所以最省事的做法：把私钥放 `%USERPROFILE%\.ssh\`，代码一行都不用改。
> 💡 打包成 exe 后第 3 条依然有效 —— 它用的是 `os.path.expanduser("~")`，跟着**当前用户**走，换电脑也不用改。
---
4. 打包成 exe
> **推荐用 `--onedir`（文件夹模式），不要用 `--onefile`（单文件）。**
4.1 为什么选文件夹模式
	`--onedir`（推荐）	`--onefile`
产物形态	一个文件夹：`XXX.exe` + `_internal/`	单个 `XXX.exe`
启动速度	快，直接加载	每次启动先解压到临时目录，慢 1~3 秒
杀软误报	少	多（见下）
分发方式	整个文件夹压 zip	单个文件
单文件为什么容易被杀软拦？
`--onefile` 的产物本质是"自解压包"：每次双击，程序会先把自己解压成几十个碎文件到 `%TEMP%`，
运行完再删掉。这种"悄悄在临时目录释放可执行文件"的行为，和很多木马的作案手法高度一致，
所以 Windows Defender / SmartScreen 很容易弹「已保护你的电脑」。
文件夹模式没有这个解压动作，干净得多。
> 单文件也不是不能用，手动点「更多信息 → 仍要运行」或把 exe 加进白名单即可 —— 只是对**分发给别人**不友好。
4.2 打包命令
先在 PowerShell 里清掉上次的构建残留：
```powershell
Remove-Item -Recurse -Force build, dist, *.spec -ErrorAction SilentlyContinue
```
然后逐个打包：
```powershell
# 1. 双时区时钟（必须带上时区库和中国节假日库，否则日期不会上色）
pyinstaller --noconsole --onedir --collect-data tzdata --collect-all chinesecalendar --name TimeClock TimeClock.py

# 2. 流量监控
pyinstaller --noconsole --onedir --name TrafficMonitor traffic_monitor.py

# 3. 新闻阅读器
pyinstaller --noconsole --onedir --name NewsReader news_reader.py
```
4.3 产物结构
text
dist/
├── TimeClock/
│   ├── TimeClock.exe
│   └── _internal/            ← 依赖全在这里，千万别删
├── TrafficMonitor/
│   ├── TrafficMonitor.exe
│   └── _internal/
└── NewsReader/
    ├── NewsReader.exe
    └── _internal/
```
实测体积参考：`NewsReader.exe` 本体只有 3.7 MB，`_internal/` 里 58 个文件共约 38 MB。
4.4 怎么发给别人
整个文件夹压缩（右键文件夹 → 压缩为 zip），不能只发 exe —— 少了 `_internal/` 会直接启动失败。
对方解压后双击文件夹里的 `XXX.exe` 即可，不需要安装 Python。
如果需要 SSH，把私钥放到 exe 所在的同一个文件夹里，或者放 `~/.ssh/`（见第 3 节）。
首次运行可能被 SmartScreen 提示，点「更多信息 → 仍要运行」。
```
---
5. 全局快捷键
三个程序同时开也不会冲突，因为用的是不同的键（基于 `RegisterHotKey` 系统级注册，同一个键只能被一个程序占用）：
程序	沉底 / 置顶
`TimeClock.py`	`Ctrl + Shift + 1`
`traffic_monitor.py`	`Ctrl + Shift + 2`
`news_reader.py`	`Ctrl + Shift + 3`
窗口沉底后没有任务栏图标，只能靠这个热键找回来。
> 如果提示「注册失败，多半被其它程序占用」，可以改配置区里的 `HOTKEY` 换按键即可。
---
6. 常见问题
现象	原因 / 解决
双击 exe 弹出「已保护你的电脑」	用文件夹模式打包（见 4.1），或点「更多信息 → 仍要运行」
双击后什么都没发生	大概率弹了「配置还没改」或「找不到私钥」的窗，仔细看一下；也可能被任务栏遮住
启动就退出，窗口一闪而过	配置区还是 `YOUR_` 占位符，改成真实值再跑
热点/换网后连不上	确认 `HOST` 是公网 IP、云厂商安全组放行了 SSH 端口
时钟日期只有灰色，节假日不上色	没装 `chinesecalendar`；该库目前只覆盖 2004–2026 年，超出会退化成"只看周末"（期待后续更新吧。）
时钟报 `ZoneInfoNotFoundError`	没装 `tzdata`（Windows 不带 IANA 时区库）
热键没反应	键被别的软件占了，改配置区的 `HOTKEY`

新闻窗一直显示"正在连接服务器…"	SSH 连不上/私钥不对；看窗口右上角的状态点和页脚提示
打包后字体变丑	目标机器缺 `宋体`，程序会自动回退到 `Georgia` / `Times`
---
7. 发布前我的自检
推代码之前，跑一下确认没把真实信息带出去：
```bash
# Linux / macOS / Git Bash
grep -rnE "[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}|BEGIN .*PRIVATE KEY" . --include="*.py"
```
```powershell
# Windows PowerShell
Get-ChildItem -Recurse -Filter *.py | Select-String `
  -Pattern '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}','[\w.+-]+@[\w.-]+\.[a-z]{2,}','BEGIN .*PRIVATE KEY'
```
没有任何输出才安全。再确认私钥没被加进暂存区：
```bash
git status --short
git ls-files | grep -Ei 'id_rsa|id_ed25519|\.pem$|\.key$|\.ppk$'   # 应该没输出
```
> ⚠️ 一旦某次 commit 里带上了密钥，**即使后来删掉，它仍然留在 git 历史里**。
> 那种情况必须先去作废（rotate）那把密钥，再清理历史（`git filter-repo`）。
> 另外 commit 的 **author 邮箱**也是公开可见的，建议在 `git config user.email` 里用 GitHub 的 noreply 地址。
---
8. 说明
单文件、零外部配置：配置写在各文件顶部的常量里，复制走一个 `.py` 就能跑。
SSH 长连接：`paramiko.Transport` 只在启动时握手认证一次，之后每次刷新复用同一条连接，
不会因为反复登录触发服务器上的 Fail2ban。
超时与退避：SSH 建连 10s / 命令执行 20s，连续失败会指数退避（最长 15 分钟一次），不会疯狂重连。
UI 线程安全：所有网络操作都在后台线程，通过 `queue` 把结果交回主线程渲染，
界面不会卡死；tkinter 控件只在主线程被修改。
已验证环境
Windows 11 + Python 3.14
Ubuntu 24.04 服务器（OpenSSH 9.x）
字体：自动探测已安装的衬线字体（`宋体` → `SimSun` → `Georgia` → `Times`）
