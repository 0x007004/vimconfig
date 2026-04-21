# Redis + RocksDB 冷热探测与迁移方案（TP99 优化）

## 1. 目标与边界

### 目标
- 降低业务链路 TP99 / TP999。
- 识别“对延迟影响大”的数据并提升到 Redis。
- 在内存与网络预算可控前提下，持续执行升降级迁移。

### 非目标
- 不追求全量强一致事务语义。
- 不替代业务主存储（RocksDB 仍是权威数据源）。

## 2. 核心思路

不是单纯看“热度（QPS）”，而是看“迁移收益密度”：

```text
score = qps_ewma * (p99_rocks - p99_redis) * miss_penalty
        / (value_size_bytes * (1 + write_churn))
```

解释：
- 热度高：被访问多，潜在收益高。
- 岩盘慢：RocksDB 读延迟越高，提升到 Redis 收益越高。
- miss 代价高：回源和链路放大严重时更值得优先。
- 数据大：Redis 内存成本高，需降权。
- 写抖动高：频繁写会稀释缓存收益，需降权。

## 3. 系统分层

1. 指标采集层（只读）  
   - key / 前缀访问频率（EWMA）
   - RocksDB / Redis p99 延迟
   - value size
   - 写入频率（write churn）
   - Redis 内存水位、eviction、回源率

2. 决策引擎（本仓库 `hot_cold_probe.py`）  
   - score 计算
   - 热/冷连续周期（streak）判定，抑制抖动
   - 受内存预算、网络预算、每轮动作上限约束

3. 迁移执行层（业务系统接入）  
   - Promote: Rocks -> Redis 预热
   - Demote: Redis -> 过期淘汰或直接删除

4. 安全保护层  
   - 内存高水位限速
   - 每轮迁移上限
   - 网络预算上限
   - 异常回滚（停迁移，仅保留读回填）

## 4. 读写一致性策略（建议）

### 读路径
1. 先读 Redis；
2. miss 时回源 RocksDB；
3. 回填 Redis（可带随机 TTL 防雪崩）。

### 写路径
- 推荐：`write-through-ish` 简化版  
  1) 先写 RocksDB（权威）  
  2) 删除 Redis 对应 key（或更新 Redis + 版本号）

当一致性要求高时：
- 缓存 value 附带 `version`（逻辑时钟）；
- 回填时仅在版本更高时覆盖，避免旧值回写。

## 5. 灰度上线步骤

1. Shadow 模式：只采集与打分，不迁移。  
2. Read-fill 模式：只允许 miss 回填，不主动迁移。  
3. 小流量主动迁移：按租户 / key 前缀灰度。  
4. 全量迁移：启用自适应阈值与自动限速。  

## 6. 关键参数默认建议

- `hot_cycles_required = 2`：连续 2 个周期热才提升。
- `cold_cycles_required = 3`：连续 3 个周期冷才降级。
- `redis_promotion_budget_bytes`：每轮可新增 Redis 内存预算。
- `migration_network_budget_bytes`：每轮迁移网络预算。
- `max_promotions_per_cycle` / `max_demotions_per_cycle`：动作限流。

## 7. 失败与降级处理

- Redis 命中率下降 + RocksDB TP99 上升：立即暂停主动迁移。
- Redis 内存高水位：仅允许 demote，禁 promote。
- 网络预算耗尽：本轮停止迁移，下轮重试。
- 迁移任务异常：不影响在线读写，异步补偿。

## 8. 本仓库 PoC 说明

代码文件：
- `hot_cold_probe.py`：策略引擎与计划生成。
- `test_hot_cold_probe.py`：单元测试。

运行方式：

```bash
python -m unittest -v
python hot_cold_probe.py
```

该 PoC 目前输出“迁移计划”，不直接操作 Redis/RocksDB 客户端。  
业务接入时只需将 `MigrationAction` 映射到你的执行器即可。
