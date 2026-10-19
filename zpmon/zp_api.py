"""中票优品 MallApi 客户端（复刻 H5 签名逻辑）。

逆向结论（2026-10 实测）：
- 所有业务接口统一为 POST https://zpmall-api.zhongpiaofuli.com/mall/MallApi/handle
- 请求头：methodName / appKey / timeStamp / paramSign / apiToken
- 签名算法：参数按 key 排序后拼接 "k=v&"（对象值 JSON 序列化），末尾追加
  "appKey=<APP_KEY>"，整体取 MD5 即为 paramSign。
- apiToken 长度 > 64 时才参与签名（与前端 JS 逻辑一致）。
- 未登录也可查询商品列表/详情（usableStocks 即库存字段）。
"""
import hashlib
import json
import time

import requests

APP_KEY = "3dfde15355fd0442f81defcbd02a7a10"
API_URL = "https://zpmall-api.zhongpiaofuli.com/mall/MallApi/handle"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


class ApiError(Exception):
    """接口返回 code != 200 或网络异常。"""


class MallApi:
    def __init__(self, api_token: str = "", city_code: int = 110100):
        self.api_token = api_token or ""
        self.city_code = city_code
        self.session = requests.Session()
        self.session.headers.update({
            "content-type": "application/json;charset=UTF-8",
            "referer": "https://zpmall.zhongpiaofuli.com/",
            "accept-language": "zh-CN,zh;q=0.9",
            "user-agent": UA,
        })

    # ---------- 签名 ----------
    @staticmethod
    def _sign(params: dict) -> str:
        parts = []
        for k in sorted(params.keys()):
            v = params[k]
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
            parts.append(f"{k}={v}&")
        parts.append(f"appKey={APP_KEY}")
        return hashlib.md5("".join(parts).encode("utf-8")).hexdigest()

    # ---------- 通用调用 ----------
    def call(self, method_name: str, **params):
        body = dict(params)
        if self.api_token and len(self.api_token) > 64:
            body["apiToken"] = self.api_token
        body["timeStamp"] = int(time.time() * 1000)
        body["methodName"] = method_name
        headers = {
            "methodName": method_name,
            "appKey": APP_KEY,
            "timeStamp": str(body["timeStamp"]),
            "paramSign": self._sign(body),
            "apiToken": self.api_token,
        }
        resp = self.session.post(API_URL, json=body, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise ApiError(f"{method_name} -> code={data.get('code')} msg={data.get('message')}")
        return data

    # ---------- 商品 ----------
    def product_list(self, search_key: str, page_num: int = 1, sort_type: int = 0,
                     brand_id: str = "", category_ids: str = "",
                     city_code: int | None = None) -> dict:
        return self.call(
            "app_get_h5product_list",
            cityCode=city_code if city_code is not None else self.city_code,
            categoryIds=category_ids, pageNum=page_num,
            searchKey=search_key, merchantId="", packageId="",
            sortType=sort_type, brandId=brand_id,
        )

    def product_detail(self, product_id: str, city_code: int | None = None) -> dict:
        return self.call(
            "app_get_h5product_detail",
            productId=product_id, latitude="", longitude="",
            cityCode=city_code if city_code is not None else self.city_code,
        )

    def sku_check(self, sku_id: str) -> dict:
        """下单前权威库存校验，返回最新 sku 对象。"""
        return self.call("app_get_h5product_sku_check", skuId=sku_id)

    def shopcart_add(self, sku_id: str, quantity: int = 1, commit_from: int = 1) -> dict:
        """commitFrom: 1=立即购买 0=加入购物车。返回 data.shopCartId。"""
        return self.call(
            "app_set_h5product_shopcart_add",
            skuId=sku_id, quantity=quantity, commitFrom=commit_from,
        )

    # ---------- 福利卡 ----------
    def ptcard_list(self) -> list:
        """点数卡列表 -> data.zpCardList（cardNo / balanceNum / deducteNum ...）。"""
        return self.call("app_get_h5member_ptcard_list").get("data", {}).get("zpCardList", [])

    # ---------- 订单 ----------
    def order_preview(self, shop_cart_ids, zp_card_ids=None,
                      leave_message: str = "", address_id: str = "") -> dict:
        ids = shop_cart_ids if isinstance(shop_cart_ids, str) else ",".join(shop_cart_ids)
        body = {
            "shopCartIdList": ids,
            "orderExtInfo": {"leaveMessage": leave_message,
                             "merchantExtInfo": {}, "addressId": address_id},
        }
        if zp_card_ids:
            body["zpCardIds"] = zp_card_ids
        return self.call("app_get_h5order_preview", **body)

    def order_commit(self, shop_cart_ids, zp_card_ids=None,
                     leave_message: str = "", address_id: str = "") -> dict:
        ids = shop_cart_ids if isinstance(shop_cart_ids, str) else ",".join(shop_cart_ids)
        body = {
            "shopCartIdList": ids,
            "orderExtInfo": {"leaveMessage": leave_message,
                             "merchantExtInfo": {}, "addressId": address_id},
        }
        if zp_card_ids:
            body["zpCardIds"] = zp_card_ids
        return self.call("app_get_h5order_commit", **body)
