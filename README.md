# astrbot_plugin_bili_audio

B 站音频提取插件：发送 `/提取音频 BV号`，插件会提取该视频的音频，再按 60 秒切片连续发送可点击的 QQ 语音条；支持扫码登录 B 站账号。

可以让群友直接在群里听司冯音乐。

## 功能

| 指令 | 权限 | 行为 |
| --- | --- | --- |
| `/提取音频 BV号` | 所有人 | 提取音频，按 60 秒切片，连续发送多条 QQ 语音，单文件上限 100MB |
| `/账号登录` | 管理员 | 发送 B 站登录二维码，扫码后自动保存 Cookie |
| `/账号状态` | 管理员 | 查看当前使用的是扫码账号还是配置 Cookie |
| `/退出登录` | 管理员 | 清除扫码登录凭据 |

## 使用方法

1. 在 B 站视频页复制 BV 号，例如 `BV1ikYa6zEkp`；
2. 发送 `/提取音频 BV1ikYa6zEkp`；
3. 机器人先回复视频标题，然后逐条发送语音。

插件只接受 BV 号，会自动拼成
`https://www.bilibili.com/video/BV号`。分 P 视频默认提取 P1。

## 扫码登录（可选）

插件加载后不需要手动找 Cookie：

1. AstrBot 管理员在私聊或群里发送 `/账号登录`；
2. 机器人发送二维码图片；
3. 用哔哩哔哩 App 扫码，并在手机上确认；
4. 机器人提示登录成功，凭据写入
   `data/plugin_data/astrbot_plugin_bili_audio/credentials.json`；
5. 后续 `/提取音频` 自动使用该账号。

扫码凭据优先于 WebUI 里配置的 `cookie`。发送 `/退出登录` 后会回退到配置里的
Cookie。该 JSON 文件包含账号凭据，请不要外传。

> `/账号登录` 默认只允许 AstrBot 管理员触发，避免群友把自己的账号绑到机器人上。
> 如果私聊里也无法使用，请先在 AstrBot 配置里把自己的 QQ 设为管理员。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `audio_chunk_seconds` | `60` | 语音切片长度，QQ 语音上限 60 秒 |
| `max_audio_chunks` | `30` | 单次最多发送多少段语音 |
| `audio_send_interval` | `0.6` | 多段语音发送间隔（秒） |
| `max_audio_mb` | `100` | 音频文件大小上限 |
| `send_progress` | `true` | 是否先发一条“正在提取音频”的提示 |
| `cooldown_seconds` | `20` | 单用户冷却，0 表示关闭 |
| `max_concurrent` | `1` | 同时处理的任务数 |
| `timeout_seconds` | `60` | 单次解析/下载/转码超时 |
| `max_parse_mb` | `16` | yt-dlp 解析结果和扫码凭据文件的最大读取大小 |
| `cookie` | 空 | B 站 Cookie，扫码登录优先；也可手动填 SESSDATA |
| `proxy` | 空 | 下载代理，如 `http://127.0.0.1:7890` |
| `ffmpeg_path` | 空 | 自定义 ffmpeg 路径，留空自动查找 |

## QQ 语音说明

插件发送的是 AstrBot 的 `Record` 消息段：

- OneBot v11 / NapCat：由 NapCat 转成 silk 并显示为语音条；
- QQ 官方机器人：由 AstrBot 转成 `tencent_silk` 并走富媒体上传。

普通 QQ 语音最长 60 秒，所以插件会把音频切片后逐条发送；超过
`max_audio_chunks` 的视频只发送前 N 段，并给出提示。

## License

MIT，详见 [LICENSE](LICENSE)。
