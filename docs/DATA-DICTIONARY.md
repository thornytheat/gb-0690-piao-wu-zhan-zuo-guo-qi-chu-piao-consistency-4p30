# 租约与票据定义

id为事件号、order为订单、ticket为票号，字符串保留前导零及超大数字。seat接受A01或A-01并统一A-01，目录A-01至A-16。channel为官方、票社、团购。
time带偏移，统一UTC，回放按事件时间排序，同刻先release再hold再pay再issue。hold需expires且后于time。
占座有效区间[start,end)，到期点付款无效。release仅能释放当前同订单租约，缩短end；其后旧支付不得恢复座席。占用中其他订单hold隔离。
同事件最高version采信，同版相互矛盾全部隔离。重复、旧版、无效状态都有来源行号与原因码。支付每租约只允许一笔，出票需在支付之后且金额相等，票号及座席全场唯一。
渠道对账为有效支付减有效出票，金额为Decimal字符串，悬单单独统计，不把它认作丢失事件。事件原始行序判断补传迟到，业务仍按事件时间完整回放。
输入行数=采信事件+隔离，租约/支付/票据各为派生集合不替代原始计数。空文件等失败码独立，运行ID含输入哈希、规则版本与参数，复核整数版本递增。

支付渠道须与其占座租约一致，出票渠道须与有效支付一致；跨渠道冲突逐行隔离，不在渠道间静默移动金额。

## 票号盘存 M8

盘存输入CSV字段：`id,kind,segment,seg_start,seg_end,channel,time,ticket,note,count,amount,ref`。`kind` 取 `segment`（号段发放登记）、`print`（打印记录）、`allocate`（临售申领）。号段行填 `seg_start/seg_end` 连号区间与渠道；打印行填 `time/ticket/note`（note 仅 `回执`、`缺报`）与票款 `amount`；申领行填 `count/amount`。`ref` 为业务参考，打印与申领必填且全局去重，重跑同 ref 不重复占用。

票号字符串保留前导零，按所属号段宽度规范化；号段连号全场唯一，跨渠道重叠或撞号隔离（原因码 `INV_TAKEN`）。票号状态阶梯为 `available 可发放 → allocated 已占用 → pending 待核实 → sold 已售`，只允许上行：缺报打印冻结在 pending，已售票号收到缺报或任何降级记录返回 `INV_REVERSE` 证据且状态不变；SQLite 触发器对任意写入路径兜底。申领只消费 available 中最小连号，余量不足返回 `INV_EXHAUSTED` 且整笔不占用。

纸票连号与 M5 电子票号是两套编号，不按票号相等匹配；按渠道+金额+出纸时点不晚于电子出票逐张核销，一张电子出票至多核销一张纸票。回执先于电子出票到达时暂挂 pending，后续盘存可确认。票款对账按渠道汇总：`delta=有效支付-已确认纸票`，待核实金额 `pending_amount` 单列，补回执确认后占用与对账在同一 SQLite 事务更新。盘存 run_id 由输入哈希、参数与盘存规则版本 `INV_RULE_VERSION` 决定，相同内容重跑复用批次。
