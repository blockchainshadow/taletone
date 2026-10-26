# TaleTone 发布验证

## 实际执行

- 本机分发版：`python3 -m unittest discover -s local -p 'test_*.py' -q`，33项，成功，1项跳过，69.538秒。没有把跳过的真实神经模型生成当作验证通过。
- 干净私有目录使用明确的`--source`及`--data-dir`启动成功；HTTP项目接口返回仅一条原声、空片段的初始项目，不依赖仓库中的私人示例。服务仅监听loopback。
- 静态网页使用真实H.264/AAC视频与外部AAC配音验证。第一次发现音频waiting导致播放意图清零；已修复为缓冲暂停后canplay继续，不再以正常缓冲永久取消播放。
- 修复后外部配音首次播放成功：原视频muted=true；视频9.024826秒、实际外部音频8.970896秒，音频paused=false、readyState=4，瞬时偏差约54ms。此为单机单次样本，不是所有设备同步上限。
- 验证暂停、60秒seek、1.5倍速、明确选择原声后解除静音、再选外部配音后原声静音。
- 新手引导首屏可见、导航按钮可操作；真实导出JSON包含音轨元数据、所选音轨、60秒进度及1.5倍速。
- 新页面导入该JSON后，播放禁用、video无src、音轨要求重新关联；没有把元数据冒充恢复的媒体。已有媒体页面的导入确认在自动化中使标签页终止，未据此声称该确认流程通过。
- 公开代码检查未发现生产视频、私人源摘要、本机个人绝对路径或GitHub令牌；只部署web/。本机项目、音频、视频、缓存与模型不上传GitHub。

## 发布与域名

GitHub：https://github.com/blockchainshadow/taletone 。域名为用户确认的`papaw.cc`，不是最初误写的`papw.cc`。

Porkbun仅将根域名原停放ALIAS改为`blockchainshadow.github.io`；保留其他已有CNAME/TXT，不改nameservers。公开Google DNS已返回GitHub Pages四个185.199.*.153地址。GitHub Pages自定义域名已设置，证书状态approved，并启用HTTPS强制跳转。

## 未证明或不提供

公开网页不提供云端AI、自动连接本机服务、媒体上传或云端媒体恢复；本地AI需要另外安装并运行，当前发布回归不等于在全新设备完成真实MLX推理。浏览器后台节流和不同硬件可能影响双媒体同步，不承诺零漂移；只有用户主动选原声才启用原始声音。

## 线上最终检查

- GitHub Pages部署任务`40329976608`成功，提交`aceda4c`；线上`app.js`与已验证本地文件SHA256一致：`031dd565d584f18b79f990084b006f13cb1eaffb52ba5c947678fa9416aa3755`。
- `http://papaw.cc/`实际返回301到`https://papaw.cc/`；HTTPS网页可访问，品牌与引导正确显示。
- 在真实`https://papaw.cc/`重新选取本地视频与配音并点击声音卡片、播放：视频时钟0.651488秒，paused=false、muted=true，状态为配音播放中且原声静音。媒体没有作为仓库或站点资源发布。

