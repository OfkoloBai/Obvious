****

# 地震速报监听程序Obvious

感谢[Wolfx Project](https://wolfx.jp/)和[FAN Studio API](https://api.fanstudio.tech/)对本项目的大力支持！

一个用于监听日本气象厅(JMA)和中国地震预警网(CEA)地震预警信息的Python程序。当检测到达到设定阈值的地震时，会自动触发警报和OBS录制。

# 已知 Bug

 ⚠️ 注意： 本程序在第一次自动录制自动停止录制后，会由于不明原因无法再次自动启动录制！
 
 解决方法： 请在自动录制触发后手动重启程序或服务！

## 功能特点

- 监听日本气象厅(JMA)和中国地震预警网(CEA)的地震预警信息
- 可自定义触发阈值（JMA震度和CEA烈度）
- 达到阈值时自动播放警报音效
- 自动启动OBS进行录制
- 系统托盘图标控制
- HTTP API远程控制
- 详细的日志记录和轮转

## 系统要求

- Windows 10（目前仅在Windows 10上测试过）
- Python 3.7+
- OBS Studio（需要预先安装并配置）

## 安装步骤

1. 克隆或下载此仓库到本地

```bash
git clone <仓库地址>
```

2. 安装必要的Python依赖：

```bash
pip install websocket-client winsound pystray pillow win10toast
```

3. 根据您的环境修改代码中的配置项（特别是OBS路径和警报音文件路径）

打开 `obvious_rec.py` 文件，找到以下配置项并进行修改：

```python
# 默认配置 - 用户需要根据自己的环境修改这些配置
DEFAULT_CONFIG = AppConfig(
    obs_path=r"请填写您的OBS路径，例如：C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    obs_dir=r"请填写您的OBS目录，例如：C:\Program Files\obs-studio\bin\64bit",
    record_path=os.path.expanduser(r"~\Videos"),  # 默认录制到用户视频目录
    record_duration=360,  # 录制时长(秒)
    cooldown=360,  # 冷却时间(秒)
    alert_wav=r"请填写您的警报音文件路径，例如：alarm.wav",
    toast_app_name="地震速报监听",
    trigger_jma_intensity="5弱",  # JMA触发阈值
    trigger_cea_intensity=7.0,  # CEA触发阈值(烈度)
    ws_jma="wss://ws-api.wolfx.jp/jma_eew",  # JMA WebSocket地址
    ws_cea="wss://ws.fanstudio.tech/cea",  # CEA WebSocket地址
    control_port=8787,  # HTTP控制端口
    log_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")  # 日志目录
)
```

4. 运行程序：

```bash
python obvious_rec.py  
```
其次，为了更稳定运行，我们建议使用[NSSM](https://nssm.cc/download)将程序加载为Windows服务来运行


## 配置说明

在运行程序前，您需要修改代码中的以下配置项：

- `obs_path`: OBS可执行文件的完整路径
- `obs_dir`: OBS所在的目录
- `alert_wav`: 警报音效文件的完整路径
- `record_path`: 录制视频的保存路径
- `trigger_jma_intensity`: JMA触发阈值（如"5弱"）
- `trigger_cea_intensity`: CEA触发阈值（如7.0）

## 使用方法

1. 运行程序后，它会最小化到系统托盘
2. 右键点击托盘图标可以：
   - 暂停/恢复监听
   - 打开日志文件
   - 清理日志文件
   - 退出程序
3. 程序也可以通过HTTP API进行控制：

```bash
# 暂停监听
curl "http://127.0.0.1:8787/command?cmd=pause"

# 恢复监听
curl "http://127.0.0.1:8787/command?cmd=resume"

# 触发测试警报
curl "http://127.0.0.1:8787/command?cmd=test"

# 获取当前状态
curl "http://127.0.0.1:8787/command?cmd=status"

# 清理日志
curl "http://127.0.0.1:8787/command?cmd=cleanuplogs"
```

## 注意事项

- 此程序目前仅在Windows 10上测试过，不保证在其他操作系统上的兼容性
- 需要预先安装并配置好OBS Studio
- 程序使用WebSocket连接接收预警信息，需要稳定的网络连接
- 请根据您所在地区的地震风险合理设置触发阈值

## 故障排除

如果遇到问题，请检查：

1. OBS路径和警报音文件路径是否正确
2. 网络连接是否正常
3. 查看日志文件（位于程序目录下的logs文件夹中）

常见问题：

1. **无法启动OBS录制**：请确认OBS路径配置正确，且OBS已正确安装
2. **无法播放警报音**：请确认警报音文件路径正确，且文件格式为WAV
3. **无法连接到预警服务器**：请检查网络连接，特别是防火墙设置

## 免责声明

此程序仅作为技术演示和个人使用，不保证预警信息的准确性和及时性。对于因使用此程序而导致的任何直接或间接损失，作者不承担任何责任。请勿将此程序用于关键安全应用。
