"""自动下单模块（默认 dryRun，仅在 autoOrder.enabled=true 且 dryRun=false 时真实扣款）。

下单流程（与 H5「立即购买」一致）：
1. app_get_h5product_sku_check        权威库存校验
2. app_set_h5product_shopcart_add     立即购买（commitFrom=1）-> shopCartId
3. app_get_h5member_ptcard_list       获取点数卡（cardNo / deducteNum）
4. app_get_h5order_preview            预览：确认点数抵扣与微信补付金额
5. app_get_h5order_commit             提交订单（wxPayAmount=0 时无需微信支付）

安全护栏：
- dryRun=True（默认）：走到预览即停止，不提交、不扣任何款项；
- 每个品种独立配置 rechargePhone / quantity / maxWxPayAmount；
- maxWxPayAmount：预览微信补付金额超过该值则中止；
- 电子券商品需在订单备注(leaveMessage)填写对应充值账号；
- 每品种每天限量 1 张：由 monitor.py 记录，下单成功当天不再购买该 sku。

配置两种写法（新版推荐 targets 列表，可给每个品种配不同充值账号）：
    "autoOrder": {
      "enabled": true, "dryRun": false,
      "targets": [
        {"skuId": "12325240480893951", "rechargePhone": "京东账号手机号"},
        {"skuId": "14794", "rechargePhone": "话费充值手机号"}
      ]
    }
旧版单目标写法（skuId 为字符串）仍然兼容。
"""
import re
from datetime import datetime


