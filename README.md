# DDD: frps wrapper with port-proxy

本镜像用于在受限环境（仅 443 可用）下运行 frps，并通过一个轻量 wrapper 进程完成：
- 对外监听“业务端口”（默认 6000）
- 当本地后端端口（默认 7000，对应 frps 暴露的端口）可用时，进行双向转发
- 当后端暂不可用时，立即返回占位信息，避免平台健康检查或上游调用报错

> 设计场景：frpc 通过 443 连接到 frps；平台（如 Koyeb）需要一个稳定可探测的本地端口用作健康检查/对外服务。此时，本镜像对外暴露 6000，同时在本地轮询 7000 是否就绪，并按需转发。

## 运行时参数

可通过环境变量或命令行参数进行配置：

- LISTEN_HOST / --listen-host：对外监听地址，默认 `0.0.0.0`
- LISTEN_PORT / --listen-port：对外监听端口，默认 `6000`
- TARGET_PORT / --target-port：后端目标端口（frps 暴露的端口），默认 `7000`
- PLACEHOLDER_HTTP_200 / --placeholder-http：当后端未就绪时返回 HTTP 200（文本），默认 `0`（关闭）。设置为 `1` 开启。
- PLACEHOLDER_MESSAGE / --placeholder-message：占位返回内容（文本），默认 `service not ready`。
- FRPS_BIN / --frps-bin：frps 二进制路径，默认 `/usr/bin/frps`

除上述参数外，传给容器的其它参数会被原样透传给 `frps`，例如：
- `-p 443`：frps 监听 443
- `-c /path/to/frps.ini`：使用外部挂载的配置文件（本项目未在镜像内内置 frps.ini）

## 示例（参考）

在本地 Docker（或 Koyeb）中：

- 对外暴露 6000，用作服务/健康检查端口
- frps 监听 443（供外部 frpc 连接）
- 当 frps 成功建立与 frpc 的隧道，且本地 7000 就绪时，6000 会自动转发到 7000

```bash
# 仅示意：实际在 Koyeb 请用平台的端口/健康检查配置界面
# 构建镜像
# docker build -t ddd-frps-wrapper .

# 运行容器（示例）
# docker run --rm -p 6000:6000 -p 443:443 \
#   -e LISTEN_PORT=6000 -e TARGET_PORT=7000 \
#   ddd-frps-wrapper -p 443
```

> 注意：frps 的端口与出站策略需按你的实际网络环境配置。若需要更复杂的 TLS、鉴权、路由等，请通过 `-c /path/to/frps.ini` 注入配置（本镜像不内置）。

## 行为说明

- 后端未就绪：
  - 若 `PLACEHOLDER_HTTP_200=1`，返回 `HTTP/1.1 200 OK` 与占位文本
  - 否则返回纯文本占位内容后关闭连接
- 后端就绪：纯 TCP 层转发（可用于 HTTP 等上层协议）
- 退出处理：容器收到 SIGTERM/SIGINT 时会优雅停止代理并终止 frps

## 文件结构

- Dockerfile：构建镜像，下载并安装 frps
- entrypoint.py：启动 frps 与端口代理的入口脚本
- README.md：使用说明（本文件）
