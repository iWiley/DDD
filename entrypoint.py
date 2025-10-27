import socket
import threading
import subprocess
import argparse
import os
import sys
import signal
import time
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
    Pump data from src to dst using half-close semantics.

    - Only this thread closes its own src (read side) when done.
    - On EOF (recv returns b""), shutdown dst's write side to signal no more data.
    - Do NOT close dst here; the peer pump thread is responsible for its own src.
    - Be tolerant to races where the peer already closed/shutdown the socket.
    """
    try:
        while True:
            try:
                data = src.recv(4096)
            except (OSError, ValueError):
                # src likely already closed or invalid; exit cleanly
                break

            if not data:
                # EOF from src: half-close dst write to signal end-of-stream
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
                break

            try:
                dst.sendall(data)
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                # Peer closed or other send error; exit loop
                break
    finally:
        # Close only our read side; leave dst lifecycle to the peer thread.
        try:
            src.shutdown(socket.SHUT_RD)
        except Exception:
            pass
        try:
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


def handle_client(client_sock, target_port, placeholder_http, placeholder_message):
    if is_backend_alive(target_port):
        try:
            target_sock = socket.create_connection(("127.0.0.1", target_port))
            t1 = threading.Thread(target=pipe, args=(client_sock, target_sock), daemon=True)
            t2 = threading.Thread(target=pipe, args=(target_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception as e:
            send_placeholder(client_sock, placeholder_http, placeholder_message or b"backend connect error\r\n")
    else:
        send_placeholder(client_sock, placeholder_http, placeholder_message or b"service not ready\r\n")

def start_proxy(listen_host, listen_port, target_port, placeholder_http, placeholder_message, stop_event):
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
                    args=(client_sock, target_port, placeholder_http, placeholder_message),
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
            args=(args.listen_host, lp, tp, args.placeholder_http, args.placeholder_message, stop_event),
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
