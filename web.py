# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""订阅页面。不连游戏，和 main.py 分开跑。

  python3 web.py
  python3 web.py --host 0.0.0.0 --port 8765

扫城仍用 main.py，例如 python3 main.py --watch-pages 1201:10-20。
两边读写同一份 city_players.db。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tankstorm.log import get_logger
from tankstorm.paths import data_file, ensure_user_copy, user_path
from tankstorm import webui

log = get_logger()

if ensure_user_copy("config.json"):
    log.info("已在程序目录生成 config.json，可直接编辑")


def _load_json(path: str, required: bool = True) -> dict:
    if not os.path.exists(path):
        if required:
            log.error("缺少文件: %s", path)
            sys.exit(1)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def main() -> int:
    parser = argparse.ArgumentParser(
        description="坦克风暴订阅页面（不连游戏）")
    parser.add_argument("--host", default="0.0.0.0", metavar="地址",
                        help="监听地址，默认 0.0.0.0。证书由反向代理处理")
    parser.add_argument("--port", type=int, default=8765, metavar="端口",
                        help="端口，默认 8765")
    args = parser.parse_args()
    config = _deep_merge(
        _load_json(data_file("config.json")),
        _load_json(user_path("config.local.json"), required=False))
    try:
        return webui.serve(args.host, args.port, config)
    except OSError as exc:
        log.error("订阅页面没能监听 %s:%s：%s", args.host, args.port, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
