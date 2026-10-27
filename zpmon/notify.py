"""提醒渠道集合：Bark / ggsuper(七只猪全能Bark，支持鸿蒙华为) / PushPlus / Server酱 /
钉钉 / 企业微信 / 飞书 / Telegram / 自定义 Webhook。

所有渠道均为可选，在 config.json 的 notify 段配置即启用；
未配置任何渠道时至少输出到控制台和日志文件。

send(title, content, url="")：url 为点击跳转链接，仅 Bark / ggsuper 会使用。
"""
import json
import urllib.parse
import urllib.request

from zp_api import UA

# 七只猪「全能消息推送 BARK」推送 API（OpenAPI 3.1, https://www.ggsuper.com.cn/push/api.html）
#   个人推送：POST /push/api/v1/sendMsg3_New.php (application/json)
#   频道推送：POST /push/api/v1/sendChannelMsg2_New.php (application/json)
#   成功响应：{"code":"80000000","msg":"Success"}；code=200 表示 token 错误/失效
GG_SUCCESS_CODE = "80000000"


def _post_json(url: str, payload: dict, timeout: int = 10):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _get(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _gg_push(url: str, payload: dict, timeout: int = 10) -> str:
    """调用 ggsuper 推送接口并校验业务码。"""
    body = _post_json(url, payload, timeout)
    try:
        data = json.loads(body)
    except Exception:
        return body
    code = str(data.get("code"))
    if code != GG_SUCCESS_CODE:
        raise RuntimeError(f"ggsuper 返回 code={code} msg={data.get('msg')}")
    return body


class Notifier:
    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.channels = []

        bark = cfg.get("bark") or {}
        if bark.get("key"):
            base = (bark.get("url") or "https://api.day.app").rstrip("/")
            self.channels.append(("bark", lambda t, c, u="", b=base, k=bark["key"]:
                                  _get(f"{b}/{k}/{urllib.parse.quote(t)}"
                                       f"?url={urllib.parse.quote(u)}"
                                       f"&body={urllib.parse.quote(c)}")))

        # 七只猪全能BARK：鸿蒙/华为机型离线推送（普通 Bark 仅 iOS）
        gg = cfg.get("ggsuper") or {}
        if gg.get("token"):
            base = (gg.get("url") or "https://www.ggsuper.com.cn").rstrip("/")
            tok = gg["token"]
            sender = gg.get("sender") or ""
            icon = gg.get("icon") or ""
            channel_id = gg.get("channelId") or ""
            if channel_id:
                endpoint = f"{base}/push/api/v1/sendChannelMsg2_New.php"
                self.channels.append(("ggsuper-频道", lambda t, c, u="", e=endpoint, k=tok,
                                                             cid=channel_id, s=sender:
                                      _gg_push(e, {"title": t, "msg": c, "token": k,
                                                   "channel_id": cid, "sender": s,
                                                   **({"url": u} if u else {})})))
            else:
                endpoint = f"{base}/push/api/v1/sendMsg3_New.php"
                self.channels.append(("ggsuper", lambda t, c, u="", e=endpoint, k=tok,
                                                           s=sender, ic=icon:
                                      _gg_push(e, {"title": t, "msg": c, "token": k,
                                                   "issecure": 0, "msgType": 1, "sender": s,
                                                   **({"icon": ic} if ic else {}),
                                                   **({"url": u} if u else {})})))

        if cfg.get("pushplus", {}).get("token"):
            tok = cfg["pushplus"]["token"]
            self.channels.append(("pushplus", lambda t, c, u="", k=tok: _post_json(
                "http://www.pushplus.plus/send",
                {"token": k, "title": t, "content": c})))
        if cfg.get("serverchan", {}).get("sendkey"):
            key = cfg["serverchan"]["sendkey"]
            self.channels.append(("serverchan", lambda t, c, u="", k=key: _post_json(
                f"https://sctapi.ftqq.com/{k}.send",
                {"title": t, "desp": c})))
        if cfg.get("dingtalk", {}).get("webhook"):
            hook = cfg["dingtalk"]["webhook"]
            self.channels.append(("dingtalk", lambda t, c, u="", h=hook: _post_json(
                h, {"msgtype": "markdown",
                    "markdown": {"title": t, "text": f"### {t}\n\n{c}"}})))
        if cfg.get("wecom", {}).get("webhook"):
            hook = cfg["wecom"]["webhook"]
            self.channels.append(("wecom", lambda t, c, u="", h=hook: _post_json(
                h, {"msgtype": "markdown", "markdown": {"content": f"### {t}\n\n{c}"}})))
        if cfg.get("feishu", {}).get("webhook"):
            hook = cfg["feishu"]["webhook"]
            self.channels.append(("feishu", lambda t, c, u="", h=hook: _post_json(
                h, {"msg_type": "text",
                    "content": {"text": f"【{t}】\n{c}"}})))
        tg = cfg.get("telegram") or {}
        if tg.get("botToken") and tg.get("chatId"):
            self.channels.append(("telegram", lambda t, c, u="", tok=tg["botToken"], cid=tg["chatId"]:
                                  _post_json(f"https://api.telegram.org/bot{tok}/sendMessage",
                                             {"chat_id": cid, "text": f"*{t}*\n{c}",
                                              "parse_mode": "Markdown"})))
        if cfg.get("webhook", {}).get("url"):
            hook = cfg["webhook"]["url"]
            self.channels.append(("webhook", lambda t, c, u="", h=hook: _post_json(
                h, {"title": t, "content": c})))

    def send(self, title: str, content: str, url: str = ""):
        print(f"[NOTIFY] {title}\n{content}\n")
        for name, fn in self.channels:
            try:
                fn(title, content, url or "")
                print(f"[NOTIFY] {name} 发送成功")
            except Exception as e:  # 单个渠道失败不影响其他渠道
                print(f"[NOTIFY] {name} 发送失败: {e}")
