# 工业传感器二进制帧重同步 API

基于 **Python 3.11 + FastAPI + Pydantic v2 + SQLite** 的服务，用于在可能从任意半帧
开始的原始字节流中重新定位、校验并解码工业传感器二进制帧。

核心能力：

- **协议注册**：声明同步字节、长度字段（偏移 / 宽度 / 端序 / 调整值）、DLE 风格转义
  规则、CRC-16 参数（内置常用预设，也可自定义 poly/init/refin/refout/xorout），以及
  带符号与定点缩放（`scale` / `divisor`，支持位域与 min/max）的字段布局。
- **重同步解码** `POST /decode`：提交 base64 字节流，服务按同步字节逐个扫描候选帧；
  长度越界、转义非法、CRC 失败的候选被跳过并从下一个同步字节重试；选择**最早结束且
  校验通过**的完整帧；两个不同起点的合法帧若在同一位置结束则判定为协议歧义，返回
  结构化 422。
- **编码下发** `POST /encode`：按已注册协议把字段物理值（`Decimal` 精确反算原始整数）
  组装成可直接下发的二进制帧：写入位域、补 0 未写比特、派生长度值与 CRC、执行 DLE
  转义并添加同步字节；结果在返回前强制经现有解码器回读校验，字段不一致直接拒绝返回。
- **不完整尾部保留**：流末尾尚未凑齐的帧（含 dangling 转义符、只出现一半的同步头）
  原样保留在 `tail` 中返回，可直接与后续到达的字节拼接后再次提交。
- **分批采集会话** `POST /sessions` + `POST /sessions/append` + `GET /sessions/{id}`：
  创建会话时固定协议**确切版本**；现场字节流按批到达时，服务把上次未决尾部拼到新分片前、
  沿用同一套重同步解码，只返回**本次新确定**的帧与丢弃区间（起止为会话累计字节位置），
  并返回新待决尾部与下一偏移。已接收偏移、尾部与本次响应一起持久化，重启可续；最近一次
  分片按同一偏移与内容重发返回原响应且不重复产出；同偏移不同内容、并发/乱序提交一律 409
  拒绝且不改状态；拼合数据超限或解码报错均不推进偏移。提交以接收偏移为版本号在数据库内
  compare-and-swap 落库，**多个服务实例共用同一 SQLite 文件**时竞争追加也不会互相覆盖。
- **丢弃区间审计**：每次跳过都返回 `[start, end)` 区间与各候选的失败原因
  （`length_out_of_bounds` / `crc_failed` / `malformed_escape`）。
- **序号连续性稽核（可选）** `POST /sessions` 传 `"enable_sequence_audit": true`：
  创建时校验协议声明了**有效位宽至少两位的无符号整数** `sequence_field`（整字节无符号
  字段或 ≥2 位的无符号位域），否则 422 拒绝创建。每次成功追加只对**本次新确认的有效帧**
  按位宽取模与上一帧比较：相同→`duplicate`；前进超过一步且**小于半圈**→`suspected_gap`
  并给出 `missing_count`；**超过半圈**→`backward`；**恰好半圈**→`direction_unknown`
  （后两者不推断缺帧）。每个有效帧（含异常帧）都成为下一帧的比较基准，坏帧与未决尾部不
  改变基准。追加响应返回本次异常的帧偏移、前后序号与类型，进度查询给出最近序号与各类累计
  异常次数；稽核进度与分片状态、本次响应在同一条原子 UPDATE 中提交，重试只重放原结果、
  竞争 / 解码失败不增加异常也不推进基准，重启后续传。
- **结果复用**：同一协议版本 + 输入字节 SHA-256 摘要命中 SQLite 缓存，响应中
  `cached=true`。
- **结构化错误**：字段越界、协议歧义、非法协议声明均返回统一错误信封的 HTTP 422。

## 一条命令启动

```bash
docker compose up --build
```

容器把宿主机 **8000 端口**映射到容器内 8000（compose 中的 `ports: ["8000:8000"]`，
镜像 `EXPOSE 8000`，uvicorn 监听 `0.0.0.0:8000`）。服务监听
<http://localhost:8000>，交互式文档在 <http://localhost:8000/docs>。
若 8000 被占用，把 compose 映射改为 `"8001:8000"` 即可从 <http://localhost:8001> 访问。
SQLite 数据文件保存在命名卷 `resync-data`（容器内 `/data/resync.db`）。

