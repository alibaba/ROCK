# 接口响应体说明

## 基础结构

| 字段 | 类型 | 描述 |
| --- | --- | --- |
| status | String | 枚举值：Sucess/Failed。<br>代表请求是否正常由沙箱执行完成。 |
| message | String | 响应信息。 |
| error | String | 表示当status=Failed时的错误描述信息。 |
| result | Object | 请求执行结果。根据具体功能，结构有所差异。 |
| └code | Integer | 响应状态码。参考下方状态码定义。 |
| └failure\_reason | String | 当code=5xxx时，包含一些额外的错误信息。 |
| └... |  | 根据不同接口，扩展信息有所不同。 |

### 响应状态码定义

| **值** | **描述** |
| --- | --- |
| 2xxx | 命令执行成功 |
| 5xxx | 服务端异常。该场景建议用户进行重试。 |
| 6xxx | 命令执行错误 |

## result扩展信息定义

### execute/run\_in\_session

| 字段 | 类型 | 描述 |
| --- | --- | --- |
| exit\_code | Integer | 命令执行退出码。<br>特殊退出码：-1（超时/网络/会话失效） |
| stdout | String | 命令执行标准输出 |
| stderr | String | 命令执行标准错误 |

### create\_session

| 字段 | 类型 | 描述 |
| --- | --- | --- |
| output | String | session创建结果。 |
| session\_type | String | session类型。<br>枚举值：<br>*   bash |

### close\_session

| 字段 | 类型 | 描述 |
| --- | --- | --- |
| session\_type | String | session类型。<br>枚举值：<br>*   bash |

### upload

| **字段** | **类型** | **描述** |
| --- | --- | --- |
| success | Boolean | 文件上传是否成功。 |
| message | String | 上传结果信息。 |
| file\_name | String | 文件名 |

现有响应结构固定返回默认值，需要调整。 

```shell
{
    "status": "Success",
    "message": null,
    "error": null,
    "result": {
        "success": false,
        "message": "",
        "file_name": ""
    }
}
```

### write\_file

| **字段** | **类型** | **描述** |
| --- | --- | --- |
| success | Boolean | 文件写入是否成功。 |
| message | String | 写入结果信息。 |

### read\_file

| **字段** | **类型** | **描述** |
| --- | --- | --- |
| content | String | 文件内容。 |