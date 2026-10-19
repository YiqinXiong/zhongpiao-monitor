"""补货监控主程序。

用法：
    python monitor.py [--config config.json]
    python monitor.py --calibrate-now               # 立即执行一次商品ID校准并退出
    python monitor.py --mark-purchased 14598      # 手动标记某 sku 今日已购（当天不再监控/购买）
    python monitor.py --reset-purchased           # 清除全部已购标记（测试用）

监控逻辑：
- sku 模式（推荐）：轮询 app_get_h5product_sku_check，以 usableStocks > 0
  判定有货。不受商品上下架/城市影响（库存数据全局一致），最轻量稳健。
- search 模式：轮询 app_get_h5product_list，对搜索结果按 nameFilter 过滤，
  以 product.usableStocks > 0 判定有货（null 视为未知，不告警）。
- detail 模式：轮询 app_get_h5product_detail，以 remindMessageTip 为空且
  skuList 中存在 usableStocks > 0 判定有货；商品下架时接口返回
  code=1000「该商品暂时无法购买」，视为无货并持续监控。
- 每个 target 可单独配置 cityCode（可选，仅影响列表/详情查询上下文；
  库存监控与城市无关）。
- ID 自动校准：商品经常上架/下架，重新上架后可能换新 skuId/productId。
  对配置了 "calibrate": true 的目标，启动时、每 calibrateIntervalHours
  （默认24h）、以及接口报错时，自动用 searchKey 搜索并按商品名匹配更新 ID，
  校准结果存 monitor_state.json（重启不丢），ID 变化时推送通知。
- 检测到缺货 -> 有货的跳变时触发全部提醒渠道；可选持续重复提醒。
- autoOrder.enabled=true 时触发自动下单（默认 dryRun）；只购买
  autoOrder.targets 或 matchName 指定的商品。
- 每日限购：每个 sku 每天限量 1 张。下单成功（或订单创建待支付）后，
  该 sku 当天自动停止监控与购买，次日自动恢复。手动买中后可用
  --mark-purchased <skuId> 让监控当天安静。

环境变量（便于不编辑文件切换，改后需重启进程）：
    ZPMON_AUTO_ORDER=1     开启自动下单（等同 enabled=true）
    ZPMON_AUTO_ORDER=0     关闭自动下单
    ZPMON_DRY_RUN=1        强制演练模式（不提交订单）
    ZPMON_DRY_RUN=0        关闭演练模式（真实下单，慎用）
"""
import argparse
import json
import os
import random
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from notify import Notifier
from zp_api import ApiError, MallApi

STATE_FILE = Path("monitor_state.json")
LOG_FILE = Path("monitor.log")
MALL_URL = "https://zpmall.zhongpiaofuli.com"

_stop = False


