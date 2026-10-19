#!/usr/bin/env python3
"""下载本地语义嵌入模型 bge-small-zh-v1.5（Xenova 的 ONNX 版，约 100MB）。

用法：python3 download_model.py
可设 HF_ENDPOINT 环境变量指定镜像，默认 huggingface.co，失败自动改试 hf-mirror.com。
"""

import os
import sys
import time
from pathlib import Path

import requests

REPO = "Xenova/bge-small-zh-v1.5"
FILES = [
    ("onnx/model.onnx", "model.onnx"),
    ("tokenizer.json", "tokenizer.json"),
]
DEST = Path(
    os.environ.get(
        "MEMORY_MODEL_DIR",
        Path(__file__).resolve().parent / "models" / "bge-small-zh-v1.5",
    )
)
HOSTS = [os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/"), "https://hf-mirror.com"]


def fetch(rel_path: str, dest: Path) -> bool:
    for host in HOSTS:
        url = f"{host}/{REPO}/resolve/main/{rel_path}"
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                done = 0
                start = time.time()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            sys.stdout.write(f"\r  {dest.name}: {pct}% ({done >> 20}MB/{total >> 20}MB)")
                            sys.stdout.flush()
                tmp.rename(dest)
                speed = done / max(time.time() - start, 1) / (1 << 20)
                print(f"\r  {dest.name}: 完成 {done >> 20}MB，{speed:.1f}MB/s（{host}）")
                return True
        except Exception as exc:
            print(f"\r  {dest.name}: {host} 失败（{exc}），尝试下一个源")
    return False


def main() -> int:
    print(f"下载 {REPO} -> {DEST}")
    for rel_path, name in FILES:
        dest = DEST / name
        if dest.exists() and dest.stat().st_size > 10_000:
            print(f"  {name}: 已存在，跳过")
            continue
        if not fetch(rel_path, dest):
            print(f"下载失败：{rel_path}")
            return 1
    print("全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
