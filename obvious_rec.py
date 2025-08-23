"""
地震速报监听程序 - 建议在PC上使用
监听日本气象厅(JMA)和中国地震预警网(CEA)的地震预警信息
当检测到达到设定阈值的地震时，自动触发警报和OBS录制
注意：此代码目前只在Windows10上勉强调试运行过，不一定适合所有人
"""
import os
import json
import time
import logging
import threading
import subprocess
import http.server
import socketserver
import sys
import io
import glob
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any, List, Callable, Set
from logging.handlers import RotatingFileHandler
from enum import Enum, auto
from pathlib import Path

# 第三方库
import websocket
import winsound
import pystray
from PIL import Image, ImageDraw
from win10toast import ToastNotifier

# =========================
# 枚举和常量定义
# =========================
class AlertSource(Enum):
    """预警来源枚举"""
    JMA = auto()  # 日本气象厅
    CEA = auto()  # 中国地震预警网
    TEST = auto()  # 测试来源

class FilteredStderr(io.TextIOWrapper):
    """过滤特定错误信息的 stderr 包装器"""
    def write(self, text):
        # 过滤掉特定的错误信息
        if "WNDPROC return value cannot be converted to LRESULT" in text:  # 过滤 WNDPROC 返回值转换错误
            return
        if "WPARAM is simple, so must be an int object (got NoneType)" in text:  # 过滤 WPARAM 类型错误
            return
        super().write(text)  # 调用父类的 write 方法写入未被过滤的内容

class ProgramState(Enum):
    """程序状态枚举"""
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()


# JMA震度映射表（从小到大）
JMA_INTENSITY_MAP = {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5弱": 5,
    "5強": 6,
    "6弱": 7,
    "6強": 8,
    "7": 9
}

# =========================
# 配置类（使用dataclass管理配置）
# =========================
@dataclass
class AppConfig:
    # OBS相关配置
    obs_path: str
    obs_dir: str
    record_path: str
    record_duration: int
    
    # 触发和冷却配置
    cooldown: int
    trigger_jma_intensity: str
    trigger_cea_intensity: float
    
    # 警报和通知配置
    alert_wav: str
    toast_app_name: str
    
    # WebSocket和网络配置
    ws_jma: str
    ws_cea: str
    control_port: int
    
    # 日志和文件配置
    log_dir: str
    max_log_size: int = 5 * 1024 * 1024  # 5MB
    log_backup_count: int = 5
    log_retention_days: int = 30  # 日志保留天数
    
    # WebSocket连接配置
    ws_reconnect_delay: int = 5  # WebSocket重连延迟(秒)
    ws_max_reconnect_delay: int = 60  # 最大重连延迟(秒)
    ws_reconnect_backoff: float = 1.5  # 重连退避系数
    ws_ping_interval: int = 30  # 常规Ping间隔(秒)
    ws_ping_timeout: int = 10  # 常规Ping超时(秒)
    wolfx_ping_interval: int = 55  # Wolfx API Ping间隔(略小于60秒)
    wolfx_ping_timeout: int = 10  # Wolfx API Ping超时(秒)
    
    # 其他配置
    alarm_repeat_count: int = 3  # 警报音重复次数
    log_cleanup_interval: int = 86400  # 日志清理间隔(秒)，默认每天一次
    debug_mode: bool = False  # 调试模式，开启更详细的日志
    
    def to_dict(self) -> Dict[str, Any]:
        """将配置转换为字典"""
        return asdict(self)


# 默认配置 - 用户需要根据自己的环境修改这些配置
DEFAULT_CONFIG = AppConfig(
    obs_path=r"【改为你自己的OBS可执行文件路径，如C:\Program Files\obs-studio\bin\64bit\obs64.exe】",
    obs_dir=r"【改为你自己的OBS路径，如C:\Program Files\obs-studio\bin\64bit】",
    record_path=os.path.expanduser(r"~\Videos"),  # 默认录制到用户视频目录
    record_duration=360,  # 录制时长(秒)
    cooldown=360,  # 冷却时间(秒)
    alert_wav=r"【改为你自己的音频文件路径，如E:\EEW\Media\eewwarning.wav】",
    toast_app_name="地震速报监听",
    trigger_jma_intensity="5弱",  # JMA触发阈值
    trigger_cea_intensity=7.0,  # CEA触发阈值(烈度)
    ws_jma="wss://ws-api.wolfx.jp/jma_eew",  # JMA WebSocket地址
    ws_cea="wss://ws.fanstudio.tech/cea",  # CEA WebSocket地址
    control_port=8787,  # HTTP控制端口
    log_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")  # 日志目录
)