本地（无 Docker）启动：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RESYNC_DB_PATH` | `/data/resync.db` | SQLite 文件路径 |
| `RESYNC_MAX_DECODE_BYTES` | `1048576` | 单次解码原始字节上限（超出返回 413） |

## 快速示例

### 1. 注册协议

```bash
curl -s -X POST localhost:8000/protocols -H 'Content-Type: application/json' -d '{
  "protocol_id": "sensor-line",
  "version": 1,
  "sync": [170, 85],
  "endian": "big",
  "length_field": {"offset": 4, "size": 1},
  "crc": {"preset": "CRC-16/CCITT-FALSE", "offset": 6},
  "fields": [
    {"name": "seq",  "type": "u8",  "offset": 0},
    {"name": "addr", "type": "u8",  "offset": 1},
    {"name": "temp", "type": "s16", "offset": 2, "scale": "0.01"}
  ],
  "sequence_field": "seq",
  "max_frame_length": 64
}'
```

帧的逻辑视图（同步字节之后）：

```
offset  0    1    2    3    4      5    6    7    8
        seq  addr temp_hi temp_lo len  st   crc_hi crc_lo x
└──────────── CRC 覆盖（offset 6..7 之外的全部逻辑字节）────────┘
```

内置 CRC 预设：`CRC-16/CCITT-FALSE`、`CRC-16/XMODEM`、`CRC-16/X25`、
`CRC-16/MODBUS`、`CRC-16/IBM-SDLC`、`CRC-16/ARC`、`CRC-16/USB`、`CRC-16/DNP`；
也可以省略 `preset`，直接给 `poly/init/refin/refout/xorout`。

### 2. 解码（可从半帧开始）

```bash
curl -s -X POST localhost:8000/decode -H 'Content-Type: application/json' -d '{
  "protocol_id": "sensor-line",
  "payload_b64": "AZm...（base64，可包含噪声、坏帧、半帧尾部）"
}'
```

响应示例：

```json
{
  "protocol_id": "sensor-line",
  "version": 1,
  "input_digest": "b4f07670…",
  "frames": [
    {
      "start": 2,
      "end": 13,
      "logical_length": 9,
      "sequence": 10,
      "crc_ok": true,
      "fields": [
        {"name": "seq",  "offset": 0, "size": 1, "raw": 10,   "value": 10,    "signed": false},
        {"name": "addr", "offset": 1, "size": 1, "raw": 3,    "value": 3,     "signed": false},
        {"name": "temp", "offset": 2, "size": 2, "raw": 2345, "value": 23.45, "signed": true}
      ]
    }
  ],
  "discarded": [
    {"start": 0, "end": 2, "reason": "noise", "failed_attempts": []}
  ],
  "tail": {"start": 13, "end": 18, "payload_b64": "qlUKAAk="},
  "frame_count": 1,
  "consumed": 13,
  "cached": false
}
```

- `start` / `end`：帧在**原始字节流**中的半开区间 `[start, end)`（端到端，含同步字节）。
- `discarded`：被放弃的字节区间；`reason` 为 `noise`（其间没有可识别同步头）或
  `resync_failed_checks`，后者的 `failed_attempts` 给出每个候选同步点的失败细节。
- `tail`：流尾部无法确认成帧的字节，base64 原样回传，收到新数据后拼接重试即可。
- 再次提交完全相同的字节流时 `cached=true`。

### 3. 编码一帧（可直接下发）

`POST /encode` 按已注册协议反向组装一帧。请求给出 `protocol_id`、`version`（可省略，
取最新版本）、`logical_length`（逻辑体长度，即同步字节之后、转义之前的字节数）和
`fields`（字段名 → 物理值）。字段值建议用字符串给出精确小数（裸 JSON 数字也会按
`Decimal` 解析，不会经过二进制浮点）：

```bash
curl -s -X POST localhost:8000/encode -H 'Content-Type: application/json' -d '{
  "protocol_id": "sensor-line",
  "version": 1,
  "logical_length": 9,
  "fields": {
    "seq": 1,
    "addr": 3,
    "temp": "23.45",
    "state": 7,
    "alarm": 1
  }
}'
```

响应示例：

```json
{
  "protocol_id": "sensor-line",
  "version": 1,
  "wire_b64": "qlUBAwkJ…",
  "logical_b64": "AQMJCSVl…",
  "logical_length": 9,
  "wire_length": 11,
  "length_value": 9,
  "crc": "65A5",
  "fields": [
    {"name": "seq",  "offset": 0, "size": 1, "value": "1",     "raw": 1,    "wire": 1,    "bit_offset": null, "bit_width": null, "signed": false},
    {"name": "addr", "offset": 1, "size": 1, "value": "3",     "raw": 3,    "wire": 3,    "bit_offset": null, "bit_width": null, "signed": false},
    {"name": "temp", "offset": 2, "size": 2, "value": "23.45", "raw": 2345, "wire": 2345, "bit_offset": null, "bit_width": null, "signed": true},
    {"name": "state", "offset": 5, "size": 1, "value": "7",    "raw": 7,    "wire": 7,    "bit_offset": 0, "bit_width": 3, "signed": false},
    {"name": "alarm", "offset": 5, "size": 1, "value": "1",    "raw": 1,    "wire": 1,    "bit_offset": 3, "bit_width": 1, "signed": false}
  ]
}
```

- `wire_b64`：`同步字节 || 转义后的逻辑体`，base64 解码后即可直接下发。
- `logical_b64`：同步字节之后、未转义的逻辑体；未被任何字段写入的比特保证为 0。
- `length_value` / `crc`：实际写入长度字段的原始值（`logical_length - adjustment`）
  与实际计算并写入的 CRC-16（大写十六进制）。
- `logical_length` / `wire_length`：实际逻辑长度与线上长度（开启转义时后者更大）。
- `fields[].raw` 为按 `value × divisor ÷ scale` 反算的有符号原始整数；
  `fields[].wire` 为放到线上的无符号值（有符号字段为二补码，位域为位域内整数值）。

编码规则与校验：

- 服务用 `Decimal` 执行 `raw = value × divisor ÷ scale`，结果必须是**精确整数**且
  满足字段位宽（含位域宽度）与 `min`/`max`，否则 422。
- 多个字段覆盖同一比特时，只允许写入相同的值；冲突返回
  `OVERLAPPING_WRITE_CONFLICT` 422（details 给出字节/比特与冲突字段）。
- 字段不得占用长度字段或 CRC 的派生位置（`DERIVED_POSITION_CONFLICT`）。
- 字段全部写入后才写长度值与 CRC，随后执行转义、添加同步字节。
- 返回前强制把 `wire_b64` 送入现有 `/decode` 解码器回读；帧数不为 1、尾部非空或任一
  字段值与请求不一致时返回 `ENCODE_VERIFICATION_FAILED` 422，不下发该帧。

### 4. 分批采集会话（跨分片 / 重试 / 重启续传）

当现场字节流分批到达时，用一个**采集会话**把分片串成同一条逻辑字节流，服务保证每帧只被
产出一次。会话在创建时就**固定协议的确切版本**（不接受“最新版本”省略写法）。

#### 4.1 创建会话

```bash
curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' -d '{
  "protocol_id": "sensor-line",
  "version": 1
}'
```

> 可选参数 `"enable_sequence_audit": true` 开启序号连续性稽核（详见 [4.7](#47-序号连续性稽核可选启用)）。

响应：

```json
{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "protocol_id": "sensor-line",
  "version": 1,
  "received_offset": 0,
  "next_offset": 0,
  "tail": {"start": 0, "end": 0, "payload_b64": ""},
  "frames_total": 0
}
```

#### 4.2 追加分片

`POST /sessions/append` 提交 `session_id`、`expected_offset`（期望接收偏移，即上一次响应的
`next_offset`；首个分片填 0）与**非空** base64 分片 `chunk_b64`。服务每次把**上次未决尾部
拼到新分片之前**（`tail || chunk`），沿用现有重同步解码，只返回**本次新确定**的帧与丢弃区间，
其 `start`/`end` 已统一换算成**会话累计字节位置**，并返回新的待决尾部与下一偏移。

把 `噪声2字节 + 完整帧1 + 帧2前4字节` 作为第 1 片（共 17 字节）：

```bash
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "expected_offset": 0,
  "chunk_b64": "EjSqVQEDCSkJAHWbAKpVAgM="
}'
```

响应（只含本次新确定的帧；帧 2 的前 4 字节进入 `tail`）：

```json
{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "protocol_id": "sensor-line",
  "version": 1,
  "received_offset": 0,
  "next_offset": 17,
  "frames": [
    {"start": 2, "end": 13, "logical_length": 9, "sequence": 1, "crc_ok": true, "fields": [ ... ]}
  ],
  "discarded": [
    {"start": 0, "end": 2, "reason": "noise", "failed_attempts": []}
  ],
  "tail": {"start": 13, "end": 17, "payload_b64": "qlUCAw=="},
  "frame_count": 1,
  "frames_total": 1,
  "replayed": false
}
```

第 2 片用上次的 `next_offset=17` 作为 `expected_offset`，只发帧 2 剩余的 7 字节
（`/2oJAKqKAA==`）。服务把上片尾部 `aa 55 02 03` 接到本片前面重解，帧 2 在累计偏移
`[13, 24)` 处补齐：

```bash
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "expected_offset": 17,
  "chunk_b64": "/2oJAKqKAA=="
}'
```

```json
{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "version": 1,
  "received_offset": 17,
  "next_offset": 24,
  "frames": [
    {"start": 13, "end": 24, "logical_length": 9, "sequence": 2, "crc_ok": true, "fields": [ ... ]}
  ],
  "discarded": [],
  "tail": {"start": 24, "end": 24, "payload_b64": ""},
  "frame_count": 1,
  "frames_total": 2,
  "replayed": false
}
```

要点：

- 第 1 片已返回的帧与丢弃区间**不会**在第 2 片响应里重复出现；被拆开的帧（含跨片的同步头、
  dangling 转义符）只在补齐那一片的响应中出现一次。
- `frames`/`discarded` 的偏移是**会话累计位置**；`tail.start/end` 也是累计位置，
  `tail.payload_b64` 是该尾部的原始字节。
- `received_offset` 是本次追加**之前**已接收的字节数，`next_offset` 是下一次应填的
  `expected_offset`，`frames_total` 是整个会话累计确认的帧数。

#### 4.3 重试（幂等）与冲突

网络超时等情况下，把**最近一次分片**按同一 `expected_offset` 和**完全相同的内容**重发即可：
服务返回与上次完全一致的响应，只是 `replayed: true`，不会重复产出帧、`frames_total` 不变。

```bash
# 与第 2 片完全相同的重发
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "expected_offset": 17,
  "chunk_b64": "/2oJAKqKAA=="
}'
# -> 200，响应体同上，但 "replayed": true，frames_total 仍为 2
```

以下提交一律 **409 `SESSION_APPEND_CONFLICT` 且不改变任何状态**：

- **同偏移、不同内容**（试图改写已提交分片）：`details.reason = "content_mismatch"`，
  并给出 `committed_sha256` / `submitted_sha256`；
- **乱序 / 跳跃提交**（`expected_offset` 不等于服务端 `received_offset`，含迟到的旧分片、
  并发抢跑）：`details.reason = "offset_mismatch"`，并给出当前 `next_offset`。

```bash
# 同偏移不同内容 -> 409 content_mismatch
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "expected_offset": 17,
  "chunk_b64": "WFhYWA=="
}'

