"""交互式登录工具：在终端里完成短信验证码登录，自动写入 config.json 的 apiToken。

专为无显示器环境设计（群晖 NAS / 服务器 / SSH），不需要浏览器：
    python login.py                       # 默认找 ./config.json 或 /data/config.json
    python login.py --config /data/config.json

NAS 容器内使用：
    docker exec -it zpmon python /app/login.py

流程：输入手机号 -> 接口发送验证码 -> 你从手机短信读码输入 -> 登录成功 ->
      自动把 apiToken 写进 config.json，并顺带查询福利卡余额确认登录有效。
"""
import argparse
import json
import sys
from pathlib import Path

from zp_api import ApiError, MallApi

SMS_TEMPLATE_CODE = "SMS_199890295"


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        sys.exit("\n已取消（无输入）")


def find_config(explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            sys.exit(f"配置文件不存在: {p}")
        return p
    for cand in (Path("config.json"), Path("/data/config.json")):
        if cand.exists():
            return cand
    sys.exit("未找到 config.json（可用 --config 指定路径）")


def save_token(cfg_path: Path, token: str):
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["apiToken"] = token
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"\n✅ apiToken 已写入 {cfg_path}（长度 {len(token)}）")
    print("   自动下单相关功能已可用；如监控正在运行，下轮轮询即生效。")
    print("   （若 autoOrder.enabled 仍为 false，只会提醒不会自动购买）")


def show_cards(api: MallApi):
    try:
        cards = api.ptcard_list()
        if not cards:
            print("   当前账号没有已绑定的福利卡（app_get_h5member_ptcard_list 为空）")
            return
        print("   福利卡列表：")
        for c in cards:
            name = c.get("cardName") or c.get("templateName") or "未命名卡"
            print(f"   - {name} | 卡号 {c.get('cardNo')} | 余额 {c.get('balanceNum')}"
                  f" | 可抵扣 {c.get('deducteNum')}")
    except ApiError as e:
        print(f"   （查询福利卡失败: {e}）")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="", help="config.json 路径")
    args = parser.parse_args()
    cfg_path = find_config(args.config)
    city = 110100
    try:
        city = int(json.loads(cfg_path.read_text(encoding="utf-8")).get("cityCode", 110100))
    except Exception:
        pass

    api = MallApi(city_code=city)
    print(f"配置文件: {cfg_path}")

    phone = _ask("请输入手机号: ")
    if not (phone.isdigit() and len(phone) == 11):
        sys.exit("手机号格式不正确（应为 11 位数字）")

    # 1. 发送验证码（失败可重试）
    while True:
        try:
            api.call("app_get_phone_sms_code_send",
                     memberMobile=phone, templateCode=SMS_TEMPLATE_CODE, wxState="")
            print("📨 验证码已发送，请查收手机短信（60 秒内可重发一次）")
            break
        except ApiError as e:
            print(f"发送失败: {e}")
            if _ask("重试发送？(y/n): ").lower() != "y":
                sys.exit("已取消")

    # 2. 输入验证码登录（验证码错误可重输，不用重新发送）
    for attempt in range(1, 6):
        code = _ask("请输入短信验证码: ")
        if not code:
            continue
        try:
            data = api.call("app_set_phone_sms_code_login",
                            memberMobile=phone, smsCode=code, wxState="").get("data") or {}
        except ApiError as e:
            print(f"登录失败: {e}（还可重试 {5 - attempt} 次）")
            continue
        token = data.get("apiToken")
        if not token:
            print(f"接口未返回 apiToken: {data}")
            sys.exit(1)
        print("\n✅ 登录成功！")
        print(f"   bindCardGuidanceStatus={data.get('bindCardGuidanceStatus')}"
              f"（1 表示建议去 App 绑定福利卡）")
        show_cards(MallApi(api_token=token, city_code=city))
        save_token(cfg_path, token)
        return
    sys.exit("验证码错误次数过多，请重新运行本工具")


if __name__ == "__main__":
    main()