# =========================
# 连接状态跟踪类
# =========================
class ConnectionStatus:
    """跟踪WebSocket连接状态"""
    def __init__(self):
        self.last_message_time = 0
        self.connected = False
        self.reconnect_count = 0
        self.last_error = None


# =========================
# 全局状态管理类
# =========================
class GlobalState:
    """管理程序全局状态"""
    def __init__(self):
        self.config = DEFAULT_CONFIG
        self.monitoring_enabled = True
        self.program_state = ProgramState.RUNNING
        self.last_trigger_time = 0.0
        self.triggered_event_ids: Set[str] = set()
        self.obs_process: Optional[subprocess.Popen] = None
        self.logger: Optional[logging.Logger] = None
        self.toast = ToastNotifier()
        self.tray_icon: Optional[pystray.Icon] = None
        self.tray_icon_running = False  # 添加标志位跟踪托盘状态
        self.lock = threading.Lock()  # 添加线程锁
        self.jma_status = ConnectionStatus()  # JMA连接状态
        self.cea_status = ConnectionStatus()  # CEA连接状态
        
    def is_in_cooldown(self) -> bool:
        """检查是否处于冷却时间内"""
        return time.time() - self.last_trigger_time < self.config.cooldown
    
    def update_trigger_time(self):
        """更新最后触发时间"""
        self.last_trigger_time = time.time()
    
    def add_triggered_event(self, event_id: str):
        """添加已触发的事件ID"""
        self.triggered_event_ids.add(event_id)
    
    def is_event_triggered(self, event_id: str) -> bool:
        """检查事件是否已触发过"""
        return event_id in self.triggered_event_ids
    
    def set_obs_process(self, process: subprocess.Popen):
        """设置OBS进程"""
        self.obs_process = process
        
    def cleanup(self):
        """清理资源"""
        self.stop_recording()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
                self.tray_icon = None
            except Exception as e:
                self.logger.error(f"停止托盘图标时出错: {e}")
        self.tray_icon_running = False  # 重置标志位

# 全局状态实例
state = GlobalState()


# =========================
# 工具函数
# =========================
def setup_logging() -> logging.Logger:
    """设置日志系统"""
    # 确保日志目录存在
    os.makedirs(state.config.log_dir, exist_ok=True)
    log_file = os.path.join(state.config.log_dir, "quake_monitor.log")
    
    # 创建日志记录器
    logger = logging.getLogger("quake_monitor")
    logger.setLevel(logging.INFO)
    
    # 清除可能存在的旧处理器
    logger.handlers.clear()
    
    # 文件处理器（带轮转）
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=state.config.max_log_size, 
        backupCount=state.config.log_backup_count, 
        encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    
    # 添加处理器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


def validate_config(config: AppConfig) -> bool:
    """验证配置是否有效"""
    errors = []
    
    # 检查必要文件路径
    if not os.path.exists(config.obs_path):
        errors.append(f"OBS路径不存在: {config.obs_path}")
    
    if not os.path.exists(config.alert_wav):
        errors.append(f"警报音文件不存在: {config.alert_wav}")
    
    # 检查配置值有效性
    if config.trigger_jma_intensity not in JMA_INTENSITY_MAP:
        errors.append(f"JMA阈值设置无效: {config.trigger_jma_intensity}")
    
    if config.trigger_cea_intensity <= 0:
        errors.append(f"CEA阈值必须大于0: {config.trigger_cea_intensity}")
    
    # 记录所有错误
    if errors:
        for error in errors:
            state.logger.error(error)
        return False
    
    return True


def ensure_directory_exists(path: str) -> bool:
    """确保目录存在，如果不存在则创建"""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError as e:
        state.logger.error(f"创建目录失败 {path}: {e}")
        return False


