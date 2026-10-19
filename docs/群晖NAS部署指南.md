# 群晖 NAS（Docker）部署指南

> 适用环境：Synology DSM 7.2（Container Manager 套件）
> 全程图形界面操作，约 15 分钟。程序只主动访问外网，**不开放任何端口**，NAS 安全性不受影响。

---

## 目录

1. [部署前检查](#1-部署前检查)
2. [获取项目文件](#2-获取项目文件)
3. [放置配置文件](#3-放置配置文件)
4. [启动容器（方式一：Container Manager 项目，推荐）](#4-启动容器方式一container-manager-项目推荐)
5. [启动容器（方式二：SSH 命令行）](#5-启动容器方式二ssh-命令行)
6. [日常运维](#6-日常运维)
7. [NAS 专属注意事项](#7-nas-专属注意事项)
8. [常见问题 FAQ](#8-常见问题-faq)

---

## 1. 部署前检查

| 检查项 | 操作 | 要求 |
| --- | --- | --- |
| Container Manager 套件 | 套件中心搜索 "Container Manager" 安装 | DSM 7.2 自带套件中心可装 |
| NAS 架构 | 控制面板 → 信息 → 处理器 | Intel/AMD（x86）或 ARM 均可，镜像自动适配 |
| 网络 | 能访问外网 | 容器需要访问 `zpmall-api.zhongpiaofuli.com` |

> 旧版 DSM（7.0 以下）套件叫 "Docker"，操作类似；本指南以 Container Manager 为准。

## 2. 获取项目文件

**方式 A：从 GitHub 下载（推荐，与仓库同步更新）**

1. 浏览器打开项目仓库页面 → 绿框 **Code** → **Download ZIP**；
2. 解压，得到 `zpmon` 文件夹（里面应有 `Dockerfile`、`docker-compose.yml`、`config.example.json` 等）。

**方式 B：本地上传**

用 File Station 把 `zpmon` 整个文件夹上传到 NAS，例如 `/docker/zpmon`。

## 3. 放置配置文件

1. File Station 进入 `/docker/zpmon`，**新建文件夹 `data`**；
2. 把 `config.example.json` 复制进 `data/` 并**重命名为 `config.json`**；
3. 右键 `config.json` → 用文本编辑器打开，至少填一项提醒渠道（见 [快速上手指南 · 第二步](../快速上手指南.md#第二步设置消息提醒约5分钟)），例如 Bark：

```json
"bark": {
  "url": "https://api.day.app",
  "key": "你的BarkKey"
}
```

4. 保存。此时目录结构应为：

```text
/docker/zpmon/
├── Dockerfile
├── docker-compose.yml
├── zp_api.py / monitor.py / ...
└── data/
    └── config.json        ← 你的配置（含 token，勿外传）
```

> `data/` 里还会自动生成 `monitor_state.json`（限购/校准状态）和 `monitor.log`（日志），
> **升级程序时不要删这个文件夹**。

## 4. 启动容器（方式一：Container Manager 项目，推荐）

1. 打开 **Container Manager** → 左侧 **项目** → **新增**；
2. 来源选**本地**，路径填 `/docker/zpmon`（即放 `docker-compose.yml` 的目录）；
3. 项目名称随意（如 `zpmon`），勾选**构建并启动**（Build and start）；
4. 等待构建完成（首次约 1~3 分钟，取决于 NAS 性能），列表出现 `zpmon` 即成功；
5. 点项目下的容器 `zpmon` → **日志**，应看到：

```text
监控启动 cityCode=440300 interval=10.0s targets=5 autoOrder=off
[sku:12325240480893951] 缺货/补货中
...
```

容器已设置为**开机自启 + 崩溃自动重启**，NAS 重启也不用管。

## 5. 启动容器（方式二：SSH 命令行）

控制面板 → 终端机和 SNMP → 开启 SSH，然后：

```bash
ssh 你的NAS用户名@NAS的IP
cd /volume1/docker/zpmon          # 路径按实际调整
sudo docker compose up -d --build
```

后续操作：

```bash
sudo docker compose logs -f       # 看实时日志（Ctrl+C 退出查看，不影响运行）
sudo docker compose restart       # 改完配置后重启
sudo docker compose down          # 停止
```

## 6. 日常运维

| 需求 | 操作 |
| --- | --- |
| **看日志** | Container Manager → 项目 → zpmon → 容器 → 日志；或 File Station 直接看 `data/monitor.log` |
| **改配置** | File Station 编辑 `data/config.json` → Container Manager 里**重启** zpmon 容器 |
| **改监控商品** | 同上，编辑 `data/config.json` 的 `targets`（查商品 ID：见下方命令） |
| **开关自动下单** | 编辑 `config.json` 的 `autoOrder.enabled` / `dryRun`，重启容器；或临时用环境变量 |
| **查商品 ID** | 容器 → 终端 → 新终端：`python /app/find_product.py 京东` |
| **立即校准商品 ID** | `python /app/monitor.py --calibrate-now` |
| **手动买中了** | `python /app/monitor.py --mark-purchased <skuId>` |
| **恢复当天监控** | `python /app/monitor.py --reset-purchased` |

> 终端命令在 Container Manager → 项目 → zpmon → 容器 → **终端** → **新终端** 里执行
> （创建终端选 `/bin/sh` 或 `/bin/bash` 均可）。

**升级程序版本**：下载新版 `zpmon` 文件夹 → 覆盖 NAS 上旧文件（**保留 `data/` 不动**）→
Container Manager → 项目 → zpmon → **操作 → 重新构建**。

## 7. NAS 专属注意事项

- **不要加端口映射**：程序只出网访问商城 API，容器无需对外监听。compose 里已刻意不配 `ports`，保持默认最安全。
- **时区**：compose 已固定 `TZ=Asia/Shanghai`，勿改——"每日限购"按容器本地日期判定。
- **NAS 休眠**：休眠期间不轮询，唤醒后自动继续。若商品在休眠时上架又下架，可能错过——介意的话在 控制面板 → 硬件和电源 → 硬盘休眠 里把时间调长或关闭。
- **资源占用**：约 50~80MB 内存（已限制 256MB 上限），CPU 可忽略；对任何机型都无压力。
- **架构适配**：镜像基于 `python:3.12-slim`，自动拉取 amd64/arm64 版本。若拉取镜像报 `no matching manifest`（极少数 ARMv7 老机型），告诉我 NAS 型号，我来出适配方案。
- **config.json 含账号 token**：等同账号权限，勿分享、勿提交 git。

## 8. 常见问题 FAQ

**Q1：构建时卡住/失败？**
 Container Manager → 注册表，确认能拉取基础镜像；或项目 → 操作 → 重新构建。失败时看构建日志最后几行。

**Q2：日志显示"API错误"？**
 偶发正常（网络抖动）。持续报错且校准也失败，可能是商城接口升级，见《快速上手指南》Q11。

**Q3：怎么确认监控真的在跑？**
 看日志每 ~10 秒新增一行 `[sku:...] 缺货/补货中` 即正常；或 File Station 看 `data/monitor.log` 时间戳在更新。

**Q4：换电脑/重装后状态会丢吗？**
 不会。限购记录、校准记录都在 `data/monitor_state.json`，跟着文件夹走。

**Q5：想监控的商品搜不到？**
 商品可能正在下架。持续监控即可，重新上架会提醒；也可以用 `find_product.py` 换关键词或城市确认。

**Q6：Docker 版和直接跑 Python 版有什么区别？**
 功能完全一致。Docker 版免去装 Python 环境、适合 NAS 7×24；直接跑适合临时测试。

---

*文档版本：v1.0（2026-10-19） · 配套：`zpmon/Dockerfile`、`zpmon/docker-compose.yml`*
