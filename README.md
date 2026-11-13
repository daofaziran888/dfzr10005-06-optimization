# 数仓治理与运维平台

面向数据仓库团队的治理与运维一体化平台。覆盖表生命周期管理、存储成本优化、数据热度分析、任务依赖编排、SLA告警、资源弹性调度与数仓健康度评估的完整治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 表生命周期管理 | `lifecycle` | 生命周期策略 | 策略编号、表名称、创建日期 |
| 存储成本优化 | `cost` | 成本分析报告 | 报告编号、分析范围、存储总量 |
| 数据热度分析 | `heatmap` | 热度记录 | 记录编号、表名称、查询次数 |
| 任务依赖编排 | `dag` | 依赖图节点 | 节点编号、节点名称、上游节点 |
| SLA告警管理 | `sla` | SLA规则 | 规则编号、监控对象、产出时间 |
| 资源弹性调度 | `resource` | 资源池配置 | 配置编号、资源类型、最小配额 |
| 数仓健康度评估 | `score` | 健康度报告 | 报告编号、评估范围、存储健康分 |
| 数据漂移检测 | `drift` | 漂移记录 | 记录编号、表名称、漂移字段 |
| 数据资产盘点 | `catalog` | 资产记录 | 资产编号、表名称、资产分类 |
| 数据保留策略 | `retention` | 保留规则 | 规则编号、数据集范围、保留期限 |
| 数据去重管理 | `dedup` | 去重任务 | 任务编号、目标表名、去重字段 |
| 数据分层规划 | `tier` | 分层定义 | 分层编号、分层名称、层级别名 |
| 基线监控管理 | `baseline` | 基线配置 | 基线编号、基线名称、承诺时间 |
| 数据回收站 | `recycle` | 回收记录 | 记录编号、表名称、删除时间 |
| 变更审计追踪 | `audit` | 变更记录 | 记录编号、变更对象、变更类型 |
| 查询性能分析 | `benchmark` | 性能报告 | 报告编号、查询语句、执行耗时 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 成本扫描（存储成本优化）

取数调用链：`cost/index.vue → /api/cost/scans → CostScanService → build_inventory()/read_segment()`，
汇总后写入容量摘要，报告列表与查询性能分析同步引用。

- **分段粒度**：按「表 + 分区」切段，每个分区一个可独立重试的分段，段键 `表|分区|路径` 唯一。
- **范围和批次**：同一分析范围（`范围指纹`）只保留一个计算批次；重复提交复用已有进度，
  新采样批次（`采样批次`）只把受影响分段打回待扫描，其余成功段命中缓存。
- **可恢复**：每段独立记录 `待扫描 / 扫描中 / 已完成 / 失败`，批次记录进度与当前成本口径；
  刷新页面按 `localStorage` 里的批次编号恢复同一进度，也可用 `GET /api/cost/scans` 直接查询。
- **失败隔离**：单个分段不可读只影响该段，报告不会被整份丢弃；失败原因带错误码、原始信息、
  处理建议和是否可重试，`POST /scans/{id}/retry` 只重试失败段，`POST /scans/{id}/resume`
  只处理待扫描段，已完成段不会被覆盖。
- **并发**：认领分段使用批次锁 + 认领标识；结束段只有一个请求能写回，容量摘要发布带版本号，
  并发重复提交不会重复写入预计节省。认领超时的分段会自动回到待扫描。
- **容量摘要**：全部成功后才发布 `CAP-xxxx`，包含存储总量、冷数据占比、预计节省、覆盖状态与
  当前成本口径；历史摘要编号（如 BENC-0002 引用的 `CAP-0002`）原地更新，不随重扫换号。
- **兼容历史**：COST-0001～COST-0003 报告编号和字段不变；COST-0002 的分析范围、存储总量
  （31.72 TB）、冷数据占比（77.6%）、预计节省（8.61 TB）、生成时间作为历史基线保留。

接口一览：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/cost/scans` | 提交范围（可选 `report_id`、`采样批次`、`max_segments`） |
| GET | `/api/cost/scans` | 批次列表与进度 |
| GET | `/api/cost/scans/{id}` | 批次明细：分段、失败原因、成本口径 |
| POST | `/api/cost/scans/{id}/resume` | 从断点继续 |
| POST | `/api/cost/scans/{id}/retry` | 只重试失败段 |
| GET | `/api/cost/capacity-summaries` | 容量摘要列表（性能分析引用） |

回调示例（首次扫描会被分段上限中断，之后可续扫）：

```bash
curl -X POST http://127.0.0.1:8000/api/cost/scans \
  -H 'Content-Type: application/json' \
  -d '{"values":{"分析范围":"dwd_order_detail 2026-09","report_id":2,"max_segments":2}}'
curl -X POST http://127.0.0.1:8000/api/cost/scans/SCAN-0003/resume \
  -H 'Content-Type: application/json' -d '{"values":{}}'
```

运行后端测试：`cd backend && .venv/bin/python -m pytest tests -q`

领域：数据工程
子方向：数仓与数据湖