class AutoOrder:
    def __init__(self, api, cfg: dict, notifier=None):
        self.api = api
        self.cfg = cfg
        self.notifier = notifier
        self.dry_run = bool(cfg.get("dryRun", True))
        self.match_name = cfg.get("matchName", "")
        self.notify_dry_run = bool(cfg.get("notifyDryRun", True))
        self.card_nos = cfg.get("cardNos") or []      # 为空则自动使用全部点数卡

        # 目标列表：优先 targets；兼容旧版单 skuId 字符串写法
        targets = cfg.get("targets") or []
        if not targets and cfg.get("skuId"):
            targets = [{"skuId": cfg["skuId"]}]
        self.targets = targets
        # 顶层默认值（targets 里未指定时沿用）
        self.default_quantity = int(cfg.get("quantity", 1))
        self.default_max_wx_pay = float(cfg.get("maxWxPayAmount", 0))
        self.default_commit_from = int(cfg.get("commitFrom", 1))
        self.default_recharge_phone = cfg.get("rechargePhone", "")

    # ---------- 目标匹配 ----------
    def _target_cfg(self, sku_id: str, product_name: str = "") -> dict | None:
        # 先按 skuId 精确匹配
        for t in self.targets:
            if t.get("skuId") and str(t.get("skuId")) == str(sku_id):
                return t
        # skuId 可能已被校准更新：按商品名匹配（name 稳定，首选配置项）
        if product_name:
            for t in self.targets:
                name = t.get("name") or t.get("matchName")
                if name and (name == product_name or name in product_name):
                    return t
        return None

    def matches(self, sku_id: str, product_name: str = "") -> bool:
        """判断某个有货商品是否在自动购买目标范围内。"""
        if self._target_cfg(sku_id, product_name) is not None:
            return True
        if self.match_name:
            return bool(re.search(self.match_name, product_name or ""))
        return False  # 未配置目标：不自动买任何商品（只提醒）

    def _notify(self, title: str, content: str):
        if self.notifier:
            self.notifier.send(title, content)
        else:
            print(f"[AutoOrder 通知] {title}\n{content}", flush=True)

    def _pick_cards(self) -> list[dict]:
        cards = self.api.ptcard_list()
        picked = []
        for c in cards:
            if self.card_nos and c.get("cardNo") not in self.card_nos:
                continue
            deduct = c.get("deducteNum")
            if deduct is None:
                deduct = c.get("balanceNum") or 0
            if deduct and deduct > 0:
                picked.append({"cardNo": c.get("cardNo"), "deducteNum": deduct})
        return picked

    def attempt(self, sku_id: str, product_name: str = "") -> dict:
        log = lambda m: print(f"[AutoOrder {datetime.now():%H:%M:%S}] {m}", flush=True)
        t = self._target_cfg(sku_id, product_name) or {}
        quantity = int(t.get("quantity", self.default_quantity))
        max_wx_pay = float(t.get("maxWxPayAmount", self.default_max_wx_pay))
        commit_from = int(t.get("commitFrom", self.default_commit_from))
        recharge_phone = t.get("rechargePhone", self.default_recharge_phone)

        log(f"目标 sku={sku_id} {product_name} dryRun={self.dry_run} qty={quantity}")
        if not recharge_phone and not self.dry_run:
            log("警告: 未配置该品种的 rechargePhone（充值账号），备注将为空")
        try:
            # 1. 权威库存校验
            sku = self.api.sku_check(sku_id).get("data") or {}
            stocks = sku.get("usableStocks")
            if stocks is not None and stocks <= 0:
                log(f"库存校验未通过 usableStocks={stocks}，放弃")
                self._notify("自动下单未执行：库存校验时已被抢空",
                             f"{product_name} (sku={sku_id})\n提交前复核发现无货，请手动刷新商城查看。")
                return {"ok": False, "reason": "sku_check_no_stock"}

            # 2. 立即购买
            cart = self.api.shopcart_add(sku_id, quantity=quantity,
                                         commit_from=commit_from)
            shop_cart_id = (cart.get("data") or {}).get("shopCartId")
            if not shop_cart_id:
                log(f"加购失败: {cart}")
                self._notify("自动下单失败：加购未成功",
                             f"{product_name} (sku={sku_id})\n接口返回：{cart}\n请立即手动下单。")
                return {"ok": False, "reason": "shopcart_add_failed"}
            log(f"shopCartId={shop_cart_id}")

            # 3. 选卡
            cards = self._pick_cards()
            if not cards:
                log("没有可用点数卡，中止")
                self._notify("自动下单中止：没有可用点数卡",
                             "未获取到余额>0 的点数卡，请检查福利卡状态或 config 的 cardNos。")
                return {"ok": False, "reason": "no_cards"}
            log(f"使用点数卡: {cards}")

            # 4. 预览
            leave = f"充值手机号:{recharge_phone}" if recharge_phone else ""
            preview = self.api.order_preview(shop_cart_id, cards, leave_message=leave)
            pdata = preview.get("data") or {}
            wx_pay = pdata.get("orderWxPayAmount") or 0
            log(f"预览: 微信补付={wx_pay} 详情={str(pdata)[:300]}")
            if wx_pay > max_wx_pay:
                log(f"微信补付 {wx_pay} 超过上限 {max_wx_pay}，中止")
                self._notify("自动下单中止：微信补付超上限",
                             f"预览微信补付 {wx_pay} 元，超过设定上限 {max_wx_pay} 元。\n"
                             f"请确认点数余额后手动下单。")
                return {"ok": False, "reason": "wx_pay_over_limit", "wxPayAmount": wx_pay}

            # 5. 提交
            if self.dry_run:
                log("dryRun 模式：未提交订单。将 dryRun 设为 false 后才真实下单。")
                if self.notify_dry_run:
                    self._notify("【演练】自动下单预览完成（未扣款）",
                                 f"{product_name} (sku={sku_id})\n"
                                 f"点数卡：{cards}\n微信补付：{wx_pay} 元\n"
                                 f"备注：{leave or '（未填写）'}\n\n"
                                 f"确认无误后将 config.json 的 autoOrder.dryRun 改为 false。")
                return {"ok": True, "dryRun": True, "shopCartId": shop_cart_id,
                        "wxPayAmount": wx_pay, "preview": pdata}

            result = self.api.order_commit(shop_cart_id, cards, leave_message=leave)
            rdata = result.get("data") or {}
            log(f"提交结果: {str(rdata)[:400]}")
            if rdata.get("orderPayNumber"):
                self._notify("自动下单成功！",
                             f"{product_name}\n订单号：{rdata['orderPayNumber']}\n"
                             f"微信补付：{rdata.get('wxPayAmount', 0)} 元\n"
                             f"请到「我的订单」查看，电子券到账后可充值。")
                return {"ok": True, "orderCreated": True,
                        "orderPayNumber": rdata["orderPayNumber"]}
            if (rdata.get("wxPayMpOrder") or {}).get("payChannel"):
                self._notify("自动下单部分成功：待微信支付",
                             "订单已创建但需要微信支付（点数未覆盖全款）。\n"
                             "请立即打开商城完成支付，超时订单将关闭。")
                return {"ok": False, "orderCreated": True, "reason": "need_wx_pay",
                        "data": rdata}
            self._notify("自动下单结果未知，请立即人工检查",
                         f"接口返回：{str(rdata)[:300]}\n请打开商城「我的订单」确认。")
            return {"ok": False, "reason": "unknown", "data": rdata}

        except Exception as e:
            log(f"下单异常: {e}")
            self._notify("自动下单异常，请立即手动下单",
                         f"{product_name} (sku={sku_id})\n错误：{e}")
            return {"ok": False, "reason": f"exception:{e}"}