# 期望偏移跳跃 -> 409 offset_mismatch
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "expected_offset": 99,
  "chunk_b64": "WVk="
}'
```

同一会话的并发追加按会话加锁串行化；跨进程/多实例共用同一 SQLite 文件时，提交本身以
**接收偏移为版本号的 compare-and-swap**（`UPDATE ... WHERE received_offset = <读取时偏移>`）
在数据库内核对读取时的偏移仍有效，新偏移、待决尾部、累计帧数、最近分片与响应作为**一个状态**
原子生效。因此两个**不同**分片在同一偏移竞争时，恰有一个成功、另一个 409
（`content_mismatch`）；两个**相同**分片竞争时，一个正常提交、另一个作为重放返回
（`replayed: true`），绝不重复计数。

#### 4.4 超限与解码报错都不推进偏移

上次待决尾部与新分片**拼合后**的字节数超过 `RESYNC_MAX_DECODE_BYTES` 时返回 413
`PAYLOAD_TOO_LARGE`（`details.size` 为拼合后的大小）；解码过程中出现硬错误（如
`AMBIGUOUS_FRAME`、`FIELD_OUT_OF_BOUNDS`）时返回相应 422。两种情况下会话状态都保持在追加
之前：`received_offset`、`tail`、`frames_total` 与最近一次成功响应均不变，客户端可在排查后
用**相同的偏移**重发一个更短/修正后的分片。空分片（解码后 0 字节）为 422
`VALIDATION_ERROR`，非法 base64 为 422 `INVALID_BASE64`。

#### 4.5 进度查询与重启续传

```bash
curl -s localhost:8000/sessions/00e49c0d66904047837921d05a3090d6
```

```json
{
  "session_id": "00e49c0d66904047837921d05a3090d6",
  "protocol_id": "sensor-line",
  "version": 1,
  "received_offset": 24,
  "next_offset": 24,
  "tail": {"start": 24, "end": 24, "payload_b64": ""},
  "frames_total": 2,
  "last_chunk_offset": 17,
  "last_chunk_sha256": "5a77d65f01fa07275b780aacebfc81515b6056423a41608ed04fbe85d7c0f456",
  "created_at": "2026-10-05 15:40:59",
  "updated_at": "2026-10-05 15:40:59"
}
```

已接收偏移、待决尾部、累计帧数与最近一次分片（偏移 + SHA-256 + 原响应）在每个成功追加时
以**接收偏移为守卫的同一条原子 UPDATE** 写入 SQLite（默认 `/data/resync.db`，见上方启动/卷
配置）。服务重启后用 `GET /sessions/{id}` 读取进度，把最近一次未确认的分片按
`last_chunk_offset` 重发即可拿到原响应（`replayed: true`），随后用新的 `next_offset` 继续，
不会丢帧也不会重复产出。会话不存在返回 404 `SESSION_NOT_FOUND`。

#### 4.6 多实例共用同一 SQLite（副本部署）

可以启动多个服务实例指向**同一个 SQLite 数据库文件**（同一 `RESYNC_DB_PATH`），会话追加的
正确性不依赖进程内锁：提交时在数据库内核对读取时的接收偏移仍有效才落库，竞争落败的实例会
重新读取最新状态，再按 4.3 的约定判定（同偏移同内容 → 重放原响应；同偏移不同内容 → 409
`content_mismatch`；过期/跳跃偏移 → 409 `offset_mismatch`），落败方不改动任何状态。

本地起两个实例（注意用可写的数据库路径）：

```bash
# 终端 1
RESYNC_DB_PATH=/tmp/resync-shared.db uvicorn app.main:app --host 0.0.0.0 --port 8000
# 终端 2（同一数据库文件）
RESYNC_DB_PATH=/tmp/resync-shared.db uvicorn app.main:app --host 0.0.0.0 --port 8001
```

调用方式与单实例完全一致，客户端可任选一个实例：

```bash
# 在实例 8000 上建会话、提交第 1 片
curl -s -X POST localhost:8000/protocols -H 'Content-Type: application/json' -d '{ ... }'
curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' \
  -d '{"protocol_id": "sensor-line", "version": 1}'
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' \
  -d '{"session_id": "<sid>", "expected_offset": 0, "chunk_b64": "EjSqVQEDCSkJAHWbAKpVAgM="}'

