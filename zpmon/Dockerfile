# 中票优品补货监控 - 群晖 NAS / Docker 部署镜像
# 多架构支持（amd64 / arm64），群晖 x86 与 ARM 机型均可使用
FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 运行依赖（仅 requests，保持镜像小巧）
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 程序代码
COPY zp_api.py notify.py monitor.py auto_order.py find_product.py login.py login_helper.py ./

# 数据目录：配置 / 状态 / 日志均在此，通过卷挂载持久化
RUN mkdir -p /data
WORKDIR /data

# 用法：
#   docker compose up -d --build        （默认启动监控）
#   docker exec -it zpmon python /app/login.py                     （交互式登录取token，NAS适用）
#   docker exec zpmon python /app/monitor.py --calibrate-now     （手动校准）
#   docker exec zpmon python /app/monitor.py --mark-purchased <skuId>
#   docker exec zpmon python /app/monitor.py --reset-purchased
#   docker exec zpmon python /app/find_product.py 京东            （查商品ID）
ENTRYPOINT ["python", "/app/monitor.py"]
CMD ["--config", "/data/config.json"]
