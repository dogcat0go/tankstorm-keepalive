# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""试用锁的密文。空元组表示不限制。

打包时 spec 会按环境变量临时把密文写进这里，构建结束再还原。
TANKSTORM_LOCK_QQ 锁 QQ，TANKSTORM_LOCK_GUILD 锁公会。这里不留明文。
"""

SEED = ()
BLOB = ()
GUILD_SEED = ()
GUILD_BLOB = ()
# 授权包在编译时写入。空地址表示不限制时长。
LICENSE_URL = ""
GRACE_SEC = 0
LICENSE_USER_SEED = ()
LICENSE_USER_BLOB = ()
LICENSE_PASS_SEED = ()
LICENSE_PASS_BLOB = ()