# 实例 8001 立即看到 8000 提交的进度，并可继续追加第 2 片
curl -s localhost:8001/sessions/<sid>
curl -s -X POST localhost:8001/sessions/append -H 'Content-Type: application/json' \
  -d '{"session_id": "<sid>", "expected_offset": 17, "chunk_b64": "/2oJAKqKAA=="}'
```

两个实例同时读到同一 `next_offset` 并各自提交**不同**分片时，恰有一个返回 200，另一个收到
409 `content_mismatch`；提交**相同**分片（如超时重试打到另一实例）时，一个正常提交、另一个
返回 `replayed: true` 的原响应，`frames_total` 不重复累计。原有的
`docker compose up --build` 单实例启动方式保持不变（命名卷 `resync-data` 内的数据库文件同样
可以直接挂给第二个容器实例共用）。

#### 4.7 序号连续性稽核（可选启用）

需要对单序列采集流做序号稽核时，在创建会话时打开
`enable_sequence_audit`。协议必须声明 `sequence_field`，且该字段是**有效位宽至少两位的
无符号整数**：整字节无符号字段（`u8/u16/u24/u32/u64`）天然满足；位域字段按
`bit_width` 判定（≥2）；有符号字段（`s8` 等）、1 位字段或未声明 `sequence_field`
的协议一律 422 `SEQUENCE_AUDIT_UNSUPPORTED`，会话不创建。原有会话与不带该参数（默认
`false`）的会话行为完全不变。

**启用：**

```bash
curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' -d '{
  "protocol_id": "sensor-line",
  "version": 1,
  "enable_sequence_audit": true
}'
```

响应中的 `sequence_audit` 给出稽核配置与初始状态：

```json
{
  "session_id": "a1b2c3…",
  "protocol_id": "sensor-line",
  "version": 1,
  "received_offset": 0,
  "next_offset": 0,
  "tail": {"start": 0, "end": 0, "payload_b64": ""},
  "frames_total": 0,
  "sequence_audit": {
    "enabled": true,
    "sequence_field": "seq",
    "bit_width": 8,
    "last_sequence": null,
    "anomalies": [],
    "counts": {"duplicate": 0, "suspected_gap": 0, "backward": 0, "direction_unknown": 0}
  }
}
```

**判定规则（以 `bit_width=8`、模数 256 为例）：** 每次成功追加只检查**本次新确认的有效帧**，
与比较基准（上一个有效帧的序号）按序号字段位宽取模比较：

| 模 256 的前进量 | 判定 | 含义 |
| --- | --- | --- |
| 0 | `duplicate` | 序号与上一帧相同 |
| 1 | 正常 | 连续，无异常 |
| 2 … 127 | `suspected_gap` | 疑似缺帧，`missing_count = 前进量 - 1` |
| 128（恰好半圈） | `direction_unknown` | 无法判向，**不**推断缺帧 |
| 129 … 255 | `backward` | 视为倒退，**不**推断缺帧 |

每个有效帧（包括被判为异常的帧）都成为下一帧的比较基准；**坏帧**（长度越界 / CRC 失败等被
跳过的候选）与**未决尾部**根本不会成为有效帧，因此不推进基准、不计异常。第一个有效帧只建立
基准，不产生异常。

**跨分片：** 稽核基准（最近序号）与各类累计次数随会话状态持久化，被拆开的帧只在补齐那一片
被稽核一次。下例中第 1 片确认序号 0、2（缺了序号 1），随后一个序号 3 的帧被拆成两片：

```bash
# 第 1 片：seq 0, 2
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "a1b2c3…",
  "expected_offset": 0,
  "chunk_b64": "qlECA…（含 seq=0 与 seq=2 两帧）"
}'
```

```json
{
  "frames": [ {"sequence": 0, "start": 0, "end": 11, …},
              {"sequence": 2, "start": 11, "end": 22, …} ],
  "frame_count": 2,
  "frames_total": 2,
  "next_offset": 22,
  "replayed": false,
  "sequence_audit": {
    "enabled": true,
    "sequence_field": "seq",
    "bit_width": 8,
    "last_sequence": 2,
    "anomalies": [
      {"frame_offset": 11, "frame_end": 22, "previous_sequence": 0,
       "sequence": 2, "type": "suspected_gap", "missing_count": 1}
    ],
    "counts": {"duplicate": 0, "suspected_gap": 1, "backward": 0, "direction_unknown": 0}
  }
}
```

```bash
# 第 2 片只发来序号 3 帧的前半（帧在累计偏移 22 处开始，仍在 tail 中）
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "a1b2c3…", "expected_offset": 22, "chunk_b64": "qlUD…（半帧）"
}'
# -> frame_count 0，sequence_audit.last_sequence 仍为 2，anomalies 为空

