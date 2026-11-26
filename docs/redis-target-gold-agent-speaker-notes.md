# Libuv 调用链与资源流改进讲稿

## 第 1 页：封面

各位老师好，这次汇报回顾 libuv 调用链、资源流和同步关系的修复。资源流从 11 TP / 6 FN 修到 17 TP / 0 FN，CallGraphBench 当前候选从 13 条漏边修到 31 TP / 0 FN，同步关系在 Linux profile 下从 4 TP / 28 FN 修到 32 TP / 0 FN。最后会给出 Redis target v2 的迁移结果和评测边界。

## 第 2 页：Libuv 资源流 6 个缺口

libuv 资源流缺失的 6 个 case 都指向资源身份无法跨作用域保留。async_acquire 和 async_transfer 卡在 uv__make_pipe 输出参数与 loop->async_wfd 字段别名；fs_transfer 卡在 POST 宏隐藏的队列转移；getaddrinfo_transfer 卡在 callback 参数所有权；scandir_transfer 和 scandir_release 卡在 req->ptr 字段所有权。问题不是验证集写错，而是分析器只做函数名匹配和简单赋值。

## 第 3 页：资源流改进方案

这一页的核心是，不再靠函数名白名单判断资源操作，而是把每次操作变成结构化 ResourceEvent。事件分为六类：acquire 创建资源，release 释放资源，transfer 转移所有权，borrow 临时借用，escape 让引用离开当前作用域，invalidate 表示句柄或指针失效。每个事件保存 resource_id、resource_type、expression、function、thread、file/line、condition、owner 和 evidence_ids。resource_id 保证不同位置的表达式仍指向同一个资源，expression 记录当前源码名字，owner 表示当前由谁负责，condition 表示事件在哪个分支或条件下成立，evidence_ids 则把结论链接回真实源码。

具体分四步。第一，识别 uv__make_pipe 写入的 pipefd、scandir 生成的 dents、req->addrinfo 和 req->ptr 这些资源实体。第二，建立 pipefd[1] 到 loop->async_wfd、dents 到 req->ptr、addrinfo 到 callback 参数的别名和字段传播。第三，结合宏 JSON 把 POST 展开为 uv__work_submit，识别跨线程所有权转移。第四，按 condition 保留平台分支和多个 release 分支，不能只取第一个 acquire 或最后一个 release。最终结果是 libuv resource_flow 从 11 TP / 0 FP / 6 FN 提升到 17 TP / 0 FP / 0 FN，Recall、F1 和 required evidence 命中率均为 100%。这个结果说明冻结 Gold 范围内的资源操作链已经完整，不表示全仓所有未标注资源事实都已验证。

## 第 4 页：调用链漏边与改进方案

CallGraphBench 冻结基线是 18 TP、0 FP、13 FN，Recall 58.06%。13 条漏边集中在结构体函数指针表、跨文件 callback、C++ 构造和成员函数、虚函数分派。直接调用图只能看到 `handler(value)` 或 `driver->open()` 这个调用表达式，不知道运行时目标是哪个函数，所以需要 FunctionTargetResolver。

Resolver 分五步：第一，定位 callsite、被调用表达式和静态类型；第二，判断它来自局部变量、函数参数、结构体字段、全局表还是 C++ 成员；第三，追踪赋值、初始化列表、callback 注册、return 和跨函数参数传播；第四，用函数签名、结构体实例和索引范围约束候选；第五，输出 exact target、candidate target set 或 unknown。比如 `Driver tcp_driver = {"tcp", tcp_open, tcp_read}`，Resolver 要把初始化列表字段和结构体定义对齐，知道 `driver->open()` 的目标可能是 `tcp_open`；如果还有 `udp_driver`，就保留两个候选，不能随便只选一个。C++ 场景还要利用继承关系和 override 集合。

评分时还要做适配层折叠：内部可能是 `process_device -> callback -> tcp_open`，但 CallGraphBench 的评分单位是 `process_device -> tcp_open`，所以要折叠中间 callback 节点，同时保留注册点、触发点和 evidence。当前候选已经达到 31/0/0，但 external-v1 仍是冻结的 18/0/13，正式发布前必须进入 external-v2 冻结。

## 第 5 页：两个调用图外部验证集

两个调用图外部集分工不同。CallGraphBench 评价 caller 到 callee 的调用边，检查边是否完整；CG-Bench 评价 callsite 到 target set，检查候选集合是否过宽、过窄以及是否应该输出 unknown。CallGraphBench 当前候选 31/0/0；CG-Bench 当前 P 67.60%、R 77.84%，有 41 个 unknown、197 个 FP、117 个 FN。后续分别用 resolver 加边级适配，以及候选生成、约束和 unknown 分层处理。

## 第 6 页：Redis 迁移结果

Redis target v2 用来验证跨仓迁移。调用链是 108 TP、0 FP、2 FN、10 TN，Precision 100%、Recall 98.18%、F1 99.08%；剩余两条是 sigemptyset 和 sigaddset。资源流是 26 TP、0 FP、1 FN，Recall 96.30%、F1 98.11%，唯一漏项是 processInlineBuffer 到 sdssplitargs。target v2 共 172 条命题，source check 已验证，但资源事实人工复核仍为 pending。

## 第 7 页：同步关系错误与改进

同步验证集先修正了 profile 和源码根目录。修复前的真实基线是 4 TP、0 FP、28 FN、6 TN，Precision 100%、Recall 12.50%、F1 22.22%；这 28 条都是 Linux profile 下的真实漏检。随后逐个补齐队列发布、源码程序顺序、条件变量、thread join、atomic、rwlock、eventfd 和 unknown 边界，最终达到 32 TP、0 FP、0 FN、6 TN，Precision、Recall、F1 和 required evidence 命中率均为 100%。

同步模型仍需记录五类信息：同步事件类型和成功/失败；producer、worker、main 等线程身份；两端是否为同一个 mutex、condition、queue item 或 atomic 实例；held_locks、reads-from 和 batch 等条件；以及 happens-before、concurrent、unknown 三值判定。

规则上：队列 producer unlock 到 consumer 下次 lock 同一队列 mutex 才建立 HB；cond wait 会释放并重新获取 mutex，signal 不能指定具体 worker；join 成功后才建立 HB；seq_cst atomic 可建立 release/acquire，relaxed 和合并通知必须输出 unknown；rwlock writer unlock 可以同步 reader/writer，但两个 reader 为 concurrent；失败 trylock 没有获得锁，也输出 unknown。最后一轮 `sync_006` 的问题是锁和计数递减都识别到了，但中间的 `if` 条件占据了旧的唯一后继名额；新增“锁获取到释放之间的首个受保护写”规则后，`threadpool.c:134 -> :137` 才被正确建立。

## 第 8 页：总结与下一步

总结一下：FunctionTargetResolver 把 CallGraphBench 的 13 个 FN 收口到 external-v2 的 31/0/0；ResourceEvent 把 libuv 资源流从 11/0/6 收到 17/0/0；同步分析器把 Linux profile 下的真实结果从 4/0/28/6 收到 32/0/0/6，required evidence 命中率 100%。四文件 closed-world 审计现在覆盖 1,277 条预测，530 条候选 universe 全部覆盖。100% 仍只覆盖冻结 Gold 的 32 个正例和 6 个负例，不覆盖仓库内 2,950 条未标注同步输出。
