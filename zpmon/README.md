# 中票优品补货监控工具（zpmon）

针对 https://zpmall.zhongpiaofuli.com （中票优品 H5 商城）的补货监控 + 提醒，可选自动下单。
**监控查库存无需登录**；仅自动下单需要 apiToken。

> 📖 第一次使用请先读 **[../docs/快速上手指南.md](../docs/快速上手指南.md)**（零基础可跟随）。
> 技术细节与风险分析见 [../docs/中票优品补货监控方案.md](../docs/中票优品补货监控方案.md)。

## 快速开始

```bash
cd zpmon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json
# 编辑 config.json：城市、监控目标、提醒渠道（至少填一个）

python monitor.py            # 开始监控（Ctrl+C 停止）
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `zp_api.py` | MallApi 客户端（签名复刻 + 全部业务方法） |
| `monitor.py` | 监控主程序（轮询/状态机/提醒/下单触发） |
| `notify.py` | 提醒渠道：Bark / **ggsuper(鸿蒙华为)** / PushPlus / Server酱 / 钉钉 / 企微 / 飞书 / Telegram / 自定义 webhook |
| `auto_order.py` | 自动下单（**默认 dryRun 不扣款**，成功后自动推送通知） |
| `login_helper.py` | Playwright 登录助手（自动捕获 apiToken，需显示器） |
| `login.py` | 终端交互登录：手机号+短信验证码，自动写入 config.json（NAS/SSH 适用） |
| `find_product.py` | 商品查询工具：搜商品、查 ID/库存/售价、生成可粘贴配置 |
| `config.example.json` | 配置模板（默认深圳 + 京东E卡北京监控目标） |
| `monitor.log` / `monitor_state.json` | 运行后自动生成 |

## 三种监控模式

```json
"targets": [
  { "mode": "sku",    "skuId": "12325240480893951", "name": "【京东E卡】100面值" },
  { "mode": "search", "searchKey": "面值", "nameFilter": "100" },
  { "mode": "detail", "productId": "12325240480631807" }
]
```

- `sku`（推荐）：轮询 `app_get_h5product_sku_check`，最稳健，商品下架/补货中都能持续监控；**库存与城市无关**
- `search`：按关键词搜索，可发现新上架/新规格
- `detail`：商品详情，信息最全（下架时接口返回"暂时无法购买"，程序自动按无货处理并继续监控）

默认配置已含 4 个监控目标：京东E卡 100 面值 / 盒马鲜生 100 面值 / 中石化 100 元加油券 / 三网通 100 元话费。

## 自动下单开关

```bash
# 编辑 config.json（重启生效）
"autoOrder": { "enabled": true, "dryRun": false }   # 真实下单
"autoOrder": { "enabled": true, "dryRun": true }    # 演练：只预览不扣款（默认）
"autoOrder": { "enabled": false }                   # 完全关闭

# 或用环境变量临时切换（优先级高于配置文件，需重启进程）
ZPMON_AUTO_ORDER=0 python monitor.py    # 关闭自动下单
ZPMON_DRY_RUN=1 python monitor.py       # 强制演练
```

- `autoOrder.targets` 支持多品种，每项独立配 `rechargePhone`（充值账号）/ `quantity` / `maxWxPayAmount`；只买列出的 sku，**不会误买**其他商品
- `maxWxPayAmount`：微信补付上限，0 = 点数不足时拒绝下单（防误扣现金）
- **每品种每天限量 1 张**：下单成功当天该 sku 自动停止监控/购买，次日恢复
- 下单成功/失败/待支付/被抢空都会推送通知（含订单号）

## 每日限购手动管理

```bash
python monitor.py --mark-purchased 14598   # 手动买中后：该 sku 当天安静（运行中即时生效）
python monitor.py --reset-purchased        # 清除全部已购标记（测试用）
python monitor.py --stats                  # 今日请求用量 / 限流状态 / 降速倍数
```

## 限流保护（平台按出口 IP 有每日配额）

超配额会返回 `IP[x.x.x.x]今日访问频繁`。程序三层保护：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `schedule.windows` | 09:00–17:00 → 60s | 分时段调度：补货高发时段更频繁（支持跨零点窗口） |
| `schedule.defaultMinIntervalSec` | 180 | 其余时段的检查间隔 |
| `dailyRequestBudget` | 4000 | 每日请求预算；按计划量等比缩放间隔，永不超预算（0 = 不限制） |
| `rateLimitBackoffMinutes` | 30 | 触发限流后暂停分钟数（反复触发则暂停到次日 0 点） |

- 串行调度：每轮最多 1 个请求，按各目标间隔轮转，不扎堆
- 单目标可用 `"minIntervalSec"` 覆盖时段设置
- 触发限流：自动退避 + 间隔自动放慢（×2，上限 ×16）+ 推送通知
- 用量查询：`python monitor.py --stats`；用量规划：`python monitor.py --plan`
- 换出口 IP（手机热点）可立即解除；次日 0 点配额自动重置

## 商品 ID 自动校准

商品频繁上架/下架，重新上架可能换新 skuId。对 `"calibrate": true` 的目标：

- 启动时 / 每 `calibrateIntervalHours`（默认 24h）/ 接口报错时，按 `searchKey` 搜索并按商品名匹配更新 ID
- ID 变化自动推送「监控目标已自动校准」通知；记录存 `monitor_state.json`，重启不丢
- 搜不到时（下架中）保留原 ID 继续监控；今日已购标记随品种迁移
- 手动立即校准：`python monitor.py --calibrate-now`
- `autoOrder.targets` 按商品名匹配（`name` 字段为稳定键），监控端换 ID 不影响自动购买

## 获取 apiToken（仅自动下单需要）

```bash
# 方式一：终端交互登录（NAS/SSH/无显示器环境，推荐）
python login.py                       # 本机
docker exec -it zpmon python /app/login.py   # 容器内

# 方式二：Playwright 登录助手（需要有显示器的电脑）
pip install playwright && playwright install chromium
python login_helper.py --phone 你的手机号
```

或在 Edge 已登录商城时，F12 控制台执行 `localStorage.getItem('apiToken')`，填入 `config.json`。

> token = 账号完全权限，`chmod 600 config.json`，切勿泄露或提交 git。

## 常驻部署（Linux）

```bash
# systemd 单元示例见 ../docs/快速上手指南.md 第六步
sudo systemctl enable --now zpmon
journalctl -u zpmon -f
```

## Docker / 群晖 NAS 部署

```bash
# 1. 准备配置（config.json 放这里，token/状态/日志也都在这）
mkdir -p data && cp config.example.json data/config.json && 编辑 data/config.json

# 2. 构建并启动（后台常驻，开机/崩溃自恢复）
docker compose up -d --build

# 3. 常用维护命令
docker compose logs -f                                    # 看日志
docker exec zpmon python /app/monitor.py --calibrate-now  # 立即校准商品ID
docker exec zpmon python /app/find_product.py 京东         # 查商品ID
docker exec zpmon python /app/monitor.py --mark-purchased <skuId>
docker exec zpmon python /app/monitor.py --reset-purchased
```

镜像基于 `python:3.12-slim`（amd64/arm64 自动适配），不开放任何端口，内存限制 256MB。
群晖 Container Manager「项目」可直接导入 `docker-compose.yml`，详见 [../docs/群晖NAS部署指南.md](../docs/群晖NAS部署指南.md)。
