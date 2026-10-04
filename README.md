# SQLite 事务隔离实验室

一个用于观察 SQLite（WAL 模式）事务隔离与写锁冲突行为的网页实验台。

- 后端：Python + Flask（直接调用标准库 `sqlite3`，真实执行 SQL）
- 前端：原生 HTML / CSS / JavaScript，无任何框架
- 每次「新建实验」都会得到一个独立的 WAL 数据库，内含单行整数值 `100`
- 页面左右两栏（甲 / 乙）各自绑定一条**长期独立连接**，可执行
  `BEGIN DEFERRED`、读值、加上输入整数、提交、回滚
- 第三条连接单独显示**已提交值**
- 状态刷新不执行任何 `SELECT`，不会为 DEFERRED 事务提前建立读快照
- 写锁冲突立即返回 SQLite 错误名与错误码（`busy_timeout=0`），
  不阻塞另一栏，也不会自动结束失败的事务
- 写锁冲突（`SQLITE_BUSY`）发生时事务尚未建立读快照：对方 `COMMIT` 或
  `ROLLBACK` 释放写锁后，**可在原事务直接重试写入**，无需回滚重开；
  一旦本事务显式读值（读快照建立），再与对方新提交冲突则报
  `SQLITE_BUSY_SNAPSHOT`，必须自行回滚重开（系统不代为回滚）
- 「加上」的内部边界检查不会在本连接提前建立读快照：无快照时基于
  observer 连接的已提交值预判，最终判定在 `UPDATE` 内的 `checked_add`
  函数中完成，因此不会把 `SQLITE_BUSY` 误报成 `SQLITE_BUSY_SNAPSHOT`
- 修改操作（加上）只允许在事务内进行，禁止事务外修改
- 页面提供 **PASSIVE / TRUNCATE 手动检查点按钮**，也可通过 HTTP 调用；
  每次检查点都由一条**全新的临时独立连接**真实执行
  `PRAGMA wal_checkpoint(PASSIVE|TRUNCATE)`，`busy_timeout=0`，
  执行完立即关闭，各连接关闭时保留 SQLite 默认的自动检查点行为
- 检查点返回并显示：检查点**模式**、SQLite 原始三项结果
  **(busy、WAL 总帧数、已回写帧数)**，以及操作后 **WAL 文件字节数**
  （`-wal` 文件不存在时大小记 0）
- 检查点不替甲、乙两栏提交 / 回滚，也不更换两栏连接；状态刷新只读内存
  缓存，不执行任何 `SELECT` 或 `PRAGMA wal_checkpoint`，**不会触发检查点**
- 实验仅保存在内存中，无需跨重启保留

## 数值约定

- 存储值为 SQLite 有符号 64 位整数（`-9223372036854775808` ～ `9223372036854775807`），
  正负边界均不会转成浮点数
- 单次增量上限为 ±2⁶²（`4611686018427387904`），超过即被 API 拒绝（`value too large`）
- 累加结果若超出 64 位整数范围，该次「加上」会被拒绝（SQLite 对整数溢出不会报错，
  而是静默存成 REAL，本实验在写入前做精确边界检查）；已开启的事务不受影响，
  之前的修改保留，仍可提交或回滚
- 传输编码：超出 JavaScript 安全整数范围（±2⁵³−1）的整数一律用十进制字符串传输，
  范围内仍返回 JSON 数字；「加上」请求的 `value` 字段两种形式都接受，
  旧的小整数数字请求照常可用
- 输入、操作响应、状态轮询、最近一次显式读值、日志与已提交值均无损显示完整十进制值

## 启动

```bash
docker compose up --build
```

访问：<http://localhost:8000>

## 操作示例

### 示例一：快照隔离与快照冲突（SQLITE_BUSY_SNAPSHOT）

1. 甲：`BEGIN DEFERRED` → `读值` → 显示 **100**
2. 乙：`BEGIN DEFERRED` → 输入 `10` → `加上` → `提交`
   （顶部「已提交值」变为 **110**）
