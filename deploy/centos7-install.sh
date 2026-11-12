#!/usr/bin/env bash
set -euo pipefail
umask 077

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$project_dir"

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    echo "此安装脚本适用于 Linux x86_64。" >&2
    exit 1
fi

for tool in curl tar gzip flock mktemp sha256sum; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "缺少命令 $tool，请先安装后重试。" >&2
        exit 1
    fi
done

mkdir -p -- "$project_dir/.runtime" "$project_dir/.local"
chmod 700 -- "$project_dir/.local"
exec 9>"$project_dir/.runtime/install.lock"
if ! flock -n 9; then
    echo "另一个安装进程正在运行，请等待其结束。" >&2
    exit 1
fi

uv_path="$project_dir/.runtime/uv/uv"
if [[ ! -x "$uv_path" ]]; then
    uv_version=0.13.7
    uv_package=uv-x86_64-unknown-linux-gnu
    uv_sha256=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83
    uv_work=$(mktemp -d "$project_dir/.runtime/uv-install.XXXXXX")
    trap 'rm -rf -- "$uv_work"' EXIT
    uv_archive="$uv_work/$uv_package.tar.gz"
    echo "正在下载 uv $uv_version，将显示进度；单次下载最多等待 180 秒。"
    curl --fail --show-error --location --progress-bar \
        --connect-timeout 10 --max-time 180 --retry 2 \
        "https://releases.astral.sh/github/uv/releases/download/$uv_version/$uv_package.tar.gz" \
        --output "$uv_archive"
    printf '%s  %s\n' "$uv_sha256" "$uv_archive" | sha256sum --check -
    tar -xzf "$uv_archive" -C "$uv_work" -- "$uv_package/uv" "$uv_package/uvx"
    chmod 755 -- "$uv_work/$uv_package/uv" "$uv_work/$uv_package/uvx"
    "$uv_work/$uv_package/uv" --version >/dev/null
    mkdir -p -- "$project_dir/.runtime/uv"
    mv -- "$uv_work/$uv_package/uvx" "$project_dir/.runtime/uv/uvx"
    mv -- "$uv_work/$uv_package/uv" "$uv_path"
fi
"$uv_path" --version

export UV_PYTHON_INSTALL_DIR="$project_dir/.runtime/python"
export UV_CACHE_DIR="$project_dir/.runtime/uv-cache"
export AGY_CLI_DISABLE_AUTO_UPDATE=true

if [[ ! -e "$project_dir/.venv" ]]; then
    "$uv_path" venv --python 3.11 --managed-python --seed "$project_dir/.venv"
fi
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    echo "现有 .venv 不是 Linux 虚拟环境。请使用新的源码目录安装。" >&2
    exit 1
fi
"$project_dir/.venv/bin/python" -c \
    'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "项目需要 Python 3.11 或更高版本。")'
"$uv_path" pip install --python "$project_dir/.venv/bin/python" \
    --python-platform x86_64-manylinux_2_17 --only-binary :all: \
    --requirement "$project_dir/requirements.txt"

# The official musl artifact is static and avoids CentOS 7's glibc limit.
"$project_dir/.venv/bin/python" - "$project_dir" <<'PY'
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

root = Path(sys.argv[1])
manifest_url = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/"
    "manifests/linux_amd64_musl.json"
)
with tempfile.TemporaryDirectory(prefix="agy-install-", dir=root / ".runtime") as work:
    archive_path = Path(work) / "agy.tar.gz"
    for attempt in range(1, 4):
        try:
            manifest_request = urllib.request.Request(
                manifest_url, headers={"Cache-Control": "no-cache"}
            )
            with urllib.request.urlopen(manifest_request, timeout=30) as response:
                manifest = json.load(response)
            asset_url = manifest["url"]
            expected_hash = manifest["sha512"].lower()
            if not asset_url.startswith("https://storage.googleapis.com/antigravity-public/"):
                raise RuntimeError("Antigravity 下载清单包含未知的下载地址。")
            if not re.fullmatch(r"[0-9a-f]{128}", expected_hash):
                raise RuntimeError("Antigravity 下载清单缺少有效的 SHA512。")

            digest = hashlib.sha512()
            received = 0
            print(f"正在下载官方 Antigravity CLI musl 版本，第 {attempt}/3 次…", flush=True)
            asset_request = urllib.request.Request(
                asset_url, headers={"Cache-Control": "no-cache"}
            )
            with urllib.request.urlopen(asset_request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"Antigravity 下载返回 HTTP {response.status}。")
                content_length = response.headers.get("Content-Length")
                with archive_path.open("wb") as archive_file:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        received += len(chunk)
                        archive_file.write(chunk)
            if content_length and received != int(content_length):
                raise RuntimeError(
                    f"Antigravity 下载不完整，收到 {received}/{content_length} 字节。"
                )
            if digest.hexdigest() != expected_hash:
                raise RuntimeError(f"Antigravity SHA512 校验失败，收到 {received} 字节。")
            break
        except (OSError, http.client.IncompleteRead, ValueError, RuntimeError) as error:
            if attempt == 3:
                raise RuntimeError(f"Antigravity 下载重试 3 次后失败。{error}") from error
            print(f"{error} 将重新获取下载清单并重试。", flush=True)
            time.sleep(attempt * 2)

    staged_binary = Path(work) / "agy"
    with tarfile.open(archive_path, "r:gz") as archive:
        member = archive.getmember("antigravity")
        if not member.isfile():
            raise RuntimeError("Antigravity 安装包缺少可执行文件。")
        with archive.extractfile(member) as source, staged_binary.open("wb") as target:
            shutil.copyfileobj(source, target)
    staged_binary.chmod(0o755)
    subprocess.run([str(staged_binary), "--version"], check=True, timeout=30)
    binary_dir = root / ".runtime" / "agy" / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    os.replace(staged_binary, binary_dir / "agy")
print("Antigravity CLI 已安装。", flush=True)
PY

if [[ ! -e "$project_dir/.local/server.env" ]]; then
    cat >"$project_dir/.local/server.env" <<'ENV'
QQBOT_APP_ID=''
QQBOT_APP_SECRET=''
# QQBOT_MODEL=''
ENV
fi
chmod 600 -- "$project_dir/.local/server.env"
"$project_dir/.venv/bin/python" "$project_dir/bridge.py" version
echo "运行环境安装完成。请填写 .local/server.env，并按 README 完成 agy 登录和收发测试。"
