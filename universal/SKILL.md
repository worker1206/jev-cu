---
name: jev-cu
description: 用 Jev（TypeSafe System One）做快速判断的浏览器 computer-use 能力。需要操作网页、填表、检索信息、多步网页任务时使用。
---

# jev-cu

## 何时使用
- 用户要求操作浏览器完成多步任务（搜索、填表、导航、信息采集）
- 需要低成本、低延迟的网页自动化（Jev 负责每步判断，LLM 只在低置信时介入）

## 用法
```bash
jev-cu run "在维基百科搜索人工智能" --url https://www.wikipedia.org --max-steps 25
```
输出为 JSON（含 `schema_version`、每步决策日志路径、任务结果）。

## 边界与安全
- Jev 只做判断（选元素/置信度/完成度），不生成文本；需要生成时自动升级 LLM
- margin < 0.30 的步骤升级 LLM（实测该阈值可拦截全部已知错误）
- 危险动作（提交/支付/删除/发送）无条件要求人工确认
- 不绕过验证码、不规避站点反爬；请遵守目标站点条款

## 协议
本能力同时提供 MCP（stdio）接口：`browse` / `status`。不绑定任何特定宿主。
