#!/bin/bash
# macOS 双击启动：检查 Python 版本，第一次运行时生成 config.toml，打开看板并开始监控。
cd "$(dirname "$0")" || exit 1

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "没有找到 Python 3.11 或更高版本。"
  echo "请先安装：brew install python@3.12（或从 https://www.python.org/downloads/ 下载）"
  read -r -p "按回车退出" _
  exit 1
fi

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "第一次运行：已生成 config.toml。"
  echo "浏览器打开后，在「监控内容和推送」里填邮编、勾选型号、填 Bark 地址，点保存即可。"
fi

PORT=$(grep -E '^\s*port\s*=' config.toml | head -1 | tr -dc '0-9')
(sleep 2 && open "http://127.0.0.1:${PORT:-8765}") &
"$PY" -m duo_restock run
read -r -p "监控已停止，按回车退出" _