# 第 3 片补齐该帧：仅在此时与基准序号 2 比较（2 -> 3 连续，无异常）
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "a1b2c3…", "expected_offset": 26, "chunk_b64": "…（帧剩余部分）"
}'
# -> sequence_audit.anomalies 为 []，last_sequence 更新为 3
```

序号回绕（如 8 位宽下 255 → 0、16 位宽下 65535 → 0）视为正常前进一步。

**重试：** 最近分片按同一 `expected_offset` 与完全相同内容重发时，服务只重放已提交的原响应
（`replayed: true`），其中的 `sequence_audit` 与首次响应逐字一致，**异常不重复累计、基准不
重复推进**：

```bash
curl -s -X POST localhost:8000/sessions/append -H 'Content-Type: application/json' -d '{
  "session_id": "a1b2c3…", "expected_offset": 0,
  "chunk_b64": "qlECA…（与第 1 片完全相同）"
}'
# -> 200, "replayed": true, sequence_audit.counts.suspected_gap 仍为 1
```

同偏移不同内容 / 乱序跳跃 / 并发竞争（409 `SESSION_APPEND_CONFLICT`）、拼合超限（413）或
解码硬错（422，如 `AMBIGUOUS_FRAME`）一律不改任何稽核状态：异常次数不增加、`last_sequence`
不推进，修正后用同一偏移重发即可。多个实例共用同一 SQLite 时，稽核进度与接收偏移、尾部、
帧数、最近分片及原响应在**同一条以接收偏移为守卫的原子 UPDATE** 中落库，竞争落败方不计数、
不改基准。

**进度查询：** `GET /sessions/{id}` 的 `sequence_audit` 给出最近确认序号与各类**累计**异常
次数（`anomalies` 恒为空数组，逐次异常只在对应的追加响应里返回）：

```bash
curl -s localhost:8000/sessions/a1b2c3…
```

```json
{
  "received_offset": 44,
  "next_offset": 44,
  "frames_total": 4,
  "sequence_audit": {
    "enabled": true,
    "sequence_field": "seq",
    "bit_width": 8,
    "last_sequence": 5,
    "anomalies": [],
    "counts": {"duplicate": 0, "suspected_gap": 2, "backward": 0, "direction_unknown": 0}
  },
  …
}
```

服务重启后稽核进度从 SQLite 恢复：先按 `last_chunk_offset` 重放未确认分片拿到原响应，再以
持久化的 `last_sequence` 为基准继续追加。未启用稽核的会话，创建 / 追加 / 进度响应中的
`sequence_audit` 为 `null`（进度与追加）或缺省（创建），其余行为与旧版一致；旧版本数据库
文件首次被新版服务打开时会自动补列迁移。

## 协议声明字段

| 字段 | 说明 |
| --- | --- |
| `sync` | 1–8 个同步字节（如 `[0xAA,0x55]`） |
| `endian` | 协议默认端序 `big` / `little`；长度字段、CRC、各字段可单独覆盖 |
| `length_field` | `offset`（相对逻辑体）、`size`（1/2/4）、`endian`、`adjustment`。线上长度值 + adjustment = 逻辑帧长；当长度字段把帧头也计数时用负数（如 `-4`） |
| `escaping` | DLE 转义：`escape_byte`、`xor_mask`（转义后字节 = 原字节 XOR mask）、`escapes_sync`、额外受保护字节 `escaped_bytes` |
| `crc` | CRC-16 参数与 `offset`（2 字节 CRC 在逻辑体中的偏移）、`endian`、`verify` |
| `fields[]` | `type` 为 `u8/u16/u24/u32/u64/s8/s16/s24/s32/s64`；定点值 = 整数 × `scale` ÷ `divisor`；可选 `bit_offset`+`bit_width` 取位域，`min`/`max` 做范围校验 |
| `sequence_field` | 指定作为帧序号的字段名（响应中的 `sequence`；启用会话序号稽核时必须是有效位宽 ≥2 位的无符号整数） |
| `min/max_frame_length` | 逻辑帧长上下界；越界候选触发重同步 |

### 转义语义

转义只作用于同步头之间。开启后，同步字节、转义字节（及 `escaped_bytes` 声明的字节）
在链路上以 `escape_byte || (byte XOR xor_mask)` 两个字节出现；解码时实时反转义，长度
字段与所有字段偏移都按**逻辑（反转义后）空间**解释。缓冲区恰好结束在转义引导字节处时
视为不完整帧，进入 `tail`。

## 错误信封

所有错误统一为：

```json
{"error": {"code": "FIELD_OUT_OF_BOUNDS", "message": "…", "details": { … }}}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 422 | `VALIDATION_ERROR` / `INVALID_PROTOCOL` | 请求体或协议声明不合法（附字段级 details） |
| 422 | `INVALID_BASE64` | `payload_b64` 不是合法 base64 |
| 422 | `FIELD_OUT_OF_BOUNDS` | 字段超出帧边界，或缩放后值超出 `min/max` |
| 422 | `AMBIGUOUS_FRAME` | 多个不同起点的合法帧结束于同一位置 |
| 422 | `VALUE_NOT_REPRESENTABLE` | `/encode`：反算结果不是精确整数（或值非有限值、scale 为 0） |
| 422 | `UNKNOWN_FIELD` | `/encode`：请求的字段名不在协议声明中 |
| 422 | `OVERLAPPING_WRITE_CONFLICT` | `/encode`：字段覆盖同一比特但写入不同值 |
| 422 | `DERIVED_POSITION_CONFLICT` | `/encode`：字段占用长度字段或 CRC 的字节位置 |
| 422 | `LENGTH_NOT_ENCODABLE` | `/encode`：长度值超出长度字段位宽 |
| 422 | `ENCODE_VERIFICATION_FAILED` | `/encode`：解码器回读结果与请求不一致（帧拒绝返回） |
| 404 | `PROTOCOL_NOT_FOUND` | 协议 ID / 版本不存在 |
| 409 | `PROTOCOL_VERSION_CONFLICT` | 同 ID+版本已存在但定义不同 |
| 404 | `SESSION_NOT_FOUND` | 会话 ID 不存在（追加或进度查询） |
| 422 | `SEQUENCE_AUDIT_UNSUPPORTED` | 创建会话启用序号稽核，但协议未声明 `sequence_field`、字段有符号或有效位宽不足两位（`details.reason`：`missing_sequence_field` / `signed_sequence_field` / `sequence_field_too_narrow`） |
| 409 | `SESSION_APPEND_CONFLICT` | 会话追加：同偏移不同内容（`reason=content_mismatch`）或乱序/跳跃/并发提交（`reason=offset_mismatch`），状态不变 |
| 413 | `PAYLOAD_TOO_LARGE` | 超 `RESYNC_MAX_DECODE_BYTES`（会话中指 待决尾部 + 新分片 的拼合大小） |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/protocols` | 注册（或幂等覆盖）协议版本 |
| `GET` | `/protocols` | 列出已注册协议及定义摘要 |
| `GET` | `/protocols/{id}?version=N` | 读取协议定义（省略版本取最新） |
| `POST` | `/decode` | 解码 / 重同步 |
| `POST` | `/encode` | 按已注册协议编码一帧（长度/CRC/转义/同步自动生成，回读校验） |
| `POST` | `/sessions` | 创建分批采集会话（固定协议的确切版本；`enable_sequence_audit:true` 可选启用序号连续性稽核） |
| `POST` | `/sessions/append` | 追加非空 base64 分片（尾部自动拼接，仅返回本次新结果；启用稽核时 `sequence_audit` 给出本次异常） |
| `GET` | `/sessions/{id}` | 查询会话进度（偏移/待决尾部/累计帧数/最近一次分片；启用稽核时含最近序号与累计异常次数） |

同 ID + 版本重复提交**完全相同**的定义是幂等的（返回 `replaced=true`）；定义不同则 409。

## 项目结构

```
app/
  config.py    # 环境配置
  models.py    # Pydantic 协议/请求/响应模型与协议自检
  crc.py       # 通用 CRC-16（反射/非反射）
  decoder.py   # 候选扫描、反转义、长度/CRC 校验、重同步与歧义判定
  encoder.py   # 字段写入（位域/定点反算/冲突检测）、长度与 CRC 派生、转义、回读校验
  db.py            # SQLite：协议表 + (协议版本, 输入摘要) 结果缓存 + 采集会话状态表（含序号稽核进度）
  sequence_audit.py # 序号字段有效位宽校验与模 2^w 连续性分类（重复/疑似缺帧/倒退/无法判向）
  errors.py    # 结构化错误类型
  main.py      # FastAPI 路由与错误处理（/decode、/encode、/sessions[/append]）
