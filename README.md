# iPhone Duo 补货监控

监控 Apple Store 官网上 **iPhone Duo** 的门店取货和网上送货情况。一有货就推送到手机或电脑，同时带一个本地网页看板，实时显示各门店的库存。

- ✅ **门店取货监控**：可以同时监控多个型号，覆盖附近所有门店，或只看你指定的几家
- 📦 **送货时间监控**：网上订购的送货日期一变（比如从 “1 月底” 变成 “明天送达”）就提醒你
- 🔔 **9 种通知渠道**：Bark（iPhone 推送）、Server 酱 / PushPlus（微信）、Telegram、ntfy、Discord、自定义 Webhook、邮件、电脑弹窗和提示音
- 🖥️ **网页看板**：门店 × 型号的有货表格、补货动态记录，浏览器里也能弹提醒、响提示音
- 🌏 **多地区**：美国、中国大陆、香港、台湾、日本、新加坡、加拿大、英国、澳大利亚
- 🛡️ **防限流**：请求间隔随机抖动，被 Apple 限流时自动指数退避；只在状态变化时推送，不会刷屏
- 📦 **零依赖**：只需要 Python 3.11+，不用装任何第三方库

![看板截图](docs/dashboard.png)

## 快速开始

```bash
git clone https://github.com/richardB520/Iphone-DUO-restock.git
cd Iphone-DUO-restock
cp config.example.toml config.toml   # 然后编辑 config.toml
python3 -m duo_restock check         # 先检查一次，确认配置正确
python3 -m duo_restock test-notify   # 测试推送能不能收到
python3 -m duo_restock run           # 开始持续监控
```

运行后打开 <http://127.0.0.1:8765> 就能看到网页看板。

也可以先安装成命令：`pip install .`，之后直接用 `duo-restock run`。

## 如何找到型号号码（Part Number）

Apple 的库存接口按型号号码查询，每个「容量 + 颜色」组合对应一个号码，格式类似 `MXXXXLL/A`（美国）或 `MXXXXCH/A`（中国大陆）。

1. 用电脑浏览器打开 Apple 官网的 iPhone Duo 购买页面，选好容量和颜色
2. 按 `F12` 打开开发者工具，切到 **Network（网络）** 标签
3. 在页面上点 **Check availability / 查看门店是否有货**，随便输个邮编或地区
4. 在 Network 里搜索 `fulfillment-messages`，点开这个请求，URL 里 `parts.0=` 后面的值就是型号号码（`%2F` 就是 `/`）

把号码填进 `config.toml` 的 `[[products]]` 里。想监控几个组合，就写几个 `[[products]]`。

## 配置说明

完整示例和注释见 [`config.example.toml`](config.example.toml)，主要有这几项：

| 配置 | 说明 |
| --- | --- |
| `apple.region` | 地区：`us` `cn` `hk` `hk-zh` `tw` `jp` `sg` `ca` `uk` `au` |
| `apple.location` | 美国填邮编，例如 `10001`；中国大陆填「省 市 区」，例如 `上海 上海 黄浦区` |
| `apple.store` | 也可以直接填门店编号，例如 `R095`，会顺带查附近的店 |
| `apple.store_filter` | 只关心哪些门店，例如 `["R095", "R227"]` |
| `monitor.interval_seconds` | 检查间隔，默认 60 秒，最低 20 秒 |
| `monitor.remind_minutes` | 一直有货时每隔多少分钟再提醒一次，0 表示只在刚补货时提醒一次 |
| `monitor.notify_sold_out` | 卖完时是否也推送 |
| `dashboard.host` | 改成 `0.0.0.0` 后，同一 Wi-Fi 下的手机也能打开看板 |

### 推荐的通知设置

- **iPhone 用户**：用 [Bark](https://apps.apple.com/app/bark-customed-notifications/id1403753865)，把 `level` 设成 `critical`，静音模式下也会响
- **微信**：用 [Server 酱](https://sct.ftqq.com) 或 [PushPlus](https://www.pushplus.plus)
- **安卓 / 跨平台**：用 [ntfy](https://ntfy.sh)，不用注册，起一个不容易被别人猜到的 topic 名就行

推送里会带上购买链接，点开就能直接下单。

## 命令

| 命令 | 作用 |
| --- | --- |
| `python3 -m duo_restock run` | 持续监控，同时启动看板（加 `--no-dashboard` 就不启动看板） |
| `python3 -m duo_restock check` | 只检查一次，把结果打印出来，不推送 |
| `python3 -m duo_restock test-notify` | 给所有已启用的渠道发一条测试消息 |
| `python3 -m duo_restock url` | 打印实际请求的 Apple 接口地址，可以贴到浏览器里排查问题 |

所有命令都可以加 `-c 路径` 指定别的配置文件，加 `-v` 输出调试日志。

## 长期在后台运行

监控程序需要一直开着，并且要在你自己的电脑或家里的网络上运行：Apple 经常屏蔽云服务器和数据中心的 IP。

```bash
# macOS / Linux：关掉终端也继续跑
nohup python3 -m duo_restock run > monitor.log 2>&1 &

# 或者放进 tmux / screen 里
tmux new -s duo 'python3 -m duo_restock run'
```

Mac 记得在「系统设置 → 电池」里关掉自动睡眠，不然电脑睡着了就收不到提醒。

## 常见问题

**一直显示 `HTTP 541` / `403` / `429`？**
这是被 Apple 限流了。程序会自动退避，过一会儿再试。如果一直这样，就把 `interval_seconds` 调大（比如 120），或者换个网络。

**`check` 显示「附近没有返回任何门店数据」？**
通常是型号号码填错了，或者号码和 `region` 不匹配（美国号码以 `LL/A` 结尾，中国大陆以 `CH/A` 结尾）。可以用 `url` 命令打印出接口地址，贴到浏览器里看 Apple 实际返回了什么。

**第一次运行就推送了？**
这是正常的：如果启动时已经有货，会马上提醒你。之后只在状态发生变化时才推送，已经推送过的状态记在 `state.json` 里，重启也不会重复推送。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

代码结构：

```
duo_restock/
  apple.py        Apple 库存接口：拼请求地址、解析返回的 JSON
  monitor.py      轮询循环：和上次状态比较，决定要不要推送，失败时退避
  notifiers.py    各种推送渠道
  dashboard.py    本地网页看板（dashboard.html）
  config.py       读取并校验 config.toml
  cli.py          命令行入口
```

## 声明

本项目和 Apple 没有任何关系，只是读取 Apple 官网公开的库存查询接口，供个人使用。请保持合理的检查频率，不要用来大规模抢购或倒卖。Apple 的接口随时可能变化，如果哪天失效了，欢迎提 Issue。
