# restart-permit-api

存储集群值班员用的**分批重启许可 API**。FastAPI + SQLite，单容器即可运行。

核心规则：

- 服务启动时读取**固定的分片投票副本清单**（`manifest.json`）。
- 节点探活报告携带**采样时间、健康状态、启动标识**；每个节点以**采样时刻最新**的一份为准。
  采样时刻按实际 UTC 时刻比较，**完整保留最多 9 位小数秒（纳秒）**；
  只有采样时刻真正相同（纳秒也相同）时才取**最后入库**的一份。
- 值班员提交**申请号 + 拟重启的一批节点**，满足以下条件才批准整批：
  1. 本批每个节点最新探活均健康；
  2. 本批节点未被任何**未结束许可**占用；
  3. 扣除 *本批节点 + 未结束许可占用节点 + 健康未知/异常节点* 后，本批涉及的**每个分片**剩余可用投票副本均**过半**；否则拒绝并列出受影响分片。
- **并发申请**在写事务（`BEGIN IMMEDIATE`）中串行判定，守住多数票。
- **同号同批重发**返回原许可（HTTP 200）；同号不同批返回 409。
- 许可与探活记录落盘 SQLite，**服务重启后保留**。
- **结束许可**要求本批每个节点的最新探活：采样时刻**严格晚于**获批时刻、健康、启动标识与获批时不同。
  获批时刻同样精确到纳秒；同一微秒内晚 1 纳秒的采样也算“获批后”。
  条件不满足时拒绝结单（409），许可保持 open，节点继续被占用。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互文档 http://localhost:8000/docs
```

- SQLite 数据在命名卷 `permit-data`（容器内 `/data/permits.db`），重启不丢。
- 分片清单 `./manifest.json` 只读挂载进容器；修改后需 `docker compose restart` 生效。

本地开发（不用 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DB_PATH=./data/permits.db MANIFEST_PATH=./manifest.json \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pytest tests/ -q
```

## 分片清单格式

```json
{
  "shards": {
    "shard-a": ["n1", "n2", "n3"],
    "shard-b": ["n2", "n3", "n4"],
    "shard-c": ["n3", "n4", "n5"]
  }
}
```

每个分片列出全部投票副本节点；过半线 = `floor(票数/2)+1`（3 票需 2 可用）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 服务健康检查 |
| GET | `/manifest` | 查看分片清单与各分片过半线 |
| POST | `/liveness` | 批量上报探活（201） |
| GET | `/liveness` | 全部节点最新探活 |
| GET | `/liveness/{node}` | 单节点最新探活 |
| POST | `/permits` | 申请许可（201 新批 / 200 幂等重发 / 409 拒绝） |
| GET | `/permits?status=open\|finished` | 许可列表 |
| GET | `/permits/{request_id}` | 单个许可 |
| POST | `/permits/{request_id}/finish` | 结束许可（200 / 409 条件不满足） |

## 调用示例

### 1. 上报探活

```bash
curl -X POST http://localhost:8000/liveness \
  -H 'Content-Type: application/json' -d '{
    "reports": [
      {"node_id":"n1","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n2","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n3","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n4","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n5","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"}
    ]}'
# => {"accepted":5}
```

### 2. 申请许可

```bash
curl -X POST http://localhost:8000/permits \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","nodes":["n1"]}'
# 201 =>
# {"request_id":"req-001","nodes":["n1"],"status":"open",
#  "approved_at":"...","finished_at":null,
#  "boot_ids_at_approval":{"n1":"boot-1"}}
```

同号同批重发，返回原许可（200，`approved_at` 不变）；同号不同批返回 409。

### 3. 多数票不足被拒（列出受影响分片）

`req-001` 已占用 n1 时再申请 n2（同属 shard-a，3 票最多扣 1 票）：

```bash
curl -X POST http://localhost:8000/permits \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-002","nodes":["n2"]}'
# 409 =>
# {"detail":{"ok":false,"error":"permit_rejected",
#   "affected_shards":[{"shard":"shard-a","voters":3,"required_available":2,
#     "available":1,"available_nodes":["n3"],
#     "excluded":{"n1":"occupied_by:req-001","n2":"in_batch"}}]}}
```

其它拒绝类别：`health_unknown_nodes`（健康未知）、`unhealthy_nodes`（异常）、
`occupied_nodes`（被未结束许可占用）。清单外节点一律 422。

### 4. 节点重启后上报新探活，结束许可

```bash
# 节点重启完成：采样时间晚于获批时间、健康、启动标识已变
curl -X POST http://localhost:8000/liveness \
  -H 'Content-Type: application/json' -d '{
    "reports":[{"node_id":"n1","sampled_at":"2026-11-19T10:20:00Z",
                "healthy":true,"boot_id":"boot-2"}]}'

curl -X POST http://localhost:8000/permits/req-001/finish
# 200 => {"request_id":"req-001",...,"status":"finished","finished_at":"..."}
```

结束条件不满足时 409 并给出逐节点原因，例如：

```json
{"detail":{"error":"finish_conditions_not_met",
           "nodes":{"n1":"boot_id_unchanged"}}}
```

可能原因：`no_liveness_report`、`not_sampled_after_approval`、`unhealthy`、`boot_id_unchanged`。
许可结束后节点被释放，可被后续申请占用。

## 设计说明

- **幂等**：`request_id` 是许可表主键。节点集合规范化（去重排序）后比较，
  同号同批重放直接返回原许可；同号不同批 409。被拒绝的申请不落库，整改后可用同号重试。