tests/         # pytest 用例（含构造 CRC 碰撞的歧义用例、编码回读校验、会话跨分片/重试/重启）
Dockerfile
docker-compose.yml
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest
```

覆盖：半帧起始 + 噪声 + 多帧 + 不完整尾部、坏 CRC / 长度越界后重同步、DLE 转义
（含同步字节与转义字节被转义）、little-endian + CRC-16/MODBUS + 长度 adjustment、
位域与有符号定点缩放、字段越界 422、CRC 碰撞歧义 422、缓存复用、版本冲突与解析；
编码侧覆盖：定点精确反算、正负有符号值、位域共享字节与同值重叠、转义帧往返、
LE/adjustment 长度值、不可精确表示 / min/max / 位宽 / 派生位置 / 长度溢出 422，
以及解码器回读不一致拒绝返回。
会话侧（`tests/test_sessions.py`）覆盖：版本固定与缺失版本 404、跨分片（含跨片同步头、
dangling 转义符）只产出一次且偏移为累计位置、最近分片同偏移同内容重放不重复计数、同偏移异
内容 / 乱序跳跃 / 并发竞争 409 且不改状态、拼合超限与歧义硬错不推进偏移、空/非法分片 422，
以及关闭并重建 App（模拟服务重启）后进度/尾部持久化、重放原响应并继续追加。
`tests/test_sessions_multi_instance.py` 用两个共享同一 SQLite 文件的独立实例核对：不同分片
同偏移竞争恰有一个提交、落败方 409 `content_mismatch` 且状态只反映胜方；相同分片竞争一个
提交一个重放、不重复计帧；活跃实例间交替追加与进度互见；新实例（模拟重启）读取进度、重放
最近分片并继续追加。
序号稽核侧（`tests/test_sequence_audit.py`）覆盖：缺序号字段 / 有符号 / 1 位宽 422 拒绝、
2 位与 8/16 位宽下的重复、疑似缺帧（含 `missing_count`）、超过/恰好半圈（倒退 / 无法判向）
与回绕连续、坏 CRC 帧与未决尾部不推进基准、异常帧自身成为基准、跨分片拆分只稽核一次、
重试重放不重复计数、冲突 / 超限 / 歧义硬错不改稽核状态、重启后基准与累计次数续传、旧版
数据库自动迁移，以及多实例竞争下稽核状态只反映胜方。
