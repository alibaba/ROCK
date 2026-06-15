# Start from Dockerfile — Requirement Spec

## Background

ROCK SDK 目前只支持通过预构建镜像（`SandboxConfig.image`）启动沙箱。调用方必须事先准备好镜像并推送到 registry，再将镜像名传入 `Sandbox.start()`。

在实际使用中，Harbor 的任务通常只提供一个包含 Dockerfile 的目录（`environment_dir`），而非预构建镜像，这类任务目前无法直接通过 ROCK SDK 启动沙箱。

本次需求：ROCK SDK 支持接收一个包含 Dockerfile 的目录（`environment_dir`）启动沙箱。

---

## Scope

输入 `environment_dir`（本地目录，包含 Dockerfile），启动沙箱。

---

## Acceptance Criteria

- **AC1**: 给定 `environment_dir`，成功启动沙箱，沙箱内可访问 Dockerfile 中 COPY 的文件
- **AC2**: 镜像已存在时，跳过构建直接启动

---

## Constraints

- 不引入新的 Python 依赖
- 不新增 Admin API 接口

---

## Risks

| 风险 | 影响 | 缓解 |
|------|------|------|
| 大构建上下文传输慢 | 启动延迟增加 | 利用 OSS 中转加速 |
