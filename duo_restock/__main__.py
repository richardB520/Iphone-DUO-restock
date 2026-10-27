import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"需要 Python 3.11 或更高版本，当前是 {sys.version.split()[0]}。\n"
        "macOS 自带的 python3 版本太旧，请先安装新版：brew install python@3.12\n"
        "或者从 https://www.python.org/downloads/ 下载安装，然后用 python3.12 -m duo_restock run"
    )

from .cli import main

sys.exit(main())
