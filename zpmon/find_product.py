"""商品查询工具：按关键词搜索商品，输出可直接粘贴到 config.json 的配置片段。

用法：
    python find_product.py 京东                 # 用全局城市搜索
    python find_product.py 京东 --city 110100   # 指定城市（北京）
    python find_product.py 京东 --city 440300   # 指定城市（深圳）
"""
import argparse
import json

from zp_api import MallApi

CITY_NAMES = {
    110100: "北京", 310100: "上海", 440100: "广州", 440300: "深圳",
    510100: "成都", 420100: "武汉", 310115: "上海浦东", 120100: "天津",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("keyword", help="搜索关键词，如：京东")
    parser.add_argument("--city", type=int, default=110100,
                        help="城市编码，默认 110100（北京）。常用：广州 440100 深圳 440300 上海 310100 成都 510100")
    args = parser.parse_args()

    api = MallApi(city_code=args.city)
    data = api.product_list(args.keyword).get("data") or {}
    products = data.get("productList") or []

    city_name = CITY_NAMES.get(args.city, str(args.city))
    print(f"\n城市：{city_name}({args.city})  关键词：{args.keyword}  "
          f"共 {len(products)} 个商品\n")
    if not products:
        print("（无结果——该城市可能未上架相关商品，换个城市或关键词试试）\n")
        return

    for i, p in enumerate(products, 1):
        stocks = p.get("usableStocks")
        stock_txt = f"{stocks}" if stocks is not None else "未同步"
        print(f"{i}. {p['productName']}")
        print(f"   productId={p['productId']}  skuId={p['skuId']}  "
              f"库存={stock_txt}  售价={p.get('skuMinSalePrice')}")
    print()

    # 生成可粘贴的配置片段（默认选第一个商品）
    first = products[0]
    snippet = {
        "targets": [
            {
                "mode": "detail",
                "productId": first["productId"],
                "skuId": first["skuId"],
                "cityCode": args.city,
            },
            {
                "mode": "search",
                "searchKey": args.keyword,
                "nameFilter": args.keyword,
                "cityCode": args.city,
            },
        ],
        "autoOrder": {
            "skuId": first["skuId"],
            "quantity": 1,
            "rechargePhone": "填京东账号手机号",
            "maxWxPayAmount": 0,
        },
    }
    print("将以下内容合并进 config.json 的对应字段即可监控/购买：")
    print(json.dumps(snippet, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
