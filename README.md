# HR Agent

HR Agent 是一个面向人力资源场景的 AI 助手项目，包含 Vue 3 前端和 FastAPI 后端，支持智能招聘、知识库问答、简历评估、考试管理、邮件配置和系统权限管理等能力。

## 项目结构

```text
HR_Agent/
├── backend/                 # FastAPI 后端服务
│   ├── app/                 # API、模型、服务和核心配置
│   ├── scripts/             # 数据库和初始化脚本
│   ├── skills/              # HR Agent 技能配置
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── README.md
├── frontend/                # Vue 3 前端应用
│   ├── src/                 # 页面、路由、状态和接口封装
│   ├── package.json
│   └── README.md
└── README.md                # 项目总览
```

## 核心能力

- 用户认证与角色权限管理
- 智能招聘流程：JD 生成、简历筛选、面试辅助
- 知识库管理与 RAG 问答
- 考试生成、考试管理和自动评分
- 邮件配置与调度任务
- PostgreSQL + pgvector 向量检索
- Docker Compose 本地/服务器部署

## 技术栈

### 后端

- Python 3.10+
- FastAPI
- SQLAlchemy Async
- PostgreSQL + pgvector
- LangChain / LLM 服务集成
- Alembic
- Docker / Docker Compose

### 前端

- Vue 3
- Vite
- Pinia
- Vue Router
- Element Plus
- Axios
- SCSS

## 后端启动

进入后端目录：

```bash
cd backend
```

创建虚拟环境：

```bash
python -m venv .venv
```

Windows PowerShell 激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

安装依赖：

```bash
python -m pip install -r requirements.txt
```

复制环境变量模板：

```bash
cp .env.example .env
```

启动数据库和后端容器：

```bash
docker compose up -d
```

如果只在本机运行后端：

```bash
python main.py
```

接口文档地址：

```text
http://localhost:8000/api/v1/docs
```

## 前端启动

进入前端目录：

```bash
cd frontend
```

安装依赖：

```bash
npm install
```

启动开发服务：

```bash
npm run dev
```

默认访问：

```text
http://localhost:3000
```

## 环境变量说明

后端环境变量以 `backend/.env.example` 为模板。实际运行时复制为 `backend/.env` 后再填写。

注意：

- `backend/.env` 不应提交到仓库
- 数据库密码、JWT 密钥、模型 API Key 等敏感信息只能保存在本地或服务器环境中
- Docker 部署时可通过 `.env` 或平台环境变量注入配置

## 数据库说明

项目使用 PostgreSQL，并依赖 pgvector 扩展支持向量检索。

当前后端启动时会执行数据库初始化逻辑：

```text
FastAPI 启动
  ↓
初始化数据库连接
  ↓
启用 pgvector 扩展
  ↓
按 SQLAlchemy 模型创建表
```

角色初始化脚本：

```bash
cd backend
python scripts/seed_roles.py
```

## 部署说明

后端提供 `Dockerfile` 和 `docker-compose.yml`。如果 Docker 运行在 Linux 服务器上，需要在服务器中执行 compose 命令，而不是在 Windows 本地执行。

```bash
cd backend
docker compose up -d
```

如果前端和后端分开部署，需要确认前端 API 地址指向后端服务地址。

## 提交说明

本仓库已配置忽略规则，默认不提交以下内容：

- Python 虚拟环境 `.venv/`
- Node 依赖目录 `node_modules/`
- 环境变量文件 `.env`
- 运行日志 `logs/`
- 上传文件 `uploads/`
- 构建产物 `dist/`、`build/`
- 编辑器缓存和系统临时文件

## 参考文档

- 后端说明：`backend/README.md`
- 前端说明：`frontend/README.md`