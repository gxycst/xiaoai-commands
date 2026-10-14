# 小爱亲子词库管理

原生 HTML/CSS/JS + Python 标准库，无 npm、无 pip、无数据库。独立运行一个管理容器，**不用改 xiaogpt.py，不用重建 xiaogpt，也不会自动创建或导入示例词库**。

## 1. 放到 NAS

SSH 登录 NAS，直接从仓库拉取（不要求 NAS 安装 Git）：

```sh
cd /volume2/ssd_docker
docker run --rm -v "$PWD:/repo" -w /repo alpine/git clone https://github.com/gxycst/xiaoai-commands.git
cd xiaoai-commands
sh configure.sh 192.168.31.192
docker compose up -d --build
```

最后两步会生成登录密码并启动管理容器。`192.168.31.192` 是本次部署使用的 NAS 地址，其他设备请替换为自己的内网 IP。如果同名目录已经存在，clone 会拒绝覆盖；先核对目录内容，不要删除原词库目录。完成后访问 `http://192.168.31.192:9528`。

也可以按下面步骤使用压缩包部署。

把压缩包解压到独立目录：

```text
/volume2/ssd_docker/xiaoai-commands/
  server.py
  static/
  Dockerfile
  docker-compose.yml
  configure.sh
  ...
```

已有词库保持在原位置：

```text
宿主机：/volume2/ssd_docker/xiaogpt/config/skills/commands.json
xiaogpt 容器：/config/skills/commands.json
```

管理容器把**整个现有 skills 目录**读写挂载到 `/config/skills`。挂载源不存在时 Compose 会报错，不会创建一个空目录冒充原词库。

## 2. 配置密码

SSH 登录 NAS，进入新目录：

```sh
cd /volume2/ssd_docker/xiaoai-commands
```

下面的 IP **替换成你 NAS 的真实内网 IPv4**：

```sh
sh configure.sh 192.168.1.100
```

脚本会生成 `.env` 和随机登录密码。请保存终端显示的密码；它只检查原词库是否存在，**不会修改原词库**。已有 `.env` 时拒绝覆盖。也可以复制 `.env.example` 为 `.env`，手动填写 NAS_LAN_IP 和 APP_PASSWORD（至少 12 个字符，不能使用示例密码）。

## 3. 启动、访问

```sh
docker compose up -d --build
```

手机连家庭 Wi-Fi，打开 `http://你的NAS内网IP:9528`，输入刚生成的密码。电脑也使用同一地址。首次构建需要下载 Python 基础镜像，应用运行不需要外部服务。

```sh
docker compose ps
```

查看问题：

```sh
docker compose logs --tail=50 manager
```

如果系统只提供旧命令，把上述 `docker compose` 换成 `docker-compose`；建议使用支持 `bind.create_host_path` 的 Compose v2。

## 4. 使用、验证

1. 登录后，应当直接看到你原有的两条规则。打开页面不会写文件。
2. 用搜索框查名字、关键词或回复。点编辑，把小爱误识别的同音词加到关键词里，一行一个。
3. 在命中测试中输入 `妞妞摔倒`，应显示「妞妞摔倒互动」、回复「陶妞妞摔倒了」、重复 10 次。输入 `陶雨欣摔倒了`，应显示「大名摔倒互动」及回复「陶润心摔倒了」。
4. 编辑、新建、删除、上下移动都是草稿操作；点击「保存词库」才写入文件。未保存的草稿也可测试，结果会标明状态。
5. 保存后会显示备份文件名。再向小爱说一次对应语句，确认 xiaogpt 实际使用新规则。

规则按数组顺序扫描，关键词按原顺序做**区分大小写的包含匹配**，取第一个命中的规则；不分词、不模糊匹配、不改写 query。测试显示单次回复和重复数，不调用小爱，也不调用模型。

