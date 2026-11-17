# 盲态试验 · 验证记录

工作目录：/workspace（无 Docker 守护进程的本地容器内，使用 SQLite + TestClient + Playwright 渲染替身）。

## 已执行
| 检查 | 命令 | 结果 |
|---|---|---|
| 盲态试验后端用例（10 项） | `python3 test-artifacts/gb-1017/tests/test_trials.py` | 全部 PASS |
| 全流程 API（120 件、4 层、结案揭盲、两组估计） | 手工脚本见下方说明 | PASS |
| Demo seed 幂等（连跑两次计数一致） | `python3 -m scripts.seed_demo` ×2 | PASS |
| 试验页渲染（开验表单/待入组/随访中/结案按钮/揭盲样本/估计） | 渲染替身脚本 | 13/13 PASS |

未执行：真实 Docker Compose 五服务启动、Nginx 反代下的浏览器端到端与截图（宿主缺少 Playwright 浏览器依赖且无 sudo）。此项不能记为通过。

## 已覆盖的验收点
- 开验锁定：分层重复、非等比、观察员开验均被拒绝；响应与列表中不含种子。
- 盲态：未入组槽位不落库、无接口可查；结案前 `/groups`、`/estimates` 返回 409；观察员在被试随访中看不到该件分组。
- 分配：每层每 4 件区组 A/B 各 2，40 件总体 20/20；后续区组长度秘密浮动但保持等比。
- 并发：8 线程同时入组 30 件，全部成功且层内总平衡 15/15。
- 重试/重复：相同 request_key 返回原分组并标记 replayed；同资产换 request_key 409；同 serial 换新资产 409。
- 中断恢复：游标与入组同事务提交，下一件从下一个未用槽位继续。
- 结案：有待入组或未登记观测时 409；补齐后结案冻结版本快照，整案揭盲生成两个 Dataset；重复结案 replayed=true 不重复建批。
- 补报：历史版本 append-only，版本号递增；相同 request_key 重试不再写入；结案后补报 409。
- 权限：仅 admin 可结案；viewer 不能开验/入组/补报。
