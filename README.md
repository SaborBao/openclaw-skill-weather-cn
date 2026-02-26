# openclaw-skill-weather-cn

中国天气查询 OpenClaw Skill（高德地理编码 + 彩云天气）。

## 功能

- 输入中文地名（城市/区县/学校等）
- 通过 **高德地理编码** 获取坐标
- 通过 **彩云天气** 获取实况 + 近几日预报 + 小时预报
- 输出默认采用 Telegram 友好的排版（加粗分段 + bullet + 等宽小时块）
- 支持 `--mock/--debug/--cache-dir` 便于联调

## 使用

```bash
./scripts/weather_cn.py "北京市海淀区"
```

常用参数：

```bash
./scripts/weather_cn.py "北京市海淀区" --format json
./scripts/weather_cn.py "北京市海淀区" --detail full --hourly-steps 48
./scripts/weather_cn.py "北京市海淀区" --mock --debug
```

## 配置

在当前目录创建 `.env`（不要提交到 GitHub）：

```bash
AMAP_API_KEY=xxxx
CAIYUN_API_TOKEN=xxxx
```

也可以用参数覆盖：

```bash
./scripts/weather_cn.py "北京市海淀区" --amap-key xxxx --caiyun-token xxxx
```

## 注意

- `.env` 与 `cache*/` 都应保持在 `.gitignore` 内。
- 如果你计划用于 bot/自动化，建议把缓存目录指定到可写路径：`--cache-dir ./cache`。
