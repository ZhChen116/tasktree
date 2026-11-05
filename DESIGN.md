# TaskTree 设计文档

这份文档用一份小型后端系统设计清单里的 8 个设计问题，说明 TaskTree 为什么长成现在这样。它和经典的"积分奖励"练习题（Rewards Checker）是同一个骨架：**一串事件流进来 → 逐个应用 → 更新数据库里的状态 → 有人来查**。不同的是，这里的"状态"是一棵树，"producer"是好几个同时在跑的 Claude session。

## 0. 先看一个具体例子

某一天，几个对话框里发生了这些事（简化）：

| 时间 | 谁写的 | 事件 | 树上的变化 |
|---|---|---|---|
| 10:36 | 论文 A 的 session | `update_node(paper.a.table, status=done)` | 补充材料里的表格完成 |
| 12:37 | 论文 A 的 session | `add_note(paper.a, "代码修复已上传")` | 论文 A 节点多一条带时间的记录 |
| 14:10 | 论文 B 的 session | `ask_decision(paper.b.run, options=[开始, 先不])` | 论文 B 这一枝变成"等你决定" |
| 14:10 | 论文 B 的 session（网络抖动，重发） | 同一个 `event_id` 再发一次 | 什么都不变，返回 duplicate |
| 21:40 | 总控 session | `answer_decision(paper.b.run, "开始")` | 决定完成，论文 B 回到"在做" |

最后问"我今天做了什么"，就是把 11 月 4 日的事件按时间列出来；问"现在卡在哪"，就是把树上没完成的叶子按状态分组。

## 1. Q1 事件是什么，从哪里来

六种事件：`create_node`、`update_node`、`add_note`、`move_node`、`answer_decision`、`archive_node`。每个事件有：

- `event_id`：idempotency key（去重键）。调用方可以自己给，比如重试时沿用同一个；不给就随机生成。
- `ts`：本地时间（带时区）和对应的 epoch 秒数。
- `type`、`payload`（比如 `{"id": "paper.a", "text": "..."}`）、`source`（哪个 session 写的）。

来源有三个：每个 Claude session 通过 MCP 工具写；ZH 在终端用 `python3 -m tasktree` 写；viewer 页面上点的决定，由 Claude 读回来后写成 `answer_decision`。

## 2. Q2 状态，以及按什么 key 存

| 表 | 四类状态里的哪一类 | key |
|---|---|---|
| `events` | 处理记录，也是唯一的真相（source of truth） | `event_id` UNIQUE；`seq` 决定重放顺序 |
| `nodes` | 业务状态：树的当前样子（materialized view） | `id`，例如 `job.x.prep` |
| `notes` | 产出记录：每条带时间的笔记 | 每行对应一个 `event_id` |

没有"消费位置"（offset）这一类，因为这里没有 feed 文件：事件一到就在同一个事务里应用，不存在"读到哪了"的问题。

为什么同时存 `events` 和 `nodes`：`nodes` 里的 `status` 是被覆盖写的，从 `doing` 改成 `done` 之后，旧值就没了。`events` 保留了完整历史，所以既能回答"它现在是什么状态"，也能回答"它是什么时候、被谁改成这样的"。这就是 event sourcing（事件溯源）；`nodes` 是为了查询快而缓存的计算结果。

## 3. Q3 Invariants（不能被破坏的规则）

- **I1**：同一个 `event_id` 最多应用一次。用 `events.event_id UNIQUE` 加 `INSERT OR IGNORE` 保证。同一个 `event_id` 配上不同内容会被拒绝，因为那说明调用方把 key 用错了。
- **I2**：`nodes` 和 `notes` 永远等于"把 `events` 从头重放一遍"的结果。所有写入都在同一个事务里；`rebuild` 命令可以随时重放来验证。
- **I3**：树没有环，每个 `parent` 都存在。由 `decide` 在移动和新建时检查。
- **I4**：被拒绝的事件什么都不改，也不进日志。
- **I5**：多个 session 同时写，不丢更新。

## 4. Q4 需要明确的语义

- **父节点的状态怎么算**：叶子显示自己的状态；父节点显示"未完成子节点"和"自己"之中最紧急的状态；子节点全部完成时，父节点算完成，除非它自己明确标着在做、卡住或等你决定（说明还有没拆出来的工作）。紧急程度：等你决定 > 卡住 > 在做 > 等待 > 完成。这和作者原有的项目树页面规则一致。
- **删除**：不删除，只归档（`archive_node`）。历史一直保留在日志里。
- **决定**：决定节点带 `options`，答案必须是其中之一；答完就标记为完成。
- **"今天"**：按本地日期（America/Chicago）算，不按 UTC。

