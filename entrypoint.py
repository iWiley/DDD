import socket
import threading
import subprocess
import argparse
import os
import sys

def is_frps_alive(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except:
        return False

def pipe(src, dst):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    finally:
        src.close()
        dst.close()

def handle_client(client_sock, target_port):
    if is_frps_alive(target_port):
        try:
            target_sock = socket.create_connection(("127.0.0.1", target_port))
            threading.Thread(target=pipe, args=(client_sock, target_sock)).start()
            threading.Thread(target=pipe, args=(target_sock, client_sock)).start()
        except Exception as e:
            client_sock.send(b"Error connecting to frps proxy\r\n")
            client_sock.close()
    else:
        client_sock.send(b"frps not ready\r\n")
        client_sock.close()

def start_health_proxy(listen_port, target_port):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", listen_port))
    server.listen(5)
    print(f"[health-proxy] Listening on {listen_port}, forwarding to {target_port}")

    while True:
        client_sock, _ = server.accept()
        threading.Thread(target=handle_client, args=(client_sock, target_port)).start()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("LISTEN_PORT", "8081")))
    parser.add_argument("--target-port", type=int, default=int(os.getenv("TARGET_PORT", "6000")))
    args, unknown = parser.parse_known_args()

    # 启动 frps 子进程
    frps_cmd = ["/usr/bin/frps"] + unknown
    print(f"[entrypoint] Starting frps with: {' '.join(frps_cmd)}")
    subprocess.Popen(frps_cmd)

    # 启动健康检查代理
    start_health_proxy(args.listen_port, args.target_port)

if __name__ == "__main__":
    main()
