# 心穹中枢

![检查](https://github.com/Atri-YTL/xinqiong/actions/workflows/ci.yml/badge.svg)
![许可](https://img.shields.io/badge/license-AGPL--3.0-blue)

把机器人的记忆库渲染成**一颗会呼吸的 3D 星图球体**。

## 长什么样

- 中心是「原意识」，每段记忆按黄金角散布成球
- **形态**表示清晰度：实心＝清晰，半透明＝模糊，空心＝只剩印象，虚线＝已遗忘
- **颜色**表示情绪：亲近 / 好奇 / 开心 / 担心 / 认真 / 委屈 / 平静
- **大小**＝被想起的次数，**饱和度**＝离现在多久（越旧越灰），**外环**＝最新那批
- 点进中心会亮起「**万家灯火**」：所有星星依次点亮，同情绪的星之间拉出会呼吸的线
- `▶ 回放` 按日期从第一天重演记忆的生长，结尾 7 种情绪线逐个亮起、最后整张网一起亮
- 时间轴可以拖动，翻过去的记忆会留成灰影

## 装法

需要 **AstrBot >= 4.29**。

```
1. 把 plugins 里两个目录整个复制到 <AstrBot>/data/plugins/
     astrbot_plugin_console     ← 星图页面和它的 API 都在这
     astrbot_plugin_xinqiong    ← 把星图挂成侧边栏独立入口
2. WebUI → 插件 → 重载（或重启 AstrBot）
3. 侧边栏「扩展功能」→ 心穹中枢
```

> 两个都要装。`xinqiong` 只负责入口，页面由 `console` 生成后镜像过去。
> 首次进入如果空白，等 20~30 秒（`console` 每 30 秒重写一次数据）。

## 数据和隐私

星图数据从你自己的 AstrBot 里读：会话记录、`botdata/memory` 里的记忆日档、记忆衰减插件的清晰度字段。
**这个仓库里不含任何运行数据**——`pages/*/data.json`、`pages/*/index.html` 都在 `.gitignore` 里。

## 依赖的其它插件

星图本身能跑，但语义更完整需要配套：

- `memory_decay`（清晰度衰减 + 被提起就复习）——「被想起次数」「越旧越灰」来自它
- `memory_lib`（按情绪/清晰度挑选要注入的记忆）

这两个在作者的另一个仓库里，没有也能用，只是星星不会衰减。

## 许可

AGPL-3.0-or-later，见 `LICENSE` / `NOTICE`。

Copyright (C) 2026 心穹中枢
