# 搜索进度、来源快照与变更监控

## 目录

1. 目标与边界
2. 搜索进度
3. 来源快照
4. 变更判断
5. 监控清单
6. 刷新工作流
7. 交付标准

## 目标与边界

把一次性搜索升级为可续接的研究过程，同时区分三个概念：

- **搜索进度**：已经查了哪些范围、查询和页面，下一步从哪里继续。
- **来源快照**：某一来源在某个时间点的结构化字段和内容指纹。
- **变更监控**：哪些来源何时需要复查，以及复查后发生了什么变化。

监控不等于后台持续抓取。只有用户明确授权并且当前环境已经创建定时任务时，
才可以声称“持续监控”；否则只交付监控清单和到期任务。

使用：

- `assets/research-progress-template.json`
- `assets/source-snapshots-template.json`
- `assets/monitoring-template.json`
- `scripts/research_monitor.py`

## 搜索进度

开始公司或岗位搜索时先确定 `scope`：

`候选人类型｜城市｜行业｜岗位线｜公司类别｜招聘类型｜时间窗口`

每执行一个查询或检查一组页面，记录一个 `search_event`：

`轮次｜查询词｜覆盖维度｜来源 URL｜来源等级｜结果状态｜检查页数｜发现公司数｜发现招聘项目数｜发现岗位数｜下一游标｜核验时间｜备注`

轮次只使用：

- `company_discovery`
- `program_discovery`
- `job_deep_check`
- `refresh`

结果状态只使用：

- `completed`
- `partial`
- `blocked`
- `no_result`

`partial` 必须保存下一页、下一公司或下一查询词；`blocked` 必须记录验证码、登录、
访问限制或其他阻断原因。不得把无法访问写成没有岗位。

运行 `audit-progress` 后检查：

- 三轮检索是否都有记录
- 哪些城市、行业、岗位线和公司类别尚未触达
- 哪些查询仍有下一游标
- 总共检查了多少页面并发现多少公司、招聘项目和岗位

## 来源快照

对高价值或动态来源建立稳定 `source_id`，优先包括：

- 官方岗位详情页
- 官方招聘项目或公告页
- 公司职位搜索页
- 截止日期临近的公共招聘公告
- 处于“待确认”的关键来源
- 用户明确要求持续关注的公司或查询

每次快照至少记录：

`source_id｜source_type｜URL｜来源等级｜核验时间｜HTTP/访问状态｜结构化字段｜内容哈希｜字段哈希｜与上一版关系`

`source_type` 使用：

`company｜program｜job｜policy｜search_result｜other`

岗位快照的 `observed_fields` 优先包含：

`company｜title｜job_code｜location｜batch｜application_status｜published_at｜deadline｜education｜experience｜qualifications｜source_url`

招聘项目快照优先包含：

`program_name｜recruitment_type｜candidate_batch｜cities｜application_open｜published_at｜deadline｜entry_url`

快照保存结构化事实和内容指纹，不默认长期保存完整网页正文。需要留存正文或页面截图时，
应说明用途、保存位置和保留期限。

## 变更判断

同一 `source_id` 的新旧快照比较后，只使用：

- `new`：第一次记录
- `unchanged`：内容和结构化字段均未改变
- `changed`：内容或至少一个字段改变

字段变化应输出：

`字段｜旧值｜新值`

高优先级变化：

- 投递入口由开放变为关闭，或重新开放
- 截止日期提前、延后或首次公布
- 地点、届别、学历、专业、经验和资格门槛变化
- 岗位撤下、编号变化或重新发布
- 招聘、签约或用工主体变化

只有内容哈希变化、但关键字段不变时，标记“页面内容变化，岗位关键字段未确认变化”，
不要自动改写岗位结论。

## 监控清单

每个 `watch_item` 记录：

`watch_id｜source_id｜目标类型｜URL｜优先级｜刷新周期｜最后检查｜下次检查｜最后快照｜最后变化｜启用状态｜负责人`

建议刷新周期：

- 截止日期 7 天内、入口不稳定或高意愿岗位：1 天
- 正在进行的校招批次、补录、待确认岗位：2–3 天
- 一般社招岗位和公司职位页：7 天
- 暂无招聘但值得长期关注的公司：14–30 天

周期只是默认建议。以页面变化速度、截止日期和用户优先级调整。

## 刷新工作流

1. 运行 `due` 获取到期项目。
2. 按优先级和截止日期打开来源。
3. 重新提取可核验字段，保存为 JSON。
4. 运行 `capture` 写入快照并更新监控项。
5. 对 `changed` 运行 `diff`，判断是否影响硬门槛、推荐状态和下一动作。
6. 把变化同步到 `jobs.json`、`applications.csv` 和用户报告。
7. 若职责、门槛、地点、截止日期或岗位状态变化，检查对应
   `application-materials.json` 的 `source_snapshot_id`；将旧材料标记为待复核，
   不能继续把旧版求职信、网申答案或沟通稿当作当前版本。
8. 对访问受限项目记录失败原因和下一次人工核验动作。

示例：

```text
python scripts/research_monitor.py log-search --progress research-progress.json \
  --round company_discovery --query "杭州 机器人 专精特新 招聘" \
  --city 杭州 --industry 机器人 --employer-type specialized_sme \
  --result-status completed --pages-checked 3 --companies-found 8

python scripts/research_monitor.py add-watch --watchlist monitoring.json \
  --watch-id watch_job_001 --source-id source_job_001 --target-type job \
  --url "https://example.com/job/001" --priority high --cadence-days 2

python scripts/research_monitor.py capture --store source-snapshots.json \
  --source-id source_job_001 --source-type job --source-level official_job \
  --url "https://example.com/job/001" --content-file page.txt \
  --fields-json observed-fields.json --watchlist monitoring.json \
  --watch-id watch_job_001

python scripts/research_monitor.py diff --store source-snapshots.json \
  --source-id source_job_001

python scripts/research_monitor.py due --watchlist monitoring.json \
  --as-of 2026-07-29
```

## 交付标准

每次岗位搜索或刷新至少交付：

1. 本次搜索范围和已完成轮次
2. 新发现、已更新、未变化和无法访问的来源数量
3. 关键字段变化及对投递决策的影响
4. 尚未覆盖的搜索维度和下一游标
5. 到期监控项、负责人和下一检查日期
