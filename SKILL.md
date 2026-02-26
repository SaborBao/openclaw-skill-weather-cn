---
name: weather
description: 中国天气查询（替换内置 weather）。用于按中文地名查询天气（高德地理编码 + 彩云天气），输出近几日 + 小时预报，并支持 mock/debug/cache。
---

# Weather CN Query

使用 `scripts/weather_cn.py` 完成中国天气查询与联调。

## 执行查询

运行命令：

```bash
./scripts/weather_cn.py "北京市海淀区"
```

默认行为：
- 查询近几日天气（内部固定 7 天请求，受上游返回天数约束）
- 聚合小时预报（前 6 小时）
- 输出格式为 `text`

## 常用参数

- `--format text|json`：输出格式，默认 `text`
- `--detail basic|full`：详情级别，默认 `basic`
- `--hourly-steps N`：`full` 模式小时步数（1~360）
- `--mock`：离线调试模式，不访问外网
- `--debug`：打印请求与缓存命中日志
- `--cache-dir DIR`：缓存目录

## 环境变量

从当前目录 `.env` 自动加载：
- `AMAP_API_KEY`
- `CAIYUN_API_TOKEN`

也可通过参数覆盖：
- `--amap-key`
- `--caiyun-token`

## 输出约定

- 文本输出标题为“近几日天气”
- 日级仅显示周几
- 小时预报显示“降水概率 + 降水量(mm/h)”
- `json` 输出包含结构化 `daily`、`hourly`、`realtime` 字段

## 联调与修改原则

- 保持位置参数为唯一必填入参：`place`
- 保持默认输出为 `text`
- 优先保持字段语义一致：概率使用 `probability`，降水量使用 `value`
- 修改后至少执行一次 `--mock` 验证与一次真实接口验证
