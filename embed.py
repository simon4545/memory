"""本地嵌入(embedding)提供者。

提供者链（可用环境变量 MEMORY_EMBED_PROVIDER 强制指定）：
  onnx  — 本地 ONNX 版 bge-small-zh-v1.5（512 维，中英文语义检索，需先运行 download_model.py）
  hash  — 确定性字符 n-gram 哈希（512 维，零依赖零下载，退化为主题词/子串模糊匹配）

所有提供者输出 L2 归一化向量，保证与 sqlite-vec 的距离排序语义一致。
"""

import hashlib
import math
import os
import re
import sys
from pathlib import Path

MODEL_DIR = Path(
    os.environ.get(
        "MEMORY_MODEL_DIR",
        Path(__file__).resolve().parent / "models" / "bge-small-zh-v1.5",
    )
)

# bge 系列检索时给查询侧加的指令前缀（文档侧不加）
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def _log(msg: str) -> None:
    print(f"[embed] {msg}", file=sys.stderr, flush=True)


def _ascii_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text)
    grams: set[str] = set(words)
    for w in words:
        if len(w) > 3:
            grams |= {w[i : i + 3] for i in range(len(w) - 2)}
    return grams


def _cjk_tokens(text: str) -> set[str]:
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    grams: set[str] = set(chars)
    for i in range(len(chars) - 1):
        grams.add(chars[i] + chars[i + 1])
    return grams


class HashEmbedder:
    name = "hash"
    dim = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for gram in _ascii_tokens(text.lower()) | _cjk_tokens(text):
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                vec[int.from_bytes(digest, "big") % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]


class OnnxEmbedder:
    """ONNX Runtime 推理 bge-small-zh-v1.5：CLS 池化 + L2 归一化。"""

    name = "onnx-bge-small-zh-v1.5"

    def __init__(self, model_dir: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = model_dir / "model.onnx"
        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tok.enable_truncation(max_length=256)

        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("MEMORY_ONNX_THREADS", "4"))
        self.sess = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._np = np
        self._input_names = {i.name for i in self.sess.get_inputs()}
        outputs = self.sess.get_outputs()
        self._output_name = next(
            (o.name for o in outputs if "last_hidden_state" in o.name), outputs[0].name
        )
        self.dim = len(self.embed(["维度探测"])[0])
        _log(f"已加载 {self.name}（dim={self.dim}, threads={options.intra_op_num_threads}）")

    def embed(self, texts: list[str]) -> list[list[float]]:
        np = self._np
        results = []
        for text in texts:
            enc = self.tok.encode(text)
            feed = {
                "input_ids": np.array([enc.ids], dtype=np.int64),
                "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            }
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
            hidden = self.sess.run([self._output_name], feed)[0]
            cls = hidden[0, 0].astype(np.float32)
            norm = np.linalg.norm(cls) or 1.0
            results.append((cls / norm).tolist())
        return results

    def embed_query(self, query: str) -> list[float]:
        return self.embed([BGE_QUERY_INSTRUCTION + query])[0]


def model_files_present() -> bool:
    return (MODEL_DIR / "model.onnx").exists() and (MODEL_DIR / "tokenizer.json").exists()


def load_embedder(preferred: str | None = None):
    """按优先级加载嵌入提供者：onnx（模型就绪时）> hash。"""
    choice = preferred or os.environ.get("MEMORY_EMBED_PROVIDER", "auto")
    if choice in ("auto", "onnx") and model_files_present():
        try:
            return OnnxEmbedder(MODEL_DIR)
        except Exception as exc:  # 模型损坏或缺依赖时退回 hash，保证服务可用
            _log(f"ONNX 嵌入加载失败，退回 hash 模式：{exc}")
    elif choice == "onnx" and not model_files_present():
        _log("模型文件不存在（先运行 python3 download_model.py），退回 hash 模式")
    return HashEmbedder()
