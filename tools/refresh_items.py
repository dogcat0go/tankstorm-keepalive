#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""解开 CDN 上的 Item_版本.dat，写成页面用的道具表。

不带版本号时，按北京日期往回找最近三周里能解开的最新一份。
带了版本号就只解那一版，找不到也不改去猜别的。

结果写到程序目录的 items-live.json，不改仓库里的 web/src/items.json。
新出列表是这一份和上一份道具表的差。上一份优先用已经写好的表。

    python3 tools/refresh_items.py
    python3 tools/refresh_items.py 2026111801
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tankstorm.items_catalog import refresh  # noqa: E402
from tankstorm.paths import user_path  # noqa: E402


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("用法：python3 tools/refresh_items.py [版本号]", file=sys.stderr)
        return 2
    try:
        if args:
            found, rows, fresh = refresh(version=args[0])
        else:
            found, rows, fresh = refresh()
    except Exception as exc:
        print(f"道具表没解开：{exc}", file=sys.stderr)
        return 1
    print(f"{found} 共 {len(rows)} 条，新出 {len(fresh)} 条，写到 {user_path('items-live.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