def cleanup_old_logs():
    """清理旧的日志文件"""
    try:
        # 计算截止日期
        cutoff_time = time.time() - (state.config.log_retention_days * 86400)  # 86400秒=1天
        
        # 获取所有日志文件
        log_pattern = os.path.join(state.config.log_dir, "quake_monitor.log*")
        log_files = glob.glob(log_pattern)
        
        # 删除过期的日志文件
        for log_file in log_files:
            # 检查文件修改时间
            if os.path.isfile(log_file) and os.path.getmtime(log_file) < cutoff_time:
                os.remove(log_file)
                state.logger.info(f"已删除旧日志文件: {os.path.basename(log_file)}")
                
    except Exception as e:
        state.logger.error(f"清理日志文件时出错: {e}")


def log_cleanup_loop():
    """日志清理循环"""
    while state.program_state != ProgramState.STOPPING:
        try:
            # 等待清理间隔时间
            time.sleep(state.config.log_cleanup_interval)
            
            # 执行日志清理
            if state.program_state != ProgramState.STOPPING:
                cleanup_old_logs()
                
        except Exception as e:
            state.logger.error(f"日志清理循环出错: {e}")
            # 出错后等待一段时间再继续
            time.sleep(3600)  # 1小时


def check_network_connection():
    """检查网络连接状态"""
    try:
        # 尝试连接一个可靠的网站
        import urllib.request
        urllib.request.urlopen("https://www.google.com", timeout=5)
        return True
    except Exception:
        try:
            # 备用检查方法
            import socket
            socket.create_connection(("8.8.8.8", 53), timeout=5)
            return True
        except Exception:
            return False


def status_report_loop():
    """定期报告连接状态"""
    while state.program_state != ProgramState.STOPPING:
        time.sleep(300)  # 每5分钟报告一次
        
        jma_status = state.jma_status
        cea_status = state.cea_status
        
        jma_uptime = time.time() - jma_status.last_message_time if jma_status.connected else 0
        cea_uptime = time.time() - cea_status.last_message_time if cea_status.connected else 0
        
        status_msg = (
            f"状态报告: JMA-{'连接中' if jma_status.connected else '断开'}({jma_status.reconnect_count}次重连), "
            f"CEA-{'连接中' if cea_status.connected else '断开'}({cea_status.reconnect_count}次重连)"
        )
        
        if jma_status.last_error:
            status_msg += f", JMA最后错误: {jma_status.last_error}"
        if cea_status.last_error:
            status_msg += f", CEA最后错误: {cea_status.last_error}"
            
        state.logger.info(status_msg)


# =========================
# 核心功能函数
# =========================
def play_alarm():
    """播放警报音效"""
    try:
        if os.path.exists(state.config.alert_wav):
            # 创建一个线程来播放警报音，避免阻塞主程序
            def play_alarm_thread():
                for _ in range(state.config.alarm_repeat_count):
                    winsound.PlaySound(state.config.alert_wav, winsound.SND_FILENAME)
                    time.sleep(0.1)  # 添加一点间隔
            
            # 启动线程播放警报
            alarm_thread = threading.Thread(target=play_alarm_thread)
            alarm_thread.daemon = True
            alarm_thread.start()
        else:
            state.logger.error(f"警报音文件不存在: {state.config.alert_wav}")
            # 使用系统默认声音作为后备
            for _ in range(state.config.alarm_repeat_count):
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
                time.sleep(1)  # 系统声音通常较短
    except Exception as e:
        state.logger.error(f"警报音播放失败: {e}")


