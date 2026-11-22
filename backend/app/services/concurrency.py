"""领域对象之间的串行化开关。

快照退役和投递回执会同时改写同一批投递记录（例如“废弃请求”和“完成回执”
几乎同时到达）。内存仓库没有数据库事务，用一把可重入锁把“判定终态 + 写回”
包成一个临界区，保证同一笔投递只落一个终态。
"""
from __future__ import annotations

import threading

DOMAIN_LOCK = threading.RLock()