## 5. Q5 部件和数据归属

```text
Claude session A ─┐
Claude session B ─┼─ MCP (stdio) ─▶ store.apply_event ─▶ SQLite (~/.tasktree/tasktree.db)
终端 CLI ─────────┘                                         │
                                                            ▼ export_viewer
                                         Claude artifact 页面（只读视图，手机可看）
```

- `store.py` 是唯一写 SQL 的模块。
- 每个 Claude session 会启动自己的 MCP server 进程，所以**写入者不止一个**。这一点和 Rewards Checker 的 single writer 不同，因此需要 Q6 里的锁。
- 数据库文件放在仓库之外（`~/.tasktree/`），公开的代码仓库里不会有任何个人数据。

## 6. Q6 漏洞清单（10 类常见漏洞）

| # | 会不会遇到 | 本系统的防法 |
|---|---|---|
| 1 重复事件 | 会：工具调用超时后 Claude 可能重发 | `event_id` UNIQUE，`INSERT OR IGNORE` 是事务第一句 |
| 2 并发写入 | 会：多个 session 同时写 | 每次写入是一个 `BEGIN IMMEDIATE` 事务，先拿写锁再读节点；`busy_timeout` 30 秒排队。由于事务第一句就是写（去重插入），读节点一定发生在拿到写锁之后 |
| 3 中途崩溃 | 会：进程被 kill | 事件、节点、笔记在同一个事务里，要么都写入，要么都不写 |
| 4 重启 | 不太相关：没有 offset | 状态全在 SQLite；`rebuild` 能从日志恢复视图 |
| 5 坏输入 | 会：Claude 传错字段 | `decide` 校验后抛 `InvalidEvent`，作为工具错误返回给 Claude，服务器不崩溃 |
| 6 半行 | 部分：stdio 收到坏 JSON | 返回 JSON-RPC parse error，继续读下一行 |
| 7 时间 | 会："今天"的边界 | 同时存本地时间和 epoch，按本地日期查询；测试用固定时钟 |
| 8 状态无限增长 | 慢慢会 | 一年也就几千条事件，SQLite 足够；以后需要时可以给旧事件做快照 |
| 9 外部副作用 | 有：同步到 artifact 页面 | 页面只是只读副本；同步失败不影响本地数据，下次重新导出全量即可 |
| 10 钱 | 不涉及 | — |

## 7. Q7 核心函数

```text
apply_event(conn, event) -> Result          # store.py，一个事务
    BEGIN IMMEDIATE
    INSERT OR IGNORE INTO events(...)       # 去重；rowcount == 0 → duplicate
    change = decide(event, get_node, ts)    # model.py，纯函数，只做校验和计算
    写 nodes / notes
    COMMIT（任何异常 → ROLLBACK）

decide(event, get, now) -> Change           # 纯函数：不碰数据库
rollup(nodes, id) -> status                 # 纯函数：从叶子往上汇总
rebuild(conn)                               # 清空 nodes/notes，按 seq 重放全部事件
```

## 8. Q8 测试

`tests/test_tasktree.py` 的每个测试对应一条 invariant：同一事件发两次只生效一次（I1）；`event_id` 被用于不同内容时拒绝（I1）；移动成环被拒绝且状态不变（I3、I4）；重放日志得到同样的树（I2）；8 个连接各写 25 条笔记，正好 200 条（I5）；两个连接同时改同一节点的不同字段，两边的最终值都保留（I5，lost update）；按本地日期取日志；MCP 握手、工具调用、工具错误、坏输入。

## 9. 和生产环境的对应

| TaskTree | 规模更大时 |
|---|---|
| SQLite 文件 + `BEGIN IMMEDIATE` | Postgres + 行锁或带条件的 `UPDATE` |
| `events` 表 | Kafka topic 或 event store |
| `nodes` 表 | 物化视图 / read model（CQRS，command query responsibility segregation） |
| artifact 页面同步 | 订阅事件流、增量更新的前端 |

参考：Kleppmann, *Designing Data-Intensive Applications*，"Transactions" 和 "Stream Processing" 两章（事件日志、派生状态、不可变性）。
