# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`POST /api/basis`：id、整数revision、key(批次|检测项|样品编号)、basis取OWN_SPIKE、BATCH_AVG、EXTERNAL。依据不可用422且原报告保留；旧版本409。成功后批次revision+1，resolved按新依据重算。

`GET /api/export/{id}`：唯一证据CSV，含manifest、result(质控)、resolved(签发视图)、quarantine、raw(原始进样)五类记录。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
