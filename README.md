# 本地客服工单中心

面向本地单机使用的客服工单登记工具，采用 Python 3 标准库与 SQLite。

当前实现最小流程：**创建工单**与**按编号查看工单**，不包含分派、回复、状态变更或联网功能。

## 环境要求

- Python 3（仅使用标准库，无需安装第三方依赖）

## 入口与默认存储位置

```
python main.py [--db 数据库文件] <子命令> ...
```

- 全局选项 `--db` 指定 SQLite 数据库文件路径。
- 未提供 `--db` 时，默认使用**当前工作目录**下的 `tickets.sqlite`。
- 数据库文件尚不存在、且其父目录已存在时会自动创建；已有文件则继续使用，重复运行或再次创建工单都不会清空已有记录。
- 不同 `--db` 文件中的数据彼此独立。

## 创建工单

```
python main.py --db demo.sqlite create --title "登录失败" --description "重置密码后仍无法登录"
```

- 成功时退出码为 `0`，标准输出为一个 JSON 对象，字段包括：
  - `id`：同一数据库中唯一的正整数编号（新数据库首条为 `1`，重新运行后保持稳定）
  - `title`：标题，去除首尾空白后保存
  - `description`：问题描述，按原文保存，允许空字符串
  - `status`：初始状态固定为 `open`

  示例输出：

  ```json
  {"id": 1, "title": "登录失败", "description": "重置密码后仍无法登录", "status": "open"}
  ```

- 标题去除首尾空白后为空时拒绝创建：退出码 `1`，标准输出为空，标准错误输出 `标题不能为空`，不产生工单。

## 查看工单

```
python main.py --db demo.sqlite show 1
```

- 成功时退出码为 `0`，标准输出为包含 `id`、`title`、`description`、`status` 四个字段的 JSON 对象，数据与创建时一致，且查看不会改变数据。
- 编号不存在时：退出码 `1`，标准输出为空，标准错误输出 `工单不存在`。
- 编号不是正整数（例如 `0`、`-1`、`abc`）时：退出码 `1`，标准输出为空，标准错误输出 `工单编号必须为正整数`。

## 典型使用流程

```sh
# 在没有 demo.sqlite 的目录中执行
python main.py --db demo.sqlite create --title "登录失败" --description "重置密码后仍无法登录"
# -> {"id": 1, "title": "登录失败", "description": "重置密码后仍无法登录", "status": "open"}

# 再次查看，四个字段与创建结果一致
python main.py --db demo.sqlite show 1

# 空白标题创建失败，且不会影响已有工单
python main.py --db demo.sqlite create --title "   " --description "x"
# -> stderr: 标题不能为空（退出码 1）

# 查看不存在的编号
python main.py --db demo.sqlite show 999
# -> stderr: 工单不存在（退出码 1）
```