**原对话没有提供 `local_command()` 的完整源码。** 这里的测试逻辑按上述约定实现；若现有函数用了完全相等、其他分隔符或归一化逻辑，请对照它核实。管理器保持 `name / keywords / reply / repeat` 的含义和 JSON 数组结构，保存时保留规则内已有的额外字段。

## 文件保护

- 缺失、损坏或不符合结构的 JSON 会提示错误；不自动修复、不覆盖、不导入 `example.commands.json`。
- 写入前严格校验：规则名、回复、关键词非空，关键词为数组，重复数为正整数，拒绝重复 JSON 键及非标准数值。请求体最多 2 MiB。
- 每次保存先备份原文件**完整字节**到同目录的 `.commands-backups/`，备份失败则不写入。
- 新文件在同一目录落盘后原子替换，保留原文件权限与所有者；原文件或备份目录为符号链接时拒绝写入。
- 用原文件内容摘要检查版本；其他手机、电脑或手动编辑已经改变文件时，返回冲突，要求先重新载入，不强行覆盖。
- 不自动删除历史备份。请定期把备份目录复制到别处，备份空间不足时保存会失败。
- 一个 skills 目录只启动一个管理实例。管理器之间使用锁；手动修改时先停止管理容器，避免外部编辑器在保存期间并发写入。

## 恢复某个备份

先停止管理容器：

```sh
docker compose stop manager
```

在 `/volume2/ssd_docker/xiaogpt/config/skills/.commands-backups/` 选好备份。文件名时间采用 UTC（北京时间加 8 小时）。把下面的占位文字替换为**完整备份文件名，不含目录**：

```sh
docker compose run --rm --no-deps manager python /app/restore.py '替换为完整备份文件名'
```

工具会校验备份、再次备份当前内容，然后原子恢复所选规则，保留当前文件权限和所有者，不删除选中的备份。恢复成功后启动管理器：

```sh
docker compose up -d manager
```

如果当前 commands.json 已丢失或损坏，恢复工具会拒绝覆盖；请先保留损坏文件，再由管理员核对备份后手动恢复，并保留原权限/所有者。不要直接复制权限为 0600 的备份替换原文件，否则非 root 的 xiaogpt 可能读不到它。

如果 xiaogpt 当初只挂载了单个 `commands.json` 文件，原子替换后它可能仍读到旧文件；整个 `config` 或 `skills` 目录挂载可直接读到新内容。需要时仅重启现有 xiaogpt 容器让它重新挂载，**不要重建、不要修改 xiaogpt.py**。

## 家庭局域网安全

Compose 只将端口发布到 `.env` 中指定的 NAS 内网地址。请不要给 9528 配置路由器端口转发、公网反向代理或穿透。应用还会检查客户端来自私有/回环地址，忽略 `X-Forwarded-For`，并要求密码及浏览器会话。

登录使用 HttpOnly、SameSite=Strict 会话 Cookie；写入和测试需要 CSRF 令牌，跨来源请求会被拒绝。登录失败有频率限制，会话有效期 8 小时。静态文件路径采用白名单，数据不会拼进 HTML。

当前是家庭 LAN 的 HTTP 服务，密码在可信 Wi-Fi 下使用。改密码：编辑 `.env` 后重新执行 `docker compose up -d`。停止或删除管理容器不会删除挂载的现有词库。

## 本地自测（可选）

Python 3.11 或更新版本即可：

```sh
python -m unittest discover -s tests -v
```

测试只使用临时目录，不碰你的 NAS 词库。实际自测结果与验证范围见 `SELFTEST.md`。

## 文件说明

`server.py`：服务器与文件保护；`static/`：手机/电脑页面；`docker-compose.yml`、`Dockerfile`：部署；`configure.sh`：生成配置；`restore.py`：安全恢复；`example.commands.json`：仅供参考；`tests/`：独立自测。

端口绑定方式参照 [Docker 官方说明](https://docs.docker.com/engine/network/port-publishing/)，同目录替换参照 [Python os.replace 文档](https://docs.python.org/3.11/library/os.html#os.replace)。
