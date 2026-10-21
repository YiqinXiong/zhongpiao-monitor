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
- ID 自动校准：商品经常上架/下架，重新上架后可能换新 skuId/productId。
  对配置了 "calibrate": true 的目标，启动时、每 calibrateIntervalHours
  （默认24h）、以及接口报错时，自动用 searchKey 搜索并按商品名匹配更新 ID，
  校准结果存 monitor_state.json（重启不丢），ID 变化时推送通知。

限流与配额（重要）：
商城对同一出口 IP 有「每日访问次数」配额，超出会返回
「IP[x.x.x.x]今日访问频繁」。本程序因此：
- 串行调度：每轮最多发 1 个请求，按各目标的 minIntervalSec 均匀轮转，
  绝不像早期版本那样 10 秒一轮并发查 5 个目标（那会产生 4 万+ 次/天）；
- 每日预算：dailyRequestBudget（默认 3000）内自动分配间隔，
  越接近预算间隔越长，永不超预算；
- 触发限流：立即退避（默认 30 分钟）+ 降速（间隔 ×2，上限 ×16）+ 推送通知，
  反复触发则暂停到次日 0 点。

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
from datetime import datetime, timedelta
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


# ---------- 每日限购 ----------

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


# ---------- 限流与每日预算 ----------

def _next_midnight() -> float:
    dt = datetime.now()
    tomorrow = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def _day_count(state: dict) -> int:
    stats = state.get("requestStats") or {}
    if stats.get("date") != _today():
        return 0
    return int(stats.get("count", 0))


def _bump_requests(state: dict, n: int):
    if n <= 0:
        return
    stats = state.get("requestStats") or {}
    if stats.get("date") != _today():
        stats = {"date": _today(), "count": 0}
    stats["count"] = int(stats.get("count", 0)) + n
    state["requestStats"] = stats


def _is_rate_limited(err) -> bool:
    s = str(err)
    return "访问频繁" in s or "频繁" in s or "too many" in s.lower() or "限流" in s


# ---------- 分时段调度 ----------

def _parse_hhmm(text: str, fallback: tuple = (0, 0)) -> tuple:
    try:
        h, m = str(text).split(":")
        return int(h), int(m)
    except Exception:
        return fallback


def _in_window(dt: datetime, frm: str, to: str) -> bool:
    """判断时刻是否落在时间窗内（支持跨零点窗口，如 22:00-06:00）。"""
    fh, fm = _parse_hhmm(frm)
    th, tm = _parse_hhmm(to)
    cur = dt.hour * 60 + dt.minute
    start, end = fh * 60 + fm, th * 60 + tm
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end      # 跨零点


def _schedule_interval(config: dict, dt: datetime) -> float:
    """该时刻 schedule 规定的基础间隔（秒）。"""
    sch = config.get("schedule") or {}
    for w in sch.get("windows") or []:
        if _in_window(dt, w.get("from", "00:00"), w.get("to", "00:00")):
            return float(w.get("minIntervalSec", 60))
    return float(sch.get("defaultMinIntervalSec", config.get("minIntervalSec", 120)))


def _base_interval(config: dict, target: dict, now: float) -> float:
    """目标的基础间隔：显式 minIntervalSec 优先，否则跟随当前时段的 schedule。"""
    own = target.get("minIntervalSec")
    if own:
        return float(own)
    return _schedule_interval(config, datetime.fromtimestamp(now))


def _plan_requests(config: dict, now: float) -> float:
    """按当前 schedule 估算「今天剩余时间」还会产生多少请求（用于预算校验）。"""
    dt = datetime.fromtimestamp(now)
    end = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for t in config.get("targets", []):
        own = t.get("minIntervalSec")
        if own:
            total += max(0.0, end.timestamp() - now) / float(own)
            continue
        # 跟随 schedule：按 5 分钟粒度积分
        t_cur = dt
        step = 300
        while t_cur < end:
            iv = _schedule_interval(config, t_cur)
            total += step / iv
            t_cur += timedelta(seconds=step)
    return total


