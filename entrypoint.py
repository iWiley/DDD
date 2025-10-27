import socket
import threading
import subprocess
import argparse
import os
import sys
import signal
import time
import select
from contextlib import closing

def is_backend_alive(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except:
        return False

def pipe(src, dst):
    """
    单向拷贝：从 src 读，写入 dst。
    - 仅在本线程中关闭 src（读端）；不直接关闭 dst，避免与对向线程竞争导致 Bad file descriptor。
    - 当读到 EOF 时，半关闭对端写（shutdown(SHUT_WR)）提示对端完成写入。
    - 捕获 OSError/IOError，安静退出。
    """
    try:
        while True:
            try:
                data = src.recv(4096)
            except OSError:
                break
            if not data:
                # 对端读完，半关闭写端，提示完成
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
                break
            try:
                dst.sendall(data)
            except OSError:
                break
    finally:
        try:
            try:
                src.shutdown(socket.SHUT_RD)
            except Exception:
                pass
            src.close()
        except Exception:
            pass

def send_placeholder(client_sock, http_200=False, message=None):
    try:
        msg = message or b"service not ready\r\n"
        if isinstance(msg, str):
            msg = msg.encode()
        if http_200:
            body = msg
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: close\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            client_sock.sendall(resp)
        else:
            client_sock.sendall(msg)
    finally:
        client_sock.close()


def _read_http_headers_nonblocking(sock: socket.socket, timeout_ms: int, max_bytes: int = 1024):
    """
    在极短时间内通过 PEEK 方式窥探客户端数据，若发现完整 HTTP 头（以 \r\n\r\n 结束），返回解析后的头文本。
    返回 (headers_str_or_None, raw_bytes_read)。未读到任何数据或未形成完整 HTTP 头，则 headers 为 None。
    """
    try:
        # 先用 select 观察是否可读，避免无谓阻塞
        r, _, _ = select.select([sock], [], [], max(0.0, timeout_ms / 1000.0))
        if not r:
            return None, b""

        # 使用 MSG_PEEK 尽量少读，避免消费字节；若不是 HTTP，将不阻塞地直接转发
        try:
            peek = sock.recv(max_bytes, getattr(socket, "MSG_PEEK", 0))
        except Exception:
            peek = b""

        if not peek:
            return None, b""

        header_end = peek.find(b"\r\n\r\n")
        if header_end == -1:
            # 未形成完整 HTTP 头，视为非 HTTP 或数据尚未到齐；为高效起见不再继续读取
            return None, peek

        header_bytes = peek[: header_end + 4]
        try:
            headers_text = header_bytes.decode("iso-8859-1", errors="replace")
        except Exception:
            headers_text = None
        return headers_text, peek
    except Exception:
        return None, b""


def _match_health_header(headers_text: str, header_name: str, header_value: str | None):
    if not headers_text:
        return False
    # 简单大小写不敏感匹配
    lines = headers_text.splitlines()
    for ln in lines[1:]:  # 跳过请求行
        if not ln:
            continue
        if ":" not in ln:
            continue
        name, val = ln.split(":", 1)
        if name.strip().lower() == header_name.lower():
            if header_value is None:
                return True
            return header_value.strip() in val.strip()
    return False


def handle_client(client_sock, target_port, placeholder_http, placeholder_message, health_cfg):
    # 优先拦截“健康检查”请求：匹配到特定 HTTP 头则直接 200 OK 并关闭
    initial_data = b""
    try:
        if health_cfg and health_cfg.get("header_name"):
            headers_text, raw = _read_http_headers_nonblocking(
                client_sock,
                timeout_ms=int(health_cfg.get("timeout_ms", 100)),
                max_bytes=int(health_cfg.get("max_bytes", 1024)),
            )
            # 采用 PEEK，不消费字节；后续若转发，后端能收到完整请求
            initial_data = b""
            if headers_text and _match_health_header(
                headers_text,
                health_cfg.get("header_name"),
                health_cfg.get("header_value"),
            ):
                send_placeholder(
                    client_sock,
                    http_200=True,
                    message=health_cfg.get("response_message") or b"ok\r\n",
                )
                return
    except Exception:
        # 出错则忽略健康检查分支，走普通转发/占位
        initial_data = initial_data or b""

    if is_backend_alive(target_port):
        try:
            target_sock = socket.create_connection(("127.0.0.1", target_port))
            # 使用 PEEK，不需要预先补发任何数据
            t1 = threading.Thread(target=pipe, args=(client_sock, target_sock), daemon=True)
            t2 = threading.Thread(target=pipe, args=(target_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception as e:
            send_placeholder(client_sock, placeholder_http, placeholder_message or b"backend connect error\r\n")
    else:
        send_placeholder(client_sock, placeholder_http, placeholder_message or b"service not ready\r\n")

def start_proxy(listen_host, listen_port, target_port, placeholder_http, placeholder_message, stop_event, health_cfg):
    try:
        with closing(socket.socket()) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((listen_host, listen_port))
            server.listen(64)
            server.settimeout(1.0)
            print(f"[proxy] Listening on {listen_host}:{listen_port}, forwarding to 127.0.0.1:{target_port}")

            while not stop_event.is_set():
                try:
                    client_sock, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=handle_client,
                    args=(client_sock, target_port, placeholder_http, placeholder_message, health_cfg),
                    daemon=True,
                ).start()
    except OSError as e:
        print(f"[proxy] ERROR binding {listen_host}:{listen_port} -> {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default=os.getenv("LISTEN_HOST", "0.0.0.0"))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("LISTEN_PORT", "6000")))
    parser.add_argument("--target-port", type=int, default=int(os.getenv("TARGET_PORT", "7000")))
    parser.add_argument("--map", action="append", default=None, help="Map pair: listen:target, can repeat. e.g. --map 6000:7000 --map 6001:7001")
    parser.add_argument("--placeholder-http", action="store_true", default=os.getenv("PLACEHOLDER_HTTP_200", "0") == "1")
    parser.add_argument("--placeholder-message", default=os.getenv("PLACEHOLDER_MESSAGE", "service not ready\r\n"))
    parser.add_argument("--frps-bin", default=os.getenv("FRPS_BIN", "/usr/bin/frps"))
    # 健康检查（基于特定 HTTP 头）可选配置：
    #  - --health-header 形如 "X-Health-Check=1" 或 "X-Health-Check"（仅判断存在）
    #  - --health-response-message 健康检查命中时返回的正文
    #  - --health-timeout-ms 解析 HTTP 头的最大片刻等待时长
    # 默认开启：若未提供环境变量，则使用通用的 X-Health-Check 作为默认头名
    parser.add_argument("--health-header", default=os.getenv("HEALTH_HEADER", "X-Health-Check"))
    parser.add_argument("--health-response-message", default=os.getenv("HEALTH_RESPONSE_MESSAGE", "ok\r\n"))
    parser.add_argument("--health-timeout-ms", type=int, default=int(os.getenv("HEALTH_TIMEOUT_MS", "100")))
    parser.add_argument("--health-max-bytes", type=int, default=int(os.getenv("HEALTH_MAX_BYTES", "1024")))
    args, unknown = parser.parse_known_args()

    # 解析多映射：优先 --map，其次 PROXY_MAPS 环境变量，最后回退到单对 listen/target
    maps = []
    if args.map:
        raw_pairs = args.map
    else:
        env_maps = os.getenv("PROXY_MAPS", "").strip()
        raw_pairs = [p for p in env_maps.split(",") if p] if env_maps else []

    def parse_pair(p: str):
        p = p.strip()
        if ":" not in p:
            raise ValueError(f"invalid map '{p}', expected listen:target")
        l, r = p.split(":", 1)
        return int(l), int(r)

    for p in raw_pairs:
        try:
            lp, tp = parse_pair(p)
            maps.append((lp, tp))
        except Exception as e:
            print(f"[entrypoint] Skip invalid map '{p}': {e}")

    if not maps:
        maps = [(args.listen_port, args.target_port)]

    # 启动 frps 子进程（端口/配置通过命令行 unknown 透传，例如 -p 443 或 -c /path/to/frps.ini）
    frps_cmd = [args.frps_bin] + unknown
    print(f"[entrypoint] Starting frps with: {' '.join(frps_cmd)}")
    frps_proc = subprocess.Popen(frps_cmd)

    stop_event = threading.Event()

    # 健康检查配置解析
    health_cfg = None
    if args.health_header:
        header = args.health_header.strip()
        name, val = (header.split("=", 1) + [None])[:2] if "=" in header else (header, None)
        health_cfg = {
            "header_name": name.strip(),
            "header_value": val.strip() if isinstance(val, str) else None,
            "response_message": args.health_response_message,
            "timeout_ms": int(args.health_timeout_ms),
            "max_bytes": int(args.health_max_bytes),
        }

    def handle_signals(signum, frame):
        print(f"[entrypoint] Caught signal {signum}, shutting down...")
        stop_event.set()
        try:
            frps_proc.terminate()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, handle_signals)
    signal.signal(signal.SIGINT, handle_signals)

    # 启动所有端口映射
    print("[entrypoint] Port maps:")
    for lp, tp in maps:
        print(f"  - {args.listen_host}:{lp} -> 127.0.0.1:{tp}")

    threads = []
    for lp, tp in maps:
        t = threading.Thread(
            target=start_proxy,
            args=(args.listen_host, lp, tp, args.placeholder_http, args.placeholder_message, stop_event, health_cfg),
            daemon=True,
        )
        t.start()
        threads.append(t)

    try:
        # 主线程阻塞等待信号
        while not stop_event.is_set():
            signal.pause() if hasattr(signal, "pause") else time.sleep(1)
    finally:
        # 等待 frps 退出
        try:
            frps_proc.wait(timeout=5)
        except Exception:
            try:
                frps_proc.kill()
            except Exception:
                pass

if __name__ == "__main__":
    main()