- **并发**：所有写路径走单连接 + 可重入锁 + `BEGIN IMMEDIATE` 事务，
  "检查多数票 → 写入许可"是原子的；并发申请串行判定，不会超批。
- **持久化**：许可与全部探活报告存 SQLite（WAL 模式）。采样时刻同时保存
  ISO8601 文本与 `sampled_at_ns` 纳秒整数列，最新探活按
  `sampled_at_ns DESC, id DESC` 实时计算，服务重启后状态完整恢复。
- **时间精度**：Python `datetime` 只有微秒精度，直接使用会把第 7–9 位小数秒截断。
  因此采样/审批/结束时间统一用 `app.models.UtcInstant`（Unix 纳秒整数）表示，
  解析、比较、排序、接口输出全程保留纳秒；API 返回 UTC ISO8601，最多 9 位小数秒。
- **旧库迁移**：启动时幂等补 `sampled_at_ns` 列，并按每条旧记录**已保存**的
  `sampled_at` 文本回填；不修改旧记录时刻，也不影响 `received_at` 语义。
- **结束判定**：批准时快照各节点 `boot_id`；结束时要求最新报告
  `sampled_at > approved_at` 且健康且 `boot_id` 不同。

## 验证纳秒结单（Docker Compose 环境）

`docker compose up --build` 之后，保存下面的脚本并运行
`python3 demo.py http://localhost:8000`（只用标准库）：

```python
# demo.py —— 验证纳秒采样、结单严格晚于获批、晚到旧报告不生效
import json, sys, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
NODES = ["n1", "n2", "n3", "n4", "n5"]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def shift_ns(instant, delta):
    """UTC ISO8601 上做整数纳秒加减，不经过浮点。"""
    text = instant.rstrip("Z").split("+")[0]
    whole, _, frac = text.partition(".")
    sec = int(datetime.fromisoformat(whole + "+00:00").timestamp())
    ns = sec * 10**9 + int(frac.ljust(9, "0")[:9] or 0) + delta
    s, f = divmod(ns, 10**9)
    stamp = datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{f:09d}+00:00"


seed = {"reports": [
    {"node_id": n, "sampled_at": "2026-11-19T10:00:00.000000001Z",
     "healthy": True, "boot_id": "boot-1"} for n in NODES]}
print("seed:", call("POST", "/liveness", seed))

# A) 获批后 1 纳秒采样 -> 查询保留纳秒，结单 200
status, p = call("POST", "/permits", {"request_id": "demo-1", "nodes": ["n1"]})
print("permit:", status, p["approved_at"])
after = shift_ns(p["approved_at"], +1)
print("report:", call("POST", "/liveness", {"reports": [
    {"node_id": "n1", "sampled_at": after, "healthy": True, "boot_id": "boot-2"}]}))
print("query :", call("GET", "/liveness/n1"))          # sampled_at 9 位小数秒
print("finish:", call("POST", "/permits/demo-1/finish"))  # 200 finished

# B) 同刻采样不算“晚于获批”；晚到旧报告不得伪造完成 -> 409 且仍占用
status, p2 = call("POST", "/permits", {"request_id": "demo-2", "nodes": ["n2"]})
a2 = p2["approved_at"]
call("POST", "/liveness", {"reports": [
    {"node_id": "n2", "sampled_at": a2, "healthy": True, "boot_id": "boot-2"}]})
print("same instant:", call("POST", "/permits/demo-2/finish"))  # 409 not_sampled_after_approval
newer, older = shift_ns(a2, +500), shift_ns(a2, +100)
call("POST", "/liveness", {"reports": [
    {"node_id": "n2", "sampled_at": newer, "healthy": True, "boot_id": "boot-1"}]})
call("POST", "/liveness", {"reports": [
    {"node_id": "n2", "sampled_at": older, "healthy": True, "boot_id": "boot-2"}]})
print("latest:", call("GET", "/liveness/n2"))            # boot-1（较新的报告胜出）
print("finish:", call("POST", "/permits/demo-2/finish"))   # 409 boot_id_unchanged
print("status:", call("GET", "/permits/demo-2"))           # open
print("reapply:", call("POST", "/permits",
                       {"request_id": "demo-3", "nodes": ["n2"]})[0])  # 409 仍占用
```

实测输出（本机以 compose 同样的启动方式运行）：

```
permit: 201 2026-11-20T19:47:15.645960481+00:00
report: (201, {'accepted': 1})
query : (200, {'sampled_at': '2026-11-20T19:47:15.645960482+00:00', ... 'boot-2'})
finish: (200, {... 'status': 'finished' ...})
same instant: (409, {'nodes': {'n2': 'not_sampled_after_approval'}})
latest: (200, {... 'sampled_at': '...658587622+00:00', 'boot_id': 'boot-1'})
finish: (409, {'nodes': {'n2': 'boot_id_unchanged'}})
status: open
reapply: 409
```

## 已知限制 / 未完成项

- 单 worker 运行（SQLite 单写者）；如需多副本需换外部数据库。
- 未做认证鉴权，假定部署在受信内网；如需可前置网关或加 token。
- 探活无"过期"概念：只要最新一份健康即视为健康，不判断采样时间新旧。
- 结束许可比较的是节点上报的采样时钟与服务端获批时钟，依赖各节点时钟基本同步（NTP）；
  纳秒精度让“同一微秒内”的先后可分辨，但仍无法纠正节点时钟本身的偏移。
- 采样时间小数位超过 9 位时按截断处理（ISO8601 上限即 9 位）。
- 许可无过期/自动回收机制；值班员需显式 finish。
