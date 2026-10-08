# 贡献指南

感谢你愿意改进这个项目。它是一个自用的 ComfyUI 前端，代码风格偏向「实用优先、少抽象」，请尽量沿用现有风格。

## 开发环境

```bash
git clone https://github.com/<你的账号>/comfyui-webapp.git
cd comfyui-webapp
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

前置条件：本机已运行 ComfyUI（默认 `http://127.0.0.1:8188`）。

首次启动：

```bash
copy config.example.json config.json   # 按注释改成你的路径
python server.py
```

若 `config.json` 不存在，程序会使用通用默认值启动，并在控制台打印实际生效的配置。

## 提交前检查

1. `python -m py_compile server.py config.py ip_monitor.py` 通过。
2. 本地起服务后，至少手动过一遍**登录 → 首页列表 → 一个生成模式 → 任务结果查看**。
3. **确认没有提交任何凭据**：
   ```bash
   git status --short
   git check-ignore auth_config.json config.json llm_api_config.json mail_config.json
   ```
   上面四个文件都应被忽略。若你新增了配置文件，请同时更新 `.gitignore` 并提供对应的 `*.example.json`。

## 代码约定

- **不要硬编码个人环境。** 路径、端口、账号一律走 `config.py`（新增键请同步更新 `config.example.json` 与本 README 的配置表）。
- 前端为原生 HTML/CSS/JS，无构建步骤；改动 `static/` 后直接刷新即可。
- 后端是单文件 FastAPI 应用 `server.py`，改动较大时请在 PR 描述里说明影响的路由。
- 注释与提交信息使用中文或英文均可，但请写清「为什么」而不只是「做了什么」。

## 提交信息

沿用仓库现有风格，例如：

```
feat(music): 支持卡拉OK 歌词逐字对齐
fix(auth): 会话过期后正确跳回登录页
docs: 补充 config.json 字段说明
```

## Pull Request

请在描述中包含：**问题现象 → 修改思路 → 验证方式**。涉及界面改动的，附一张截图会很有帮助。
