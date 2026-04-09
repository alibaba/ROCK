# Add Doc Version

为 ROCK 项目添加新版本的 Docusaurus 文档。

## 项目文档背景

- 文档框架：Docusaurus 3.9.x，部署在 `https://alibaba.github.io/ROCK/`
- 语言：英文（默认）+ 中文（zh-Hans）
- 版本管理：`includeCurrentVersion: false`，只发布 versioned docs
- 所有文档文件均在 `docs/` 目录下

## 需要修改的文件和目录

添加新版本时，必须操作以下 **6 个位置**：

| # | 操作 | 路径 |
|---|------|------|
| 1 | 创建英文文档目录 | `docs/versioned_docs/version-{NEW}/` |
| 2 | 创建版本侧边栏 | `docs/versioned_sidebars/version-{NEW}-sidebars.json` |
| 3 | 更新版本列表 | `docs/versions.json` |
| 4 | 更新最新版本指向 | `docs/docusaurus.config.js` 中的 `lastVersion` |
| 5 | 创建中文文档目录 | `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}/` |
| 6 | 创建中文侧边栏翻译 | `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}.json` |

## 每个版本的标准目录结构

英文和中文文档目录结构一致：

```
version-X.Y.x/
├── Getting Started/
│   ├── quickstart.md
│   ├── installation.md
│   └── ...
├── User Guides/
│   └── ...
├── References/
│   ├── api.md
│   └── Python SDK References/
│       ├── python_sdk.md
│       └── ...
├── Release Notes/
│   ├── index.md
│   └── vX.Y.Z.md
└── overview.md
```

## 执行流程

### Phase 0：版本冲突检测（前置检查）

在执行任何操作之前，必须先进行版本冲突检测：

1. 读取 `docs/versions.json` 获取已有版本列表
2. 从用户输入的版本号推断文档版本号（如 `1.4.5` → `1.4.x`）
3. **检查推断出的文档版本号是否已存在于 `versions.json` 中**

如果文档版本已存在（例如用户输入 `1.4.5`，但 `1.4.x` 已在版本列表中），则：

- **不执行完整的新版本创建流程**
- 提示用户：`版本 1.4.x 的文档已存在，无需创建新版本。仅需在现有版本中添加 Release Note 即可。`
- **直接跳转到「仅添加 Release Note」流程**（见下方）

#### 仅添加 Release Note 流程

当文档版本已存在时，只需执行以下 3 步操作：

**Step A：创建英文 Release Note 文件**

在 `docs/versioned_docs/version-{EXISTING}/Release Notes/v{VERSION}.md` 创建文件，使用 Phase 2 Step 3 中定义的**英文模板**。

**Step B：创建中文 Release Note 文件**

在 `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{EXISTING}/Release Notes/v{VERSION}.md` 创建文件，使用 Phase 2 Step 3 中定义的**中文模板**。

**Step C：更新 Release Notes 索引（index.md）**

在英文和中文的 `Release Notes/index.md` 文件的链接列表**顶部**插入新版本条目。

英文文件 `docs/versioned_docs/version-{EXISTING}/Release Notes/index.md`，在标题行下方第一行插入：
```markdown
* [release v{VERSION}](v{VERSION}.md)
```

中文文件 `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{EXISTING}/Release Notes/index.md`，同样在标题行下方第一行插入：
```markdown
* [release v{VERSION}](v{VERSION}.md)
```

例如，添加 `v1.4.5` 后，英文 index.md 应变为：
```markdown
---
sidebar_position: 1
---
# Release Notes
* [release v1.4.5](v1.4.5.md)
* [release v1.4.4](v1.4.4.md)
* [release v1.4.3](v1.4.3.md)
...
```

创建完成后直接跳转到 Phase 5 验证步骤。

---

如果文档版本不存在，继续执行以下完整流程。

### Phase 1：收集信息

使用交互方式向用户询问：

1. **新版本号**：如 `1.4.x`
2. **基础版本**：从哪个版本复制？默认取 `docs/versions.json` 的第一项（即最新版本）
3. **Release Note**：是否创建新的 Release Note？若是，具体版本号是什么（如 `v1.4.0`）？
4. **是否修改 `lastVersion`**：**必须明确告知用户影响后再确认**。提示内容如下：

   > ⚠️ 修改 `lastVersion` 会改变文档站点的默认展示版本。
   > 当前默认展示版本为 `{当前 lastVersion}`，如果修改为 `{NEW}`，用户访问文档时将默认看到新版本内容。
   > 是否确认修改？

   **不要默认选「是」**，必须等用户明确确认。

如果用户给的是 `1.4.0` 这样的具体版本号，自动推断文档版本号为 `1.4.x`。

### Phase 2：创建英文文档（3 步）

#### Step 1：复制英文文档目录

