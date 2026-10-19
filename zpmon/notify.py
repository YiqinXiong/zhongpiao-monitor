"""提醒渠道集合：Bark / PushPlus / Server酱 / 钉钉 / 企业微信 / 飞书 / Telegram / 自定义 Webhook。

所有渠道均为可选，在 config.json 的 notify 段配置即启用；
未配置任何渠道时至少输出到控制台和日志文件。
"""
import json
import urllib.parse
import urllib.request

from zp_api import UA


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


class Notifier:
    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.channels = []

        bark = cfg.get("bark") or {}
        if bark.get("key"):
            base = (bark.get("url") or "https://api.day.app").rstrip("/")
            self.channels.append(("bark", lambda t, c, b=base, k=bark["key"]:
                                  _get(f"{b}/{k}/{urllib.parse.quote(t)}?url=&body={urllib.parse.quote(c)}")))
        if cfg.get("pushplus", {}).get("token"):
            tok = cfg["pushplus"]["token"]
            self.channels.append(("pushplus", lambda t, c, k=tok: _post_json(
                "http://www.pushplus.plus/send",
                {"token": k, "title": t, "content": c})))
        if cfg.get("serverchan", {}).get("sendkey"):
            key = cfg["serverchan"]["sendkey"]
            self.channels.append(("serverchan", lambda t, c, k=key: _post_json(
                f"https://sctapi.ftqq.com/{k}.send",
                {"title": t, "desp": c})))
        if cfg.get("dingtalk", {}).get("webhook"):
            hook = cfg["dingtalk"]["webhook"]
            self.channels.append(("dingtalk", lambda t, c, h=hook: _post_json(
                h, {"msgtype": "markdown",
                    "markdown": {"title": t, "text": f"### {t}\n\n{c}"}})))
        if cfg.get("wecom", {}).get("webhook"):
            hook = cfg["wecom"]["webhook"]
            self.channels.append(("wecom", lambda t, c, h=hook: _post_json(
                h, {"msgtype": "markdown", "markdown": {"content": f"### {t}\n\n{c}"}})))
        if cfg.get("feishu", {}).get("webhook"):
            hook = cfg["feishu"]["webhook"]
            self.channels.append(("feishu", lambda t, c, h=hook: _post_json(
                h, {"msg_type": "text",
                    "content": {"text": f"【{t}】\n{c}"}})))
        tg = cfg.get("telegram") or {}
        if tg.get("botToken") and tg.get("chatId"):
            self.channels.append(("telegram", lambda t, c, tok=tg["botToken"], cid=tg["chatId"]:
                                  _post_json(f"https://api.telegram.org/bot{tok}/sendMessage",
                                             {"chat_id": cid, "text": f"*{t}*\n{c}",
                                              "parse_mode": "Markdown"})))
        if cfg.get("webhook", {}).get("url"):
            hook = cfg["webhook"]["url"]
            self.channels.append(("webhook", lambda t, c, h=hook: _post_json(
                h, {"title": t, "content": c})))

    def send(self, title: str, content: str):
        print(f"[NOTIFY] {title}\n{content}\n")
        for name, fn in self.channels:
            try:
                fn(title, content)
                print(f"[NOTIFY] {name} 发送成功")
            except Exception as e:  # 单个渠道失败不影响其他渠道
                print(f"[NOTIFY] {name} 发送失败: {e}")
