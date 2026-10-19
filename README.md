# 中票优品「京东E卡」补货监控方案

针对 https://zpmall.zhongpiaofuli.com 的京东E卡补货监控与提醒，含可运行原型。
代码仓库（private）：https://github.com/YiqinXiong/zhongpiao-monitor

## 文档

- **[docs/快速上手指南.md](docs/快速上手指南.md)** —— 零基础开箱即用：环境安装、提醒方式设置（Bark/PushPlus/企业微信）、商品配置、自动下单开关、服务器常驻、FAQ
- **[docs/群晖NAS部署指南.md](docs/群晖NAS部署指南.md)** —— 群晖 Container Manager 项目导入，Docker 一条命令常驻，免装 Python
- **[docs/中票优品补货监控方案.md](docs/中票优品补货监控方案.md)** —— 完整技术方案：站点逆向结论、技术选型、登录态与验证码处理、补货检测策略、自动下单流程、限制/风险/合规分析

## 原型（zpmon/）

监控查库存**无需登录**；仅自动下单需要 apiToken。

```bash
cd zpmon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # 编辑：提醒渠道、监控目标
python monitor.py                    # 开始监控
```

## 关键实测结论

- 站点为 uni-app H5，业务接口统一为 `POST https://zpmall-api.zhongpiaofuli.com/mall/MallApi/handle`，签名 = MD5(排序参数串 + 固定 appKey)，已复刻验证
- 商品三态全覆盖：在架有货 / 补货中 / 下架，重新上架即提醒；**库存监控与城市无关**（已实测）
- 默认监控 4 品种：京东E卡 100 面值 / 盒马鲜生 100 面值 / 中石化 100 元加油券 / 三网通 100 元话费
- **商品 ID 每日自动校准**：重新上架换新 skuId 也能跟上（按关键词搜索+商品名匹配），ID 变化自动通知
- 登录态 = localStorage 的 `apiToken`，可从 Edge 直接取出复用；验证码人工收一次即可
- 自动下单支持多品种（各自配置充值账号），**默认 dryRun 不扣款**；每品种每日限量 1 张，买成功当天自动停止该品种、次日恢复；成功/失败均推送通知
- 提醒渠道：Bark / Telegram / PushPlus / Server酱 / 钉钉 / 企微 / 飞书 / webhook；微信群无官方免费机器人 API，最接近方案见指南 2.1