3. 甲：`读值` → 仍为 **100**（甲的读快照不受乙已提交的修改影响）
4. 甲：`加上` → 立即报错 **SQLITE_BUSY_SNAPSHOT（错误码 517）**，甲的事务仍然开启
5. 甲：`回滚` → `BEGIN DEFERRED` → `读值` → 显示 **110**

### 示例二：写锁冲突（SQLITE_BUSY）——提交释放写锁后在原事务重试

1. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上`（先不提交，甲持有写锁）
2. 乙：`BEGIN DEFERRED`（不显式读值）→ 输入 `7` → `加上` →
   立即报错 **SQLITE_BUSY（错误码 5）**，乙的事务不会被自动结束
3. 甲：`提交`（释放写锁，已提交值变为 **105**）
4. 乙：**在原事务中**再次输入 `7` → `加上` → 成功（不会报
   SQLITE_BUSY_SNAPSHOT）→ `提交`（已提交值变为 **112**）

> 修复前，「加上」内部的边界检查会先执行一条 SELECT，提前为乙建立读快照：
> 第 2 步拿到 SQLITE_BUSY 后快照已经留下，第 4 步会被误报为
> SQLITE_BUSY_SNAPSHOT（517）。现在内部检查不在乙的连接上读值，
> 写锁冲突不建立读快照，原事务即可直接重试。

若第 3 步甲改为 `回滚`，第 4 步乙重试并提交后值为 **107**（甲的 +5 被撤销）。

### 示例二补充：显式读值后冲突 → 必须自行回滚重开（SQLITE_BUSY_SNAPSHOT）

1. 乙：`BEGIN DEFERRED` → `读值` → 显示 **100**（读快照建立）
2. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上` → `提交`
3. 乙：输入 `7` → `加上` → 立即报错 **SQLITE_BUSY_SNAPSHOT（错误码 517）**，
   乙的事务仍开启，系统**不会**自动回滚、不替换连接、也不改变事务类型
4. 乙：`回滚` → `BEGIN DEFERRED` → 输入 `7` → `加上` → `提交`
   （已提交值变为 **112**）

### 示例二补充二：读事务阻止 WAL 回收（PASSIVE 回写未完成 / TRUNCATE busy）

观察一个尚未结束的读事务怎样阻止检查点回收 WAL。

1. 甲：`BEGIN DEFERRED` → `读值` → 显示 **100**（甲建立读快照，WAL 读位置停在 0）
2. 乙：`BEGIN DEFERRED` → 输入 `10` → `加上` → `提交`
   （顶部「已提交值」变为 **110**，WAL 新增 1 个帧）
3. 点击 **PASSIVE 检查点**：显示
   `busy=0，WAL总帧数=1，已回写帧数=0，WAL 文件 4152 字节` ——
   **回写未完成**：甲的读事务还在用旧版本，SQLite 不能把该帧覆盖进主库，
   PASSIVE 不等待也不报错
4. 点击 **TRUNCATE 检查点**：**立即**返回
   `busy=1，WAL总帧数=1，已回写帧数=0，WAL 文件 4152 字节` ——
   帧仍未回写，无法独占重启 / 截断 WAL（`busy_timeout=0`，不阻塞等待）
5. 甲：`读值` → 仍是 **100**（甲的快照不受乙的提交和检查点影响）
6. 甲：`回滚`（读事务结束，已提交值仍是 **110**）
7. 再次点击 **TRUNCATE 检查点**：得到
   `busy=0，WAL总帧数=0，已回写帧数=0，WAL 文件 0 字节`（WAL 被回写并截断；
   计数为 0 是截断成功后 wal-index 头被重启清零的原始返回），
   顶部「已提交值」仍为 **110**

> 说明：三项数字直接取自 SQLite `PRAGMA wal_checkpoint` 返回行的原始整数：
> 第 1 列 busy（是否因活动读者未完成），第 2 列 WAL 总帧数，第 3 列已回写帧数；
> WAL 字节数为操作结束后磁盘上 `exp_<id>.db-wal` 的大小，文件不存在记 0。
> 检查点由临时独立连接执行，不会替甲、乙提交或回滚，也不更换两栏连接；
> 页面状态轮询只读服务端内存里的最近结果，不会重新执行检查点。

