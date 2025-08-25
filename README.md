# 地震速报监听自动录制程序 - Obvious

一个用于监听日本气象厅(JMA)、中国地震预警网(CEA)和台湾气象署(CWA)地震预警信息的Windows服务程序。当检测到达到设定阈值的地震时，自动触发警报和OBS录制功能。

## 功能特点

- 🔔 **多源监听**: 同时监听日本(JMA)、中国(CEA)和台湾(CWA)的地震预警信息
- 📹 **自动录制**: 检测到地震时自动触发OBS录制功能
- ⏰ **智能过滤**: 内置时间窗口过滤机制，避免处理过早的地震信息
- 🔧 **服务管理**: 专为Windows服务环境优化，可通过NSSM管理
- 🎚️ **远程控制**: 内置HTTP控制服务器，支持远程暂停/恢复/测试等操作
- 📊 **状态监控**: 实时监控WebSocket连接状态和OBS录制状态
- 🧹 **日志管理**: 自动轮转和清理日志文件

## 系统要求

- Windows操作系统
- Python 3.7+
- OBS Studio 
- OBS WebSocket插件 

## 安装步骤

### 1. 安装Python依赖

```bash
pip install websocket-client winsound requests obs-websocket-py
```

### 2. 配置程序

编辑Python源代码中的以下配置项：

```python
# OBS WebSocket配置
obs_password = "你的OBS WebSocket服务器密码"  # 修改为你的OBS密码
obs_scene_name = "你的OBS场景"                # 修改为你的OBS场景名称

# 服务配置
service_name = "你为此程序注册的服务名"       # 修改为你的Windows服务名称

# 文件路径配置
alert_wav = r"你的文件路径"                  # 修改为警报音文件路径
```

### 3. 使用NSSM注册服务

```bash
nssm install "你的服务名" "python.exe" "你的脚本路径.py"
nssm set "你的服务名" AppDirectory "你的脚本所在目录"
```

### 4. 配置批处理文件

编辑批处理文件中的服务名和日志路径：

```bash
nssm start 【此处填写你注册的NSSM服务名】
notepad "【此处填写你的日志路径】"
```

## 配置说明

### 地震触发阈值

```bash
trigger_jma_intensity = "5弱"    # JMA触发阈值(震度)
trigger_cea_intensity = 7.0      # CEA触发阈值(烈度)
trigger_cwa_intensity = "5弱"    # CWA触发阈值(震度)
```

### 时间窗口设置

```bash
time_window_minutes = 10  # 忽略过早地震的时间窗口(分钟)
```

### 录制设置

```python
obs_record_duration = 600  # 录制持续时间(秒)，默认10分钟
```

## 使用方法

### 服务管理

使用配套的批处理文件或以下命令管理服务：

```bash
# 启动服务
nssm start 服务名

# 停止服务
nssm stop 服务名

# 重启服务
nssm restart 服务名

# 查看状态
nssm status 服务名
```

### HTTP控制接口

程序启动后，可以通过HTTP接口进行控制：

```tex
http://127.0.0.1:8787/command?cmd=命令
```

可用命令：

- `pause` - 暂停监听
- `resume` - 恢复监听
- `test` - 触发测试警报
- `status` - 获取当前状态
- `cleanuplogs` - 清理旧日志
- `wsstatus` - 查看WebSocket连接状态
- `obsstatus` - 查看OBS状态
- `obsstart` - 手动启动OBS录制
- `obsstop` - 手动停止OBS录制
- `timewindow` - 获取或设置时间窗口

## 文件结构

```tex
quake_monitor/
├── quake_monitor.py      # 主程序文件
├── service_manager.bat   # 服务管理批处理
├── logs/                 # 日志目录
│   └── quake_monitor.log # 主日志文件
└── README.md            # 说明文档
```

## 注意事项

1. 确保OBS WebSocket插件已正确安装和配置
2. 程序需要网络连接以访问地震预警服务
3. 警报音文件需要是有效的WAV格式
4. 服务运行时请勿直接关闭控制台窗口
5. 录制功能需要OBS Studio正在运行

## 故障排除

1. **服务无法启动**: 检查Python路径和脚本路径是否正确
2. **无法连接OBS**: 检查OBS WebSocket插件是否启用，密码是否正确
3. **无地震警报**: 检查网络连接和预警源配置
4. **录制不启动**: 检查OBS场景名称是否正确

------

**重要**: 此程序仅作为技术参考，地震预警信息请以官方发布为准。