def start_recording():
    """启动OBS录制"""
    # 先停止当前录制（如果存在）
    stop_recording()
    
    state.logger.info("启动 OBS 录制…")
    try:
        if os.path.exists(state.config.obs_path):
            # 使用subprocess.Popen启动OBS并记录进程对象
            process = subprocess.Popen(
                [
                    state.config.obs_path, 
                    "--startrecording", 
                    "--minimize-to-tray", 
                    "--recording-path", 
                    state.config.record_path
                ], 
                cwd=state.config.obs_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            state.set_obs_process(process)
        else:
            state.logger.error(f"OBS路径不存在: {state.config.obs_path}")
            return False
    except Exception as e:
        state.logger.error(f"启动 OBS 失败: {e}")
        return False
    
    # 设置定时停止录制
    if state.config.record_duration and state.config.record_duration > 0:
        timer = threading.Timer(state.config.record_duration, stop_recording)
        timer.daemon = True
        timer.start()
    
    return True


def stop_recording():
    """停止OBS录制"""
    state.logger.info("停止 OBS 录制…")
    try:
        # 只停止由我们启动的OBS进程
        if state.obs_process and state.obs_process.poll() is None:
            # 使用OBS命令行参数停止录制
            subprocess.Popen(
                [state.config.obs_path, "--stoprecording"], 
                cwd=state.config.obs_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # 等待一段时间后关闭OBS
            time.sleep(5)  # 给OBS一些时间处理停止命令
            if state.obs_process and state.obs_process.poll() is None:
                state.obs_process.terminate()  # 温和地终止进程
                state.obs_process.wait(timeout=10)  # 等待进程结束
    except Exception as e:
        state.logger.error(f"停止录制失败: {e}")
    finally:
        state.set_obs_process(None)


def unified_trigger(source: AlertSource, lines: List[str], event_id: Optional[str] = None):
    """
    统一触发处理函数，处理地震预警事件
    
    Args:
        source: 预警来源
        lines: 预警信息内容列表
        event_id: 事件ID，用于去重（可选）
    """
    # 使用线程锁确保状态检查的原子性
    with state.lock:
        # 检查是否启用监控
        if not state.monitoring_enabled:
            state.logger.info("监控已暂停，忽略触发")
            return
            
        # 去重检查
        if event_id and state.is_event_triggered(event_id):
            state.logger.info(f"事件 {event_id} 已触发过，忽略")
            return
            
        # 冷却检查
        if state.is_in_cooldown():
            state.logger.info("冷却时间内，忽略触发")
            return
            
        # 更新触发时间和事件ID
        state.update_trigger_time()
        if event_id:
            state.add_triggered_event(event_id)

    # 生成内容并记录
    source_name = {
        AlertSource.JMA: "日本气象厅 (JMA)",
        AlertSource.CEA: "中国地震预警网 (CEA)",
        AlertSource.TEST: "人工测试"
    }[source]
    
    content = "\n".join(lines)
    
    # 将耗时操作放在单独线程中执行，避免阻塞WebSocket消息处理
    def trigger_operations():
        state.logger.info(f"触发来源: {source_name}\n{content}")
        
        # 显示通知
        state.toast.show_toast(
            state.config.toast_app_name, 
            f"触发：{source_name}", 
            duration=3, 
            threaded=True
        )
        
        # 播放警报
        play_alarm()
        
        # 启动录制
        start_recording()
    
    # 启动线程执行触发操作
    threading.Thread(target=trigger_operations, daemon=True).start()


# =========================
# WebSocket 处理函数
# =========================
def on_message_jma(ws, message):
    """处理JMA WebSocket消息"""
    if not state.monitoring_enabled:
        return
        
    try:
        # 检查是否是心跳相关的消息
        if message.strip().lower() in ["ping", "pong", "heartbeat"]:
            if state.config.debug_mode:
                state.logger.debug(f"JMA 心跳: {message}")
            # 对于ping消息，回复pong
            if message.strip().lower() == "ping":
                try:
                    ws.send("pong")
                    if state.config.debug_mode:
                        state.logger.debug("JMA 回复pong")
                except Exception as e:
                    state.logger.error(f"JMA 回复pong失败: {e}")
            return
            
        data = json.loads(message)
        
        # 忽略取消、训练和假设报文
        if data.get("isCancel", False) or data.get("isTraining", False) or data.get("isAssumption", False):
            state.logger.info("JMA 非正式/取消报文，忽略")
            return
            
        # 获取最大震度
        max_intensity = str(data.get("MaxIntensity", "")).strip()
        
        # 使用映射表进行比较
        current_intensity_value = JMA_INTENSITY_MAP.get(max_intensity, -1)
        threshold_value = JMA_INTENSITY_MAP.get(state.config.trigger_jma_intensity, -1)
        
        # 检查是否达到阈值
        if current_intensity_value >= threshold_value and current_intensity_value != -1:
            place = data.get("Hypocenter", "")
            mag = data.get("Magunitude", "")  # 注意：数据源字段就是这个拼写
            depth = data.get("Depth", "")
            ann = data.get("AnnouncedTime", "")
            eid = str(data.get("EventID", ""))
            
            lines = [
                f"地点: {place}",
                f"最大震度: {max_intensity}",
                f"震级: M{mag}   深度: {depth} km",
                f"发布时间: {ann}",
                f"事件ID: {eid}"
            ]
            
            unified_trigger(AlertSource.JMA, lines, eid)
        else:
            state.logger.info(f"JMA 更新：最大震度 {max_intensity} (阈值: {state.config.trigger_jma_intensity})")
            
    except json.JSONDecodeError as e:
        # 如果不是JSON，可能是简单的心跳消息，已经处理过了
        if message.strip().lower() not in ["ping", "pong", "heartbeat"]:
            state.logger.error(f"JMA JSON解析错误: {e} - 消息内容: {message}")
    except Exception as e:
        state.logger.error(f"JMA 解析错误: {e}")


def on_message_cea(ws, message):
    """处理CEA WebSocket消息"""
    if not state.monitoring_enabled:
        return
        
    try:
        data = json.loads(message)
        d = data.get("Data", {})
        
        if not d:
            return
            
        place = d.get("placeName", "")
        mag = d.get("magnitude", "")
        depth = d.get("depth", "")
        shock = d.get("shockTime", "")
        eid = str(d.get("eventId", ""))
        epi = d.get("epiIntensity", 0)
        
        try:
            epi_val = float(epi)
        except (ValueError, TypeError):
            epi_val = 0.0
            
        # 检查是否达到阈值
        if epi_val >= state.config.trigger_cea_intensity:
            lines = [
                f"地点: {place}",
                f"预估烈度: {epi_val}",
                f"震级: M{mag}   深度: {depth} km",
                f"发震时刻: {shock}",
                f"事件ID: {eid}"
            ]
            
            unified_trigger(AlertSource.CEA, lines, eid)
        else:
            state.logger.info(f"CEA 更新：烈度 {epi_val} (< {state.config.trigger_cea_intensity})")
            
    except json.JSONDecodeError as e:
        state.logger.error(f"CEA JSON解析错误: {e}")
    except Exception as e:
        state.logger.error(f"CEA 解析错误: {e}")


def ws_loop(name: str, url: str, handler: Callable):
    """WebSocket连接循环（带自动重连和退避策略）"""
    reconnect_delay = state.config.ws_reconnect_delay
    reconnect_attempts = 0
    max_reconnect_delay = state.config.ws_max_reconnect_delay
    reconnect_backoff = state.config.ws_reconnect_backoff
    
    # 根据名称获取对应的状态对象
    status_obj = state.jma_status if name == "JMA" else state.cea_status
    
    while state.program_state != ProgramState.STOPPING:
        # 检查网络连接
        if not check_network_connection():
            state.logger.warning(f"网络连接不可用，等待 {reconnect_delay} 秒后重试")
            time.sleep(reconnect_delay)
            continue
            
        try:
            state.logger.info(f"{name} 正在连接... (尝试次数: {reconnect_attempts+1}, 延迟: {reconnect_delay}秒)")
            
            # 创建WebSocket连接
            ws = websocket.WebSocketApp(
                url,
                on_message=handler,
                on_error=lambda _ws, err: (
                    state.logger.error(f"{name} 错误: {err}"),
                    setattr(status_obj, "last_error", str(err))
                ),
                on_close=lambda _ws, code, msg: state.logger.info(f"{name} 关闭: {code} {msg}"),
                on_ping=lambda _ws, data: state.logger.debug(f"{name} 收到ping: {data}") if state.config.debug_mode else None,
                on_pong=lambda _ws, data: state.logger.debug(f"{name} 收到pong: {data}") if state.config.debug_mode else None
            )
            
            # 设置连接打开时的回调
            def on_open(ws):
                state.logger.info(f"已连接 {name}")
                status_obj.connected = True
                status_obj.last_message_time = time.time()
                status_obj.reconnect_count = 0  # 重置重连计数
                # 对于Wolfx API，连接后立即发送一个ping以响应服务端的心跳
                if "wolfx" in url.lower() or "jma" in name.lower():
                    try:
                        ws.send("ping")
                        if state.config.debug_mode:
                            state.logger.debug(f"{name} 发送初始ping")
                    except Exception as e:
                        state.logger.error(f"{name} 发送初始ping失败: {e}")
            
            ws.on_open = on_open
            
            # 根据API类型设置不同的ping参数
            ping_interval = state.config.wolfx_ping_interval if "wolfx" in url.lower() or "jma" in name.lower() else state.config.ws_ping_interval
            ping_timeout = state.config.wolfx_ping_timeout if "wolfx" in url.lower() or "jma" in name.lower() else state.config.ws_ping_timeout
            
            ws.run_forever(
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                ping_payload="ping"  # 明确指定ping载荷
            )
            
        except Exception as e:
            state.logger.error(f"{name} run_forever 异常: {e}")
            status_obj.last_error = str(e)
            
        # 检查是否需要停止
        if state.program_state == ProgramState.STOPPING:
            break
            
        # 更新连接状态
        status_obj.connected = False
        status_obj.reconnect_count += 1
        reconnect_attempts += 1
        
        # 使用退避策略增加重连延迟，但不超最大延迟
        if reconnect_delay < max_reconnect_delay:
            new_delay = reconnect_delay * reconnect_backoff
            reconnect_delay = min(new_delay, max_reconnect_delay)
        
        state.logger.info(f"{name} 将在 {reconnect_delay} 秒后尝试重连 (尝试次数: {reconnect_attempts})")
        time.sleep(reconnect_delay)


# =========================
# 托盘图标功能
# =========================
def create_icon_image() -> Image.Image:
    """创建托盘图标"""
    img = Image.new("RGBA", (64, 64), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=(220, 20, 60, 230))
    return img


def menu_toggle(icon, item):
    """切换监听状态"""
    state.monitoring_enabled = not state.monitoring_enabled
    state_text = "恢复监听" if state.monitoring_enabled else "暂停监听"
    state.logger.info(state_text)
    state.toast.show_toast(state.config.toast_app_name, state_text, duration=2, threaded=True)


def menu_open_log(icon, item):
    """打开日志文件"""
    log_file = os.path.join(state.config.log_dir, "quake_monitor.log")
    
    try:
        if os.path.exists(log_file):
            os.startfile(log_file)
        else:
            state.logger.error("日志文件不存在")
    except Exception as e:
        state.logger.error(f"打开日志失败: {e}")
        try:
            import webbrowser
            webbrowser.open('file://' + log_file)
        except Exception:
            pass


def menu_cleanup_logs(icon, item):
    """手动清理日志文件"""
    state.logger.info("手动清理日志文件")
    cleanup_old_logs()
    state.toast.show_toast(state.config.toast_app_name, "已清理旧日志文件", duration=2, threaded=True)


def menu_quit(icon, item):
    """退出程序"""
    state.logger.info("退出程序")
    state.program_state = ProgramState.STOPPING
    state.cleanup()
    icon.stop()


def setup_tray():
    """设置系统托盘图标"""
    # 如果托盘图标已经在运行，则不创建新实例
    if state.tray_icon_running:
        state.logger.info("托盘图标已在运行，跳过创建")
        return
        
    try:
        icon = pystray.Icon(
            "quake_monitor", 
            create_icon_image(), 
            "地震速报监听",
            menu=pystray.Menu(
                pystray.MenuItem("暂停/恢复监听", menu_toggle),
                pystray.MenuItem("打开日志", menu_open_log),
                pystray.MenuItem("清理日志", menu_cleanup_logs),
                pystray.MenuItem("退出", menu_quit)
            )
        )
        # 设置标志位
        state.tray_icon_running = True
        # 在单独线程中运行托盘图标
        def run_icon():
            try:
                icon.run()
            finally:
                # 确保在图标停止时更新状态
                state.tray_icon_running = False
        
        threading.Thread(target=run_icon, daemon=True).start()
        state.tray_icon = icon
        state.logger.info("托盘图标已创建")
    except Exception as e:
        state.logger.error(f"创建托盘图标失败: {e}")
        state.tray_icon_running = False

# =========================
# HTTP控制服务器
# =========================
class ControlHandler(http.server.BaseHTTPRequestHandler):
    """HTTP请求处理器"""
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/command":
            self.send_response(404)
            self.end_headers()
            return
            
        qs = parse_qs(parsed.query)
        cmd = (qs.get("cmd", [""])[0] or "").lower()

        if cmd == "pause":
            state.monitoring_enabled = False
            state.logger.info("已暂停监听")
            self._ok("paused")

        elif cmd == "resume":
            state.monitoring_enabled = True
            state.logger.info("已恢复监听")
            self._ok("resumed")

        elif cmd == "test":
            # 人工测试一次触发
            lines = [
                "地点: 测试地点",
                "强度: 测试",
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ]
            unified_trigger(AlertSource.TEST, lines, None)
            self._ok("triggered")

        elif cmd == "startrec":
            start_recording()
            self._ok("startrec")

        elif cmd == "stoprec":
            stop_recording()
            self._ok("stoprec")
            
        elif cmd == "status":
            status = "running" if state.monitoring_enabled else "paused"
            self._ok(status)
            
        elif cmd == "cleanuplogs":
            cleanup_old_logs()
            self._ok("logs cleaned")

        else:
            self._ok("unknown command")

    def log_message(self, format, *args):
        # 静音 HTTP server 的默认日志
        return

    def _ok(self, msg):
        """发送成功响应"""
        body = msg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_control_server():
    """启动HTTP控制服务器"""
    try:
        server = socketserver.TCPServer(("127.0.0.1", state.config.control_port), ControlHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state.logger.info(f"本地控制服务已启动：http://127.0.0.1:{state.config.control_port}/command?cmd=...")
    except Exception as e:
        state.logger.error(f"启动控制服务器失败: {e}")


# =========================
# 主程序
# =========================
def main():
    """主程序入口"""
    # 重定向 stderr 以过滤特定错误
    original_stderr = sys.stderr
    sys.stderr = FilteredStderr(original_stderr.buffer, 
                               encoding=original_stderr.encoding,
                               errors=original_stderr.errors,
                               line_buffering=original_stderr.line_buffering)
                               
    # 初始化日志
    state.logger = setup_logging()
    
    # 验证配置
    if not validate_config(state.config):
        state.logger.error("配置验证失败，程序退出")
        return
    
    # 确保日志目录存在
    if not ensure_directory_exists(state.config.log_dir):
        state.logger.error("无法创建日志目录，程序退出")
        return
    
    # 初始化组件
    setup_tray()
    start_control_server()
    
    # 启动日志清理线程
    log_cleanup_thread = threading.Thread(target=log_cleanup_loop, daemon=True)
    log_cleanup_thread.start()
    
    # 启动状态报告线程
    status_thread = threading.Thread(target=status_report_loop, daemon=True)
    status_thread.start()
    
    state.logger.info(
        f"程序已启动并最小化到托盘 "
        f"(JMA阈值: {state.config.trigger_jma_intensity}, "
        f"CEA阈值: {state.config.trigger_cea_intensity}, "
        f"日志保留天数: {state.config.log_retention_days})"
    )
    
    state.toast.show_toast(
        state.config.toast_app_name, 
        "程序已启动", 
        duration=2, 
        threaded=True
    )

    # 启动两个 WS 线程
    jma_thread = threading.Thread(
        target=ws_loop, 
        args=("JMA", state.config.ws_jma, on_message_jma), 
        daemon=True
    )
    
    cea_thread = threading.Thread(
        target=ws_loop, 
        args=("CEA", state.config.ws_cea, on_message_cea), 
        daemon=True
    )
    
    jma_thread.start()
    cea_thread.start()

    # 主线程循环
    try:
        while state.program_state != ProgramState.STOPPING:
            time.sleep(1)
            
    except KeyboardInterrupt:
        state.logger.info("收到中断信号，程序退出")
        
    finally:
        state.program_state = ProgramState.STOPPING
        state.cleanup()


if __name__ == "__main__":
    main()