def _scale(config: dict, state: dict, now: float):
    """预算缩放系数：>=1。按 schedule 的预计用量若超过剩余预算，则整体等比放慢。
    返回 None 表示今日预算已耗尽。"""
    budget = int(config.get("dailyRequestBudget", 4000))
    if budget <= 0:
        return 1.0
    left = budget - _day_count(state)
    if left <= 0:
        return None
    planned = _plan_requests(config, now)
    if planned <= left:
        return 1.0
    return planned / left


def _target_key(target: dict) -> str:
    return f"{target['mode']}:{target.get('productId') or target.get('skuId') or target.get('searchKey')}"


def _pick_due(config: dict, state: dict, now: float, scale: float):
    """挑出最该检查的目标（最过期的优先）。返回 (target, key) 或 None。"""
    last_check = state.setdefault("lastCheck", {})
    best = None
    for t in config["targets"]:
        key = _target_key(t)
        mi = _base_interval(config, t, now) * scale
        overdue = now - float(last_check.get(key, 0)) - mi
        if overdue >= 0 and (best is None or overdue > best[0]):
            best = (overdue, t, key)
    return (best[1], best[2]) if best else None


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


# ---------- 库存检查 ----------

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
    不受商品上下架/城市影响，是最轻量的库存探针。"""
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


def _log_plan(config: dict, state: dict, notifier=None):
    """启动/规划时输出 schedule 计划用量，并与每日预算对照。"""
    sch = config.get("schedule") or {}
    planned = _plan_requests(config, time.time())
    budget = int(config.get("dailyRequestBudget", 4000))
    used = _day_count(state)
    left = max(0, budget - used)
    txt = (f"[计划] 按当前 schedule 预计今日剩余还需约 {planned:.0f} 次请求，"
           f"预算剩余 {left} 次")
    if sch.get("windows"):
        for w in sch["windows"]:
            txt += f"；{w.get('from')}-{w.get('to')} → {w.get('minIntervalSec')}s/目标"
    if sch.get("defaultMinIntervalSec"):
        txt += f"；其余时段 → {sch.get('defaultMinIntervalSec')}s/目标"
    _log(txt)
    if budget > 0 and planned > left:
        msg = (f"按当前 schedule 预计今日还需约 {planned:.0f} 次请求，"
               f"超过剩余预算 {left} 次。\n"
               f"程序会把所有间隔等比放慢约 {planned / max(1, left):.1f} 倍以保住配额。\n"
               f"建议：把 dailyRequestBudget 提到 {int(planned * 1.15)}，"
               f"或调大 schedule 的间隔（减少监控目标）。")
        _log(f"[计划] ⚠️ {msg}")
        if notifier and not state.get("planWarned"):
            state["planWarned"] = _today()
            save_state(state)
            notifier.send("监控计划用量超过每日预算", msg)
    return planned


def run(config: dict):
    global _stop
    city = int(config.get("cityCode", 110100))
    repeat_min = float(config.get("realertMinutes", 0))  # 0 = 有货期间不重复提醒
    budget = int(config.get("dailyRequestBudget", 3000))
    base_interval = float(config.get("minIntervalSec", config.get("pollIntervalSec", 120)))
    backoff_sec = float(config.get("rateLimitBackoffMinutes", 30)) * 60

    api = MallApi(api_token=config.get("apiToken", ""), city_code=city)
    notifier = Notifier(config.get("notify"))
    state = load_state()
    state.setdefault("alerted", {})     # key -> {"ts":..., "lastRepeat":...}
    state.setdefault("lastStatus", {})  # key -> bool
    state.setdefault("purchased", {})   # skuId -> "YYYY-MM-DD"（每日限购 1 张）
    state.setdefault("calibrated", {})  # 商品名 -> {"id":..., "at":...}（ID 自动校准）
    state.setdefault("lastCheck", {})   # key -> 上次检查时间戳（限流调度用）

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
        if auto.targets:
            targets_txt = ",".join(str(t.get("skuId")) for t in auto.targets)
        elif auto.match_name:
            targets_txt = f"名称匹配/{auto.match_name}"
        else:
            targets_txt = "未配置!"
        _log(f"自动下单已开启（{'演练(不扣款)' if auto_cfg.get('dryRun', True) else '真实下单'}）"
             f"目标: {targets_txt}")
        if not auto.targets and not auto.match_name:
            _log("警告: autoOrder 未配置 targets/matchName，将只提醒不会自动购买")

    _log(f"监控启动 cityCode={city} 目标={len(config['targets'])} "
         f"每日预算={budget} autoOrder={'on' if auto_enabled else 'off'}")
    _log_plan(config, state, notifier)

    seen_requests = api.total_requests
    # 启动校准的请求也计入当日预算
    _bump_requests(state, api.total_requests)
    save_state(state)
    notified_limit = False

    while not _stop:
        # 每轮重读状态文件：支持运行中用 --mark-purchased / --reset-purchased 生效
        state = load_state()
        state.setdefault("alerted", {})
        state.setdefault("lastStatus", {})
        state.setdefault("purchased", {})
        state.setdefault("calibrated", {})
        state.setdefault("lastCheck", {})
        now = time.time()

        # 1) 冷却期（限流退避 / 预算耗尽）
        cooldown = float(state.get("cooldownUntil", 0))
        if cooldown > now:
            reason = state.get("cooldownReason", "")
            _stop_wait(min(cooldown - now, 60))
            continue

        # 2) 每日预算自适应（按 schedule 的计划用量等比缩放间隔）
        scale = _scale(config, state, now)
        if scale is None:
            until = _next_midnight()
            state["cooldownUntil"] = until
            state["cooldownReason"] = "daily_budget"
            save_state(state)
            _log(f"[限流保护] 今日请求预算({budget})已用尽，暂停至次日 00:00")
            notifier.send("监控因请求预算用尽已暂停",
                         f"今日已请求 {_day_count(state)} 次，达到预算 {budget}。\n"
                         f"已暂停到次日 00:00 自动恢复。\n"
                         f"如需更快发现补货，可提高 dailyRequestBudget（但可能触发平台限流）。")
            _stop_wait(60)
            continue

        # 3) 选一个到期的目标（每轮最多 1 个请求）
        cur_iv = _schedule_interval(config, datetime.fromtimestamp(now)) * scale
        if abs(cur_iv - state.get("lastAdaptive", 0)) > 15:
            state["lastAdaptive"] = cur_iv
            save_state(state)
            _log(f"[调度] 当前时段检查间隔 ≈ {cur_iv:.0f}s/目标"
                 f"（今日已用 {_day_count(state)}/{budget} 次请求）")
        picked = _pick_due(config, state, now, scale)
        if not picked:
            _stop_wait(5)
            continue
        target, key = picked

        # 每日限购：目标 sku 今天已买到 -> 当天跳过（日志每天只打一次）
        target_sku = target.get("skuId")
        if target_sku and purchased_today(state, target_sku):
            if state.get("skipLogged", {}).get(key) != _today():
                state.setdefault("skipLogged", {})[key] = _today()
                save_state(state)
                _log(f"[{key}] sku={target_sku} 今日已购买，当天跳过监控（次日自动恢复）")
            state["lastCheck"][key] = now
            save_state(state)
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
            if _is_rate_limited(e):
                factor = float(state.get("throttleFactor", 1))
                state["throttleFactor"] = min(factor * 2, 16)
                state["rateLimitCount"] = int(state.get("rateLimitCount", 0)) + 1
                if state["rateLimitCount"] >= 3:
                    until = _next_midnight()      # 反复触发：按日配额处理，暂停到次日
                    state["cooldownReason"] = "rate_limit_day"
                else:
                    until = now + backoff_sec
                    state["cooldownReason"] = "rate_limit"
                state["cooldownUntil"] = until
                save_state(state)
                _log(f"[限流] {e} → 退避至 "
                     f"{datetime.fromtimestamp(until):%m-%d %H:%M}"
                     f"（间隔将放慢至 {cur_iv * state['throttleFactor']:.0f}s）")
                if not notified_limit:
                    notified_limit = True
                    notifier.send(
                        "监控触发平台频率限制，已自动降速",
                        f"错误：{e}\n\n程序已自动处理：\n"
                        f"· 暂停请求至 {datetime.fromtimestamp(until):%m-%d %H:%M}\n"
                        f"· 检查间隔自动放慢（×{state['throttleFactor']}）\n"
                        f"· 今日已请求 {_day_count(state)} 次\n\n"
                        f"建议：调大 config.json 的 minIntervalSec（当前 {base_interval}s）"
                        f"或减少监控目标，避免再次触发。")
            else:
                _log(f"[{key}] API错误: {e}")
                # ID 可能已失效（商品重新上架换了新 ID）：配置了校准就立即重搜
                if target.get("calibrate"):
                    calibrate_targets(api, config, state, notifier,
                                      only_key=_calib_key(target))
                    last_calibrate = time.time()
        except Exception as e:
            _log(f"[{key}] 异常: {e}")

        # 记录本轮请求数（用于每日预算）
        state["lastCheck"][key] = time.time()
        _bump_requests(state, api.total_requests - seen_requests)
        seen_requests = api.total_requests
        save_state(state)

        # 4) 定期校准商品 ID
        if time.time() - last_calibrate >= calibrate_interval:
            calibrate_targets(api, config, state, notifier)
            last_calibrate = time.time()
            _bump_requests(state, api.total_requests - seen_requests)
            seen_requests = api.total_requests
            save_state(state)

        # 5) 轻微间隔，避免请求扎堆
        _stop_wait(random.uniform(2, 6))

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
    parser.add_argument("--stats", action="store_true",
                        help="显示今日请求用量与限流状态后退出")
    parser.add_argument("--plan", action="store_true",
                        help="按当前 schedule 估算今日请求用量并对照预算后退出")
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
    if args.stats:
        state = load_state()
        cd = float(state.get("cooldownUntil", 0))
        print(f"今日已请求: {_day_count(state)} 次")
        print(f"限流退避至: "
              f"{datetime.fromtimestamp(cd):%Y-%m-%d %H:%M}" if cd > time.time()
              else "限流退避: 无")
        print(f"降速倍数: {state.get('throttleFactor', 1)}x")
        print(f"限流触发次数: {state.get('rateLimitCount', 0)}")
        return

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"配置文件不存在: {cfg_path}（可复制 config.example.json 修改）")
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not config.get("targets"):
        sys.exit("config.json 中 targets 为空")

    if args.plan:
        state = load_state()
        sch = config.get("schedule") or {}
        now = time.time()
        dt = datetime.fromtimestamp(now)
        planned = _plan_requests(config, now)
        budget = int(config.get("dailyRequestBudget", 4000))
        used = _day_count(state)
        print(f"当前时段: {dt:%H:%M}  生效间隔 ≈ "
              f"{_schedule_interval(config, dt):.0f}s/目标")
        for w in sch.get("windows") or []:
            print(f"  时段 {w.get('from')}-{w.get('to')} → {w.get('minIntervalSec')}s/目标")
        print(f"  其余时段 → {sch.get('defaultMinIntervalSec', config.get('minIntervalSec', 120))}s/目标")
        print(f"今日已请求: {used} / 预算 {budget}（剩余 {max(0, budget - used)}）")
        print(f"按 schedule 预计今日剩余还需: {planned:.0f} 次请求")
        if budget > 0 and planned > max(0, budget - used):
            print(f"⚠️ 超出预算：间隔将被等比放慢约 {planned / max(1, budget - used):.2f} 倍")
            print(f"   建议把 dailyRequestBudget 提到 {int(planned * 1.15)}，"
                  f"或调大 schedule 间隔")
        return

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
