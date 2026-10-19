"""登录助手：用 Playwright 打开中票优品 H5 登录页，人工完成短信验证码登录后，
自动从 localStorage 提取 apiToken 并保存到 token.json。

依赖：pip install playwright && playwright install chromium

用法：
    python login_helper.py [--phone 13800138000] [--token-file token.json]
"""
import argparse
import json
import time
from pathlib import Path

LOGIN_URL = "https://zpmall.zhongpiaofuli.com/mall/login"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="", help="预填手机号（可选）")
    parser.add_argument("--token-file", default="token.json")
    parser.add_argument("--timeout", type=int, default=600, help="等待登录超时秒数")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("请先安装: pip install playwright && playwright install chromium")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print(f"已打开登录页：{LOGIN_URL}")
        print("请在浏览器中完成：输入手机号 -> 获取验证码 -> 登录。")
        if args.phone:
            try:
                page.fill("input[type=tel], input[placeholder*=手机]", args.phone)
                print(f"已预填手机号 {args.phone}")
            except Exception:
                pass

        token = None
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            try:
                token = page.evaluate("() => localStorage.getItem('apiToken')")
            except Exception:
                token = None
            if token:
                break
            time.sleep(2)

        browser.close()

    if not token:
        raise SystemExit("未在超时时间内检测到 apiToken，登录未完成。")
    Path(args.token_file).write_text(
        json.dumps({"apiToken": token, "savedAt": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"apiToken 已保存到 {args.token_file}（长度 {len(token)}）")
    print("将其填入 config.json 的 apiToken 字段即可启用下单相关功能。")


if __name__ == "__main__":
    main()