#### 对应的 HTTP 调用示例

先记下「实验 ID」（或从新建实验接口获取，下文以 `$E` 代替）：

```bash
# 新建实验，拿到 12 位 ID
curl -s -X POST http://localhost:8000/api/experiments
# {"id":"a1b2c3d4e5f6"}
E=a1b2c3d4e5f6
BASE=http://localhost:8000/api/experiments/$E

# 1. 甲：BEGIN DEFERRED → 读值（读到 100，读快照建立）
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' -d '{"op":"read"}'

# 2. 乙：+10 并提交
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' -d '{"op":"add","value":10}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' -d '{"op":"commit"}'
curl -s $BASE/committed            # {"committed":110}

# 3. PASSIVE：回写未完成（甲的读事务还在）
curl -s -X POST $BASE/checkpoint -H 'Content-Type: application/json' -d '{"mode":"PASSIVE"}'
# {"ok":true,"error":null,
#  "result":{"mode":"PASSIVE","busy":0,"total_frames":1,
#            "checkpointed_frames":0,"wal_bytes":4152}}

# 4. TRUNCATE：立即 busy=1，WAL 保持 4152 字节
curl -s -X POST $BASE/checkpoint -H 'Content-Type: application/json' -d '{"mode":"TRUNCATE"}'
# {"ok":true,"error":null,
#  "result":{"mode":"TRUNCATE","busy":1,"total_frames":1,
#            "checkpointed_frames":0,"wal_bytes":4152}}

# 5/6. 甲仍读 100，然后回滚
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' -d '{"op":"read"}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' -d '{"op":"rollback"}'

# 7. 再次 TRUNCATE：(0,0,0)，WAL 文件 0 字节；已提交值仍为 110
curl -s -X POST $BASE/checkpoint -H 'Content-Type: application/json' -d '{"mode":"TRUNCATE"}'
# {"ok":true,"error":null,
#  "result":{"mode":"TRUNCATE","busy":0,"total_frames":0,
#            "checkpointed_frames":0,"wal_bytes":0}}
curl -s $BASE/committed            # {"committed":110}
```

查看最近一次检查点结果与检查点操作日志（只读缓存，不触发检查点）：

```bash
curl -s $BASE/status | python3 -m json.tool
# ... "checkpoint": { "last": { "mode": "TRUNCATE", "busy": 0,
#   "total_frames": 0, "checkpointed_frames": 0, "wal_bytes": 0 },
#   "log": [ ... ] }
```

`mode` 只接受 `PASSIVE` 或 `TRUNCATE`（大小写敏感），其他值返回
`400 {"error":"mode must be PASSIVE or TRUNCATE"}`。

### 示例三：大整数精度（9007199254741093）

1. 甲：`BEGIN DEFERRED` → 输入 `9007199254740993`（超出 JavaScript 安全整数范围，
   前端会以十进制字符串发送）→ `加上` → `提交`
2. 顶部「已提交值」显示 **9007199254741093**（= 100 + 9007199254740993，精确）
3. 甲：`BEGIN DEFERRED` → `读值` → 同样显示 **9007199254741093**
   （修复前因 `parseInt` 精度丢失会少 1，显示 9007199254741092）

### 示例四：64 位溢出拒绝（不会存成 REAL）

1. 甲：`BEGIN DEFERRED` → 输入 `4000000000000000000` → `加上` → 成功
   （当前值 4000000000000000100）
2. 甲：再次 `加上` → 成功（当前值 8000000000000000100）
3. 甲：第三次 `加上` → 被拒绝：**累加结果超出有符号 64 位整数范围，本次未修改**
   （修复前 SQLite 会静默存成浮点数 `1.2e+19`）
4. 甲的事务仍然开启，前两次结果保留：可 `提交`
   （已提交值变为 **8000000000000000100**），也可 `回滚`（已提交值保持 100）

> 提示：一栏的请求未完成时，该栏按钮会被禁用，另一栏仍可正常操作；
> 两栏的状态轮询与「已提交值」均通过独立连接 / 内存状态完成，
> 不会干扰甲、乙两栏各自的事务快照。