def _log(msg: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def purchased_today(state: dict, sku_id) -> bool:
    """该 sku 今天是否已购买（每品种每日限量 1 张）。"""
    return bool(sku_id) and state.get("purchased", {}).get(str(sku_id)) == _today()


def mark_purchased(state: dict, sku_id) -> None:
    if not sku_id:
        return
    state.setdefault("purchased", {})[str(sku_id)] = _today()
    save_state(state)
    _log(f"[限购] sku={sku_id} 今日已购买，当天停止监控/购买该品种（次日自动恢复）")


def check_search_target(api: MallApi, target: dict) -> list[dict]:
    """返回有货的商品列表（可能多个 SKU/面值）。"""
    data = api.product_list(
        search_key=target["searchKey"],
        page_num=target.get("pageNum", 1),
        brand_id=target.get("brandId", ""),
        city_code=target.get("cityCode"),
    )
    pattern = target.get("nameFilter")
    in_stock = []
    for p in (data.get("data", {}).get("productList") or []):
        name = p.get("productName", "")
        if pattern and not re.search(pattern, name):
            continue
        stocks = p.get("usableStocks")
        if stocks is not None and stocks > 0:
            in_stock.append(p)
    return in_stock


def check_detail_target(api: MallApi, target: dict) -> list[dict]:
    try:
        data = api.product_detail(target["productId"], city_code=target.get("cityCode"))
    except ApiError as e:
        # 商品下架时详情接口返回 code=1000「该商品暂时无法购买」：视为无货并持续监控，
        # 重新上架后接口即恢复正常。其余错误（网络/限流等）继续抛出走告警日志。
        if "1000" in str(e) or "暂时无法购买" in str(e):
            _log(f"[detail:{target['productId']}] 商品暂时无法购买（下架中），继续监控")
            return []
        raise
    product = (data.get("data") or {}).get("product") or {}
    remind = (product.get("remindMessageTip") or "").strip()
    in_stock = []
    if not remind:
        for sku in product.get("skuList") or []:
            stocks = sku.get("usableStocks")
            if stocks is None or stocks > 0:
                in_stock.append({
                    "productId": product.get("productId"),
                    "productName": product.get("productName"),
                    "skuId": sku.get("skuId"),
                    "usableStocks": stocks,
                    "salePrice": sku.get("salePrice"),
                })
    return in_stock


def check_sku_target(api: MallApi, target: dict) -> list[dict]:
    """sku 模式：直接轮询 app_get_h5product_sku_check。
    不受商品上下架/城市上架影响，是最轻量的库存探针。"""
    data = api.sku_check(target["skuId"])
    sku = data.get("data") or {}
    stocks = sku.get("usableStocks")
    if stocks is not None and stocks > 0:
        return [{
            "productId": sku.get("productId"),
            "productName": sku.get("skuName") or target.get("name", ""),
            "skuId": sku.get("skuId"),
            "usableStocks": stocks,
            "salePrice": sku.get("salePrice"),
        }]
    return []


def fmt_items(items: list[dict]) -> str:
    lines = []
    for p in items:
        price = p.get("skuMinSalePrice") or p.get("salePrice")
        pid = p.get("productId")
        link = f"{MALL_URL}/mall/cake/dg_detail?id={pid}" if pid else ""
        lines.append(
            f"- {p.get('productName')} | sku={p.get('skuId')} "
            f"| 库存={p.get('usableStocks')} | 售价={price}"
            + (f"\n  {link}" if link else "")
        )
    return "\n".join(lines)


def _env_bool(name: str):
    v = os.environ.get(name)
    if v is None or v == "":
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


# ---------- 商品 ID 自动校准 ----------

def _calib_key(target: dict) -> str:
    """校准记录的稳定键：用商品名（skuId 会随上架变化，名字不会）。"""
    return target.get("name") or target.get("matchName") or \
        target.get("skuId") or target.get("productId") or target.get("searchKey") or ""


def _best_match(products: list[dict], name: str, pattern: str = ""):
    """在搜索结果中找目标商品：精确名 > 正则 > 名称包含。"""
    if not products:
        return None
    for p in products:
        if name and p.get("productName") == name:
            return p
    if pattern:
        for p in products:
            if re.search(pattern, p.get("productName", "")):
                return p
    if name:
        for p in products:
            if name in (p.get("productName") or ""):
                return p
    return None


def calibrate_targets(api: MallApi, config: dict, state: dict,
                      notifier=None, only_key: str = "") -> list:
    """按关键词搜索校准各目标的 skuId/productId。

    商品经常上架/下架，重新上架后可能分配新 ID；对配置了 calibrate 的目标，
    定期用 searchKey 搜索、按商品名匹配，发现 ID 变化就更新（内存 + state）。
    校准记录保存在 monitor_state.json，重启不丢失。
    """
    calibrated = state.setdefault("calibrated", {})
    changed = []
    for target in config["targets"]:
        if not target.get("calibrate"):
            continue
        key = _calib_key(target)
        if only_key and key != only_key:
            continue
        search_key = target.get("searchKey") or target.get("name") or ""
        if not search_key:
            continue
        try:
            data = api.product_list(search_key, city_code=target.get("cityCode"))
            products = (data.get("data") or {}).get("productList") or []
        except Exception as e:
            _log(f"[校准] {key} 搜索失败: {e}（保留原 ID 继续监控）")
            continue
        match = _best_match(products, target.get("name") or "",
                            target.get("matchName") or "")
        rec = calibrated.setdefault(key, {})
        rec["at"] = time.time()
        if not match:
            rec["found"] = False
            _log(f"[校准] {key}: 搜索未找到（可能下架中），继续用原 ID 监控")
            continue
        rec["found"] = True
        field = "skuId" if target["mode"] == "sku" else "productId"
        new_id = match.get(field)
        old_id = target.get(field)
        rec["id"] = new_id
        if new_id and new_id != old_id:
            # 今日已购标记随品种迁移（同一天买过旧 ID = 该品种今天已买过）
            if old_id and purchased_today(state, old_id):
                mark_purchased(state, new_id)
            target[field] = new_id
            rec["oldId"] = old_id
            changed.append((key, old_id, new_id))
            _log(f"[校准] {key}: {field} 已更新 {old_id} -> {new_id}")
    save_state(state)
    if changed and notifier:
        notifier.send(
            "监控目标已自动校准",
            "商品重新上架后 ID 变化，已自动更新：\n" +
            "\n".join(f"- {n}: {o} → {x}" for n, o, x in changed))
    return changed


def apply_calibrated_ids(config: dict, state: dict) -> None:
    """启动时应用已校准的 ID（重启后用最新已知 ID 监控）。"""
    calibrated = state.get("calibrated") or {}
    for target in config["targets"]:
        if not target.get("calibrate"):
            continue
        rec = calibrated.get(_calib_key(target)) or {}
        if rec.get("id"):
            field = "skuId" if target["mode"] == "sku" else "productId"
            if target.get(field) != rec["id"]:
                _log(f"[校准] 启动恢复 {_calib_key(target)}: "
                     f"{field}={target.get(field)} -> {rec['id']}")
                target[field] = rec["id"]


def run(config: dict):
    global _stop
    interval = float(config.get("pollIntervalSec", 10))
    jitter = float(config.get("jitter", 0.2))
    city = int(config.get("cityCode", 110100))
    repeat_min = float(config.get("realertMinutes", 0))  # 0 = 有货期间不重复提醒

    api = MallApi(api_token=config.get("apiToken", ""), city_code=city)
    notifier = Notifier(config.get("notify"))
    state = load_state()
    state.setdefault("alerted", {})     # key -> {"ts":..., "lastRepeat":...}
    state.setdefault("lastStatus", {})  # key -> bool
    state.setdefault("purchased", {})   # skuId -> "YYYY-MM-DD"（每日限购 1 张）
    state.setdefault("calibrated", {})  # 商品名 -> {"id":..., "at":...}（ID 自动校准）

    # 启动时应用已校准的 ID，并立即校准一次（商品可能已换新 ID）
    apply_calibrated_ids(config, state)
    calibrate_interval = float(config.get("calibrateIntervalHours", 24)) * 3600
    last_calibrate = time.time()
    calibrate_targets(api, config, state, notifier)

    # 自动下单开关：config < 环境变量
    auto_cfg = dict(config.get("autoOrder") or {})
    env_auto = _env_bool("ZPMON_AUTO_ORDER")
    env_dry = _env_bool("ZPMON_DRY_RUN")
    if env_auto is not None:
        auto_cfg["enabled"] = env_auto
    if env_dry is not None:
        auto_cfg["dryRun"] = env_dry
    auto_enabled = bool(auto_cfg.get("enabled"))
    auto = None
    if auto_enabled:
        from auto_order import AutoOrder  # 延迟导入
        auto = AutoOrder(api, auto_cfg, notifier=notifier)
        mode = "演练(不扣款)" if auto_cfg.get("dryRun", True) else "真实下单"
        if auto.targets:
            targets_txt = ",".join(str(t.get("skuId")) for t in auto.targets)
        elif auto.match_name:
            targets_txt = f"名称匹配/{auto.match_name}"
        else:
            targets_txt = "未配置!"
        _log(f"自动下单已开启（{mode}）目标: {targets_txt}")
        if not auto.targets and not auto.match_name:
            _log("警告: autoOrder 未配置 targets/matchName，将只提醒不会自动购买")

    _log(f"监控启动 cityCode={city} interval={interval}s "
         f"targets={len(config['targets'])} autoOrder={'on' if auto_enabled else 'off'}")

    while not _stop:
        # 每轮重读状态文件：支持运行中用 --mark-purchased / --reset-purchased 生效
        state = load_state()
        state.setdefault("alerted", {})
        state.setdefault("lastStatus", {})
        state.setdefault("purchased", {})
        for target in config["targets"]:
            if _stop:
                break
            key = f"{target['mode']}:{target.get('productId') or target.get('skuId') or target.get('searchKey')}"
            # 每日限购：目标 sku 今天已买到 -> 当天跳过（日志每天只打一次）
            target_sku = target.get("skuId")
            if target_sku and purchased_today(state, target_sku):
                if state.get("skipLogged", {}).get(key) != _today():
                    state.setdefault("skipLogged", {})[key] = _today()
                    save_state(state)
                    _log(f"[{key}] sku={target_sku} 今日已购买，当天跳过监控（次日自动恢复）")
                continue
            try:
                if target["mode"] == "search":
                    items = check_search_target(api, target)
                elif target["mode"] == "sku":
                    items = check_sku_target(api, target)
                else:
                    items = check_detail_target(api, target)
                # 过滤掉今天已购买的 sku（search 模式可能命中多个品种）
                fresh = [p for p in items if not purchased_today(state, p.get("skuId"))]
                if len(fresh) != len(items):
                    _log(f"[{key}] 已过滤今日已购 sku: "
                         f"{[p.get('skuId') for p in items if p not in fresh]}")
                items = fresh
                has_stock = bool(items)
                prev = state["lastStatus"].get(key)
                state["lastStatus"][key] = has_stock

                if has_stock:
                    now = time.time()
                    alerted = state["alerted"].get(key)
                    first_alert = prev is False or alerted is None
                    repeat_due = (repeat_min > 0 and alerted is not None
                                  and now - alerted.get("lastRepeat", 0) >= repeat_min * 60)
                    if first_alert or repeat_due:
                        title = f"补货提醒：{items[0].get('productName')}"
                        content = (f"检测到有货（{datetime.now():%H:%M:%S}）\n"
                                   f"{fmt_items(items)}\n\n请尽快前往商城下单。")
                        notifier.send(title, content)
                        if alerted is None:
                            state["alerted"][key] = {"ts": now, "lastRepeat": now}
                        else:
                            alerted["lastRepeat"] = now
                        save_state(state)
                        if auto is not None:
                            for item in items:
                                if auto.matches(item.get("skuId"), item.get("productName")):
                                    res = auto.attempt(sku_id=item.get("skuId"),
                                                       product_name=item.get("productName"))
                                    # 订单已创建（成功或待支付）-> 计入每日限购
                                    if res and res.get("orderCreated"):
                                        mark_purchased(state, item.get("skuId"))
                    _log(f"[{key}] 有货 {fmt_items(items)}")
                else:
                    _log(f"[{key}] 缺货/补货中")

            except ApiError as e:
                _log(f"[{key}] API错误: {e}")
                # ID 可能已失效（商品重新上架换了新 ID）：配置了校准就立即重搜
                if target.get("calibrate"):
                    calibrate_targets(api, config, state, notifier,
                                      only_key=_calib_key(target))
                    last_calibrate = time.time()
            except Exception as e:
                _log(f"[{key}] 异常: {e}")
            time.sleep(random.uniform(0.5, 1.5))  # 目标间错峰

        if _stop:
            break
        # 定期校准商品 ID（默认每 24 小时一次）
        if time.time() - last_calibrate >= calibrate_interval:
            calibrate_targets(api, config, state, notifier)
            last_calibrate = time.time()
        _stop_wait(interval * (1 + random.uniform(-jitter, jitter)))

    _log("监控已停止")


def _stop_wait(seconds: float):
    """可被信号打断的 sleep。"""
    end = time.time() + max(0.0, seconds)
    while not _stop and time.time() < end:
        time.sleep(min(0.5, max(0.0, end - time.time())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--mark-purchased", metavar="SKU_ID",
                        help="标记某 sku 今日已购买（当天不再监控/购买，次日自动恢复）")
    parser.add_argument("--reset-purchased", action="store_true",
                        help="清除全部已购标记（恢复当天监控，测试用）")
    parser.add_argument("--calibrate-now", action="store_true",
                        help="立即执行一次商品 ID 校准并退出")
    args = parser.parse_args()

    # 一次性维护命令（不需要启动监控）
    if args.mark_purchased:
        state = load_state()
        mark_purchased(state, args.mark_purchased)
        return
    if args.reset_purchased:
        state = load_state()
        if state.get("purchased"):
            state["purchased"] = {}
            save_state(state)
            _log("已清除全部今日已购标记，监控品种当天恢复")
        else:
            _log("当前没有已购标记")
        return

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"配置文件不存在: {cfg_path}（可复制 config.example.json 修改）")
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not config.get("targets"):
        sys.exit("config.json 中 targets 为空")

    if args.calibrate_now:
        api = MallApi(api_token=config.get("apiToken", ""),
                      city_code=int(config.get("cityCode", 110100)))
        state = load_state()
        state.setdefault("calibrated", {})
        state.setdefault("purchased", {})
        apply_calibrated_ids(config, state)
        changed = calibrate_targets(api, config, state,
                                    Notifier(config.get("notify")))
        _log(f"校准完成：更新 {len(changed)} 个目标，其余未变化"
             if changed else "校准完成：所有目标 ID 无变化")
        return

    def handle_sig(*_):
        global _stop
        _stop = True
        _log("收到停止信号，正在退出...")

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    run(config)


if __name__ == "__main__":
    main()
