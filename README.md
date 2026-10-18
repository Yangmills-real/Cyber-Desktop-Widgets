# VPS 桌面小工具集（含一个无需vps的美观桌面时钟）

三个基于 Python + tkinter 的 Windows 桌面悬浮窗，配合一台 Linux VPS 使用。
都是**无边框、始终置顶、可拖拽**的极简小窗，并且都支持 **一键沉底 + 全局热键唤醒**。

| 文件 | 功能 | 需要 VPS | 主要依赖 |
|---|---|---|---|
| `TimeClock.py` | 双时区时钟：大字北京时间 + 小字美西时间，按中/美节假日给日期上色，带番茄钟休息提醒 | ❌ 不需要 | `tzdata`、`chinesecalendar`（可选） |
| `traffic_monitor.py` | 远程流量监控：SSH 读服务器 `/proc/net/dev`，显示上下行总量与连接状态 | ✅ 需要 | `paramiko` |
| `news_reader.py` | 报纸风财经新闻阅读窗：SSH 读服务器上的文本文件，每 5 分钟刷新 | ✅ 需要 | `paramiko` |

---

## 快速开始

```bash
pip install -r requirements.txt
python TimeClock.py
```

`requirements.txt` 里只有三样东西：

```
paramiko>=3.0      # traffic_monitor.py / news_reader.py（SSH 连接）
tzdata             # TimeClock.py（Windows 不带 IANA 时区库）
chinesecalendar    # TimeClock.py 可选（中国节假日，缺了只是不上色）
```

> 只跑时钟的话，其实只需要 `tzdata`（`chinesecalendar` 是可选增强）。
> 只跑另外两个的话，只需要 `paramiko`。

---

## 一、先把配置区改掉

`traffic_monitor.py` 和 `news_reader.py` 顶部的「配置区」里，所有 `YOUR_` 开头的值都是占位符，
**必须换成你自己的**，否则程序启动时会弹窗提示「配置还没改」并退出。

| 占位符 | 该填什么 | 例子 |
|---|---|---|
| `YOUR_SERVER_IP` | 你的 VPS 公网 IP | `203.0.113.10` |
| `YOUR_SSH_PORT` | SSH 端口，**填数字时去掉引号** | `"YOUR_SSH_PORT"` → `22` |
| `YOUR_SSH_USER` | 登录用户名 | `root` / `ubuntu` |
| `KEY_NAME` | 私钥文件名 | `id_ed25519` |
| `KEY_FILE_HINT` | 私钥路径，默认 `~/.ssh/id_ed25519`，一般不用改 | |
| `NEWS_PATH` | 新闻窗要读的远程文件路径（仅 `news_reader.py`） | `/home/me/daily_news.txt` |

改完之后大概长这样：

```python
HOST = "203.0.113.10"
PORT = 22
USER = "ubuntu"
KEY_NAME = "id_ed25519"
```

---

## 二、私钥放哪里（重要）

**不要把私钥写进代码，也不要打包进 exe。** 程序会按下面的顺序找，命中第一个就用：

1. 程序（或 exe）**同级目录**下的 `KEY_NAME`
2. 程序同级目录下的 `key/` 子目录
3. **`~/.ssh/KEY_NAME`** ← 推荐，也就是 `C:\Users\<你>\.ssh\id_ed25519`
4. 配置区里 `KEY_FILE_HINT` 指定的路径

所以最省事的做法：把私钥放 `%USERPROFILE%\.ssh\`，代码不用改。

> 打包成 exe 之后，第 3 条依然有效（它用的是 `os.path.expanduser("~")`，跟着当前用户走），
> 换电脑也能用。

---

## 三、打包成 exe

# 先在powershell清理一下之前可能残留的垃圾文件

Remove-Item -Recurse -Force build, dist, *.spec -ErrorAction SilentlyContinue

# 1. 打包双时区时钟（必须带上时区和节假日库）
pyinstaller --noconsole --onedir --collect-data tzdata --collect-all chinesecalendar --name TimeClock TimeClock.py

# 2. 打包流量监控
pyinstaller --noconsole --onedir --name TrafficMonitor traffic_monitor.py

# 3. 打包新闻阅读器
pyinstaller --noconsole --onedir --name NewsReader news_reader.py

#补充为什么不能用--onefile直接打包：主要是避开微软 Defender 的杀软，单文件打包的原理是，每次双击运行，它都会偷偷在系统的 Temp（临时）文件夹里把自己解压成几十个碎文件，运行完再删掉。这种“偷偷释放文件”的行为，和很多木马病毒的操作手法一模一样，所以 Windows 看到就会弹“已保护你的电脑”。（其实也可，解除一下安全权限就行）

---
## 四、全局快捷键

三个程序同时开也不会冲突，因为用的是**不同的键**（基于 `RegisterHotKey` 系统级注册，
同一个键只能被一个程序占用）：

| 程序 | 沉底 / 置顶 |
|---|---|
| `TimeClock.py` | `Ctrl + Shift + 1` |
| `traffic_monitor.py` | `Ctrl + Shift + 2` |
| `news_reader.py` | `Ctrl + Shift + 3` |

窗口沉底后没有任务栏图标，**只能靠这个热键找回来**。
如果提示「注册失败，多半被其它程序占用」，改配置区里的 `HOTKEY` 换一个即可。

---

## 五、提交前我的自检

推代码之前，跑一下这段确认没有把真实信息带出去：

```bash
# Linux / macOS / Git Bash
grep -rnE "[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}|BEGIN .*PRIVATE KEY" . --include="*.py"
```

```powershell
# Windows PowerShell
Get-ChildItem -Recurse -Filter *.py | Select-String `
  -Pattern '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}','[\w.+-]+@[\w.-]+\.[a-z]{2,}','BEGIN .*PRIVATE KEY'
```

**没有任何输出**证明安全。下面是再确认一下私钥文件没被加进去：

```bash
git status --short
git ls-files | grep -Ei 'id_rsa|id_ed25519|\.pem$|\.key$|\.ppk$'   # 应该没输出
```

> ⚠️ 给所有人的建议：一旦某次 commit 里带上了密钥，**即使后来删掉，它仍然留在 git 历史里**。
> 那种情况必须先撤销（rotate）那把密钥，再清理历史（`git filter-repo`）。

---

## 六、说明

- 这些工具都是**单文件、零外部配置**的设计，配置写在各文件顶部的常量里，方便直接改。
- SSH 部分用的是 `paramiko.Transport` 长连接：**只在启动时握手认证一次**，
  之后每次刷新复用同一条连接，不会反复登录去触发服务器上的 Fail2ban。
- 网络操作都有超时（SSH 建连 10s / 命令 20s），连续失败会指数退避，不会疯狂重连。

## 已验证环境

- Windows 11 + Python 3.14
- Ubuntu 24.04 服务器（OpenSSH 9.x）
- 字体：优先使用系统里已安装的衬线字体（自动探测 `宋体` → `SimSun` → `Georgia`）