```bash
cp -r docs/versioned_docs/version-{BASE}/ docs/versioned_docs/version-{NEW}/
```

#### Step 1.5：清理旧版本 Release Notes（仅大版本号变更时）

判断新版本的主版本号（major.minor）是否与基础版本不同。例如：
- `1.4.x` → `1.5.x`：主版本号从 `1.4` 变为 `1.5`，**需要清理**
- `1.4.x` → `1.4.x`：同主版本号，不需要清理（且此情况会被 Phase 0 拦截）

当主版本号变更时，复制过来的 `Release Notes/` 目录中包含的是旧版本的 Release Notes，需要清理：

1. **删除** `docs/versioned_docs/version-{NEW}/Release Notes/` 下除 `index.md` 之外的所有 `v*.md` 文件
2. **重写** `index.md`，仅保留框架：

```markdown
---
sidebar_position: 1
---
# Release Notes
```

这样新版本的 Release Notes 目录就是干净的，不会携带旧版本的发布说明。

#### Step 2：复制版本侧边栏

```bash
cp docs/versioned_sidebars/version-{BASE}-sidebars.json docs/versioned_sidebars/version-{NEW}-sidebars.json
```

侧边栏文件内容无需修改（除非新版本文档结构有变化）。

#### Step 3：创建 Release Note（如果需要）

英文和中文使用不同的模板。

**英文模板**：在 `docs/versioned_docs/version-{NEW}/Release Notes/v{VERSION}.md` 创建：

```markdown
# v{VERSION}

## Release Date
{Mon DD, YYYY}

---

TODO
```

**中文模板**：在 `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}/Release Notes/v{VERSION}.md` 创建：

```markdown
# v{VERSION}

## 发布日期
{YYYY} 年 {M} 月 {D} 日

---

TODO
```

注意中英文模板的差异：
- 标题部分：`## Release Date` vs `## 发布日期`
- 日期格式：`Mar 14, 2026` vs `2026 年 3 月 14 日`

#### Step 3.5：更新 Release Notes 索引（如果创建了 Release Note）

在英文 `docs/versioned_docs/version-{NEW}/Release Notes/index.md` 的标题行下方第一行插入：
```markdown
* [release v{VERSION}](v{VERSION}.md)
```

### Phase 3：创建中文文档（2 步）

#### Step 4：复制中文文档目录

```bash
cp -r docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{BASE}/ docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}/
```

#### Step 4.5：清理中文旧版本 Release Notes（仅大版本号变更时）

与 Step 1.5 同理，当主版本号变更时，需要清理中文目录下的旧 Release Notes：

1. **删除** `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}/Release Notes/` 下除 `index.md` 之外的所有 `v*.md` 文件
2. **重写** `index.md`，仅保留框架：

```markdown
---
sidebar_position: 1
---
# 版本说明
```

注意中文版 index.md 的标题是 `# 版本说明`，而非 `# Release Notes`。

#### Step 5：创建中文侧边栏翻译 JSON

```bash
cp docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{BASE}.json docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}.json
```

然后修改新 JSON 文件中的 `version.label`：

```json
{
  "version.label": {
    "message": "{NEW}",
    "description": "The label for version {NEW}"
  }
}
```

其他侧边栏分类翻译（快速上手、用户指南、参考、版本说明等）保持不变。

如果创建了 Release Note，也要在中文目录下使用**中文模板**创建对应文件：
`docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}/Release Notes/v{VERSION}.md`

并在中文 `docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{NEW}/Release Notes/index.md` 的标题行下方第一行插入：
```markdown
* [release v{VERSION}](v{VERSION}.md)
```

### Phase 4：更新配置（2 步）

#### Step 6：更新 versions.json

在 `docs/versions.json` 数组**开头**插入新版本号：

```json
[
  "{NEW}",
  "1.3.x",
  "1.2.x",
  ...
]
```

#### Step 7：更新 docusaurus.config.js（仅在用户明确确认后）

**只有用户在 Phase 1 中明确确认要修改 `lastVersion` 时才执行此步骤。**

修改 `docs/docusaurus.config.js` 中的 `lastVersion`：

```js
// 找到这一行
lastVersion: '{BASE}',
// 替换为
lastVersion: '{NEW}',
```

如果用户选择不修改，则跳过此步骤，保持原有的 `lastVersion` 不变。

### Phase 5：验证

完成所有步骤后，执行以下验证流程：

#### Step 8：构建验证

先执行构建，确保没有编译错误：

```bash
cd docs && npm run build
```

构建成功后，再启动本地预览服务：

```bash
cd docs && npm run serve
```

> **为什么用 `npm run serve` 而不是 `npm run start`？**
> `npm run start` 只启动开发模式，默认只加载默认语言（英文），无法切换到中文验证。
> `npm run serve` 基于构建产物启动静态服务，可以同时预览英文和中文版本，支持通过语言切换器验证中英文文档。

