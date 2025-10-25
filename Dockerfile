FROM python:3.11-slim

# 安装 frps
ADD https://github.com/fatedier/frp/releases/download/v0.65.0/frp_0.65.0_linux_amd64.tar.gz /tmp/
RUN tar -xzf /tmp/frp_0.65.0_linux_amd64.tar.gz -C /usr/bin --strip-components=1

# 添加配置和脚本
COPY frps.ini /app/frps.ini
COPY entrypoint.py /app/entrypoint.py

WORKDIR /app
ENTRYPOINT ["python", "entrypoint.py"]