#### Step 9：手动检查清单

提示用户在浏览器中检查以下内容：

```
✅ 文档验证清单：

英文版本检查：
   □ 版本下拉菜单中是否显示新版本
   □ 默认展示的版本是否符合预期（取决于是否修改了 lastVersion）
   □ 英文文档页面是否正常渲染
   □ Release Notes 是否按版本号倒序排列
   □ 侧边栏导航是否完整

中文版本检查（通过右上角语言切换器切换到中文）：
   □ 中文文档页面是否正常显示
   □ 侧边栏分类名称是否正确翻译（快速上手、用户指南、参考、版本说明）
   □ Release Notes 内容是否与英文版本对应

后续工作：
   □ 更新新版本英文文档中的具体内容
   □ 更新中文文档翻译内容
   □ 如有新的 Release Note，补充具体发布内容
```

## 注意事项

- `docs/rock/` 是 "current"（未发布）版本源码，因为 `includeCurrentVersion: false` 所以不会发布。所有发布内容来自 `versioned_docs/`。
- `docusaurus.config.js` 通过 `convertVersionsArrayToObject()` 从 `versions.json` 自动生成版本配置，因此只需在 `versions.json` 添加即可完成版本注册。
- 侧边栏使用 `autogenerated` 模式，新文件放入正确目录后会自动出现在侧边栏。
- Release Notes 通过 `reverseReleaseNoteSidebars()` 自动按版本号倒序排列。
- `HiddenSidebars` 数组中的文件（`Getting Started/quickstart`、`References/Python SDK References/python_sdk`、`Release Notes/index`）会从侧边栏隐藏，但仍可通过直接链接访问。

## 扩展参考：Docusaurus i18n 与侧边栏配置

当用户询问如何添加新的侧边栏分类、如何添加翻译、如何配置 i18n 等问题时，参考 Docusaurus 官方文档：

**官方 i18n 教程**：https://docusaurus.io/docs/i18n/tutorial

关键知识点：

### 添加侧边栏分类

本项目的侧边栏采用 **“顶层手动 + 内层自动”** 的混合模式：

- **顶层分类**（如 Getting Started、User Guides、References、Release Notes）是在侧边栏配置中**手动定义**的
- **内层文档**通过 `autogenerated` 从目录中**自动生成**，新文件放入对应目录即可自动出现

因此，**添加新的顶层侧边栏分类**时，需要手动修改以下文件：

1. **主侧边栏配置**：`docs/sidebars.js` — 添加新的顶层 category
2. **各版本侧边栏**：`docs/versioned_sidebars/version-{VERSION}-sidebars.json` — 在需要显示新分类的版本中添加对应的 category 配置
3. **中文翻译 JSON**：`docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-{VERSION}.json` — 添加 `sidebar.tutorialSidebar.category.{CategoryName}` 条目

顶层分类的配置格式示例（以 `version-1.4.x-sidebars.json` 为例）：

```json
{
  "type": "category",
  "label": "New Category",
  "items": [
    {
      "type": "autogenerated",
      "dirName": "New Category"
    }
  ]
}
```

同时需在 `versioned_docs/version-{VERSION}/` 和对应的 i18n 目录下创建对应的文件夹，内层文档会自动生成侧边栏条目。

而 **在已有分类下添加新文档**，则无需修改侧边栏配置，直接将 md 文件放入对应目录即可。

### 添加翻译

本项目的翻译文件存储在 `docs/i18n/zh-Hans/` 目录下，主要包括：

| 文件/目录 | 用途 |
|---------|------|
| `docusaurus-plugin-content-docs/version-{V}.json` | 侧边栏分类名称和版本标签的翻译 |
| `docusaurus-plugin-content-docs/version-{V}/` | 各版本文档内容的中文翻译 |
| `docusaurus-theme-classic/navbar.json` | 导航栏文本翻译 |
| `docusaurus-theme-classic/footer.json` | 页脚文本翻译 |
| `code.json` | React 代码中的文本标签翻译 |

提取翻译 key 的命令：
```bash
cd docs && npm run write-translations -- --locale zh-Hans
```

该命令会自动扫描项目代码和配置，生成需要翻译的 JSON 文件。生成后编辑对应的 JSON 文件填入中文翻译即可。

### 翻译文档内容

文档内容的翻译通过在 i18n 目录下创建对应的 markdown 文件实现。文件路径必须与英文原文完全对应：

```
英文原文：docs/versioned_docs/version-1.4.x/User Guides/example.md
中文翻译：docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-1.4.x/User Guides/example.md
```

如果中文目录下不存在对应文件，Docusaurus 会回退到英文原文展示。
