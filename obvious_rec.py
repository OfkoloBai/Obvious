"""
地震速报监听程序 - NSSM服务版本
监听日本气象厅(JMA)、中国地震预警网(CEA)和台湾气象署(CWA)的地震预警信息
当检测到达到设定阈值的地震时，自动触发警报和OBS录制
注意：此版本专为作为Windows服务运行而优化
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
import socket
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any, List, Callable, Set
from logging.handlers import RotatingFileHandler
from enum import Enum, auto
from pathlib import Path

# 第三方库
import websocket
import winsound
import requests  # 添加requests库用于HTTP请求

# =========================
# 枚举和常量定义
# =========================
class AlertSource(Enum):
    """预警来源枚举"""
    JMA = auto()  # 日本气象厅
    CEA = auto()  # 中国地震预警网
    CWA = auto()  # 台湾气象署
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
    # 触发和冷却配置
    cooldown: int
    trigger_jma_intensity: str
    trigger_cea_intensity: float
    trigger_cwa_intensity: str  # 修改为字符串类型
    
    # 警报和通知配置
    alert_wav: str
    toast_app_name: str
    
    # WebSocket和网络配置
    ws_jma: str
    ws_cea: str
    cwa_api_url: str  # 添加CWA API地址
    control_port: int
    
    # 日志和文件配置
    log_dir: str
    max_log_size: int = 5 * 1024 * 1024  # 5MB
    log_backup_count: int = 5
    log_retention_days: int = 30  # 日志保留天数
    
    # WebSocket增强配置
    ws_reconnect_delay: int = 5  # WebSocket重连延迟(秒)
    ws_ping_interval: int = 20   # 发送ping间隔(秒)
    ws_ping_timeout: int = 10    # 等待pong超时时间(秒)
    ws_skip_errors: bool = True  # 是否跳过非关键错误继续重连
    
    # 其他配置
    alarm_repeat_count: int = 3  # 警报音重复次数
    log_cleanup_interval: int = 86400  # 日志清理间隔(秒)，默认每天一次
    cwa_poll_interval: int = 3  # CWA API请求间隔(秒)
    
    # OBS WebSocket配置
    obs_host: str = "localhost"          # OBS WebSocket服务器地址
    obs_port: int = 4455                 # OBS WebSocket端口
    obs_password: str = "你的OBS WebSocket服务器密码"  # OBS WebSocket密码<---在此处修改为你自己的OBWebSocket服务器密码
    obs_record_duration: int = 600       # 录制持续时间(秒)，默认10分钟，单位秒<---|录制时间
    obs_scene_name: str = "你的OBS场景"     # OBS场景名称<---此处改为你自己的OBS场景名称

    # 服务重启配置
    service_name: str = "你为此程序注册的服务名"  # Windows服务名称<---如果你是使用的NSSM运行的此程序，则填写你注册的服务名称
    restart_delay: int = 5  # 重启服务前的延迟时间(秒)

    # 时间窗口配置
    time_window_minutes: int = 10  # 时间窗口（分钟），默认10分钟<---|忽略过早地震的时间窗口

    def to_dict(self) -> Dict[str, Any]:
        """将配置转换为字典"""
        return asdict(self)


# 默认配置 - 用户需要根据自己的环境修改这些配置
DEFAULT_CONFIG = AppConfig(
    cooldown=600,  # 冷却时间(秒)，建议等于或略小于录制时长
    alert_wav=r"你的文件路径",   # <---此处修改为你的警报音文件路径
    toast_app_name="地震速报监听",
    trigger_jma_intensity="5弱",  # JMA触发阈值<---|日本气象厅触发阈值(震度)
    trigger_cea_intensity=7.0,  # CEA触发阈值(烈度)<---|中国地震台网触发阈值(烈度)
    trigger_cwa_intensity="5弱",  # CWA触发阈值(震度)<---|台湾气象署触发阈值(震度)
    ws_jma="wss://ws-api.wolfx.jp/jma_eew",  # JMA WebSocket地址
    ws_cea="wss://ws.fanstudio.tech/cea",  # CEA WebSocket地址
    cwa_api_url="https://api.wolfx.jp/cwa_eew.json",  # CWA API地址
    control_port=8787,  # HTTP控制端口
    log_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),  # 日志目录
    time_window_minutes= 10 # 时间窗口（分钟）,在此处修改为你想要的值,但建议大于录制时长
)


# =========================
# OBS WebSocket控制器
# =========================
class OBSController:
    """OBS WebSocket控制器"""
    def __init__(self, config: AppConfig):
        self.config = config
        self.ws = None
        self.connected = False
        self.recording = False
        self.record_start_time = 0
        self.lock = threading.Lock()
        self.connection_attempts = 0
        self.max_connection_attempts = 3
        self.auto_stop_timer = None
        
    def connect(self) -> bool:
        """连接到OBS WebSocket服务器"""
        try:
            from obswebsocket import obsws, requests
            self.ws = obsws(
                self.config.obs_host, 
                self.config.obs_port, 
                self.config.obs_password
            )
            self.ws.connect()
            self.connected = True
            self.connection_attempts = 0
            state.logger.info("成功连接到OBS WebSocket服务器")
            return True
        except Exception as e:
            self.connection_attempts += 1
            state.logger.error(f"连接OBS失败 ({self.connection_attempts}/{self.max_connection_attempts}): {e}")
            self.connected = False
            return False
            
    def ensure_connection(self) -> bool:
        """确保OBS连接正常，如果未连接则尝试连接"""
        if self.connected:
            return True
            
        if self.connection_attempts >= self.max_connection_attempts:
            # 等待一段时间后再尝试重连
            time.sleep(30)
            self.connection_attempts = 0
            
        return self.connect()
            
    def disconnect(self):
        """断开OBS连接"""
        if self.ws and self.connected:
            try:
                self.ws.disconnect()
            except:
                pass
            finally:
                self.connected = False
            
    def start_recording(self) -> bool:
        """开始录制"""
        if not self.ensure_connection():
            return False
            
        try:
            from obswebsocket import requests
            
            # 确保使用正确的场景
            if self.config.obs_scene_name:
                try:
                    self.ws.call(requests.SetCurrentProgramScene(
                        sceneName=self.config.obs_scene_name
                    ))
                except Exception as e:
                    state.logger.warning(f"设置OBS场景失败: {e}")
                    # 场景设置失败不影响录制
                
            # 开始录制
            response = self.ws.call(requests.StartRecord())
            if response.status:
                with self.lock:
                    self.recording = True
                    self.record_start_time = time.time()
                
                # 设置自动停止计时器
                self._setup_auto_stop_timer()
                
                state.logger.info("OBS录制已开始")
                return True
            else:
                state.logger.error("OBS录制启动失败")
                # 录制失败可能是连接问题，重置连接状态
                self.connected = False
                return False
                
        except Exception as e:
            state.logger.error(f"启动OBS录制失败: {e}")
            self.connected = False
            return False
            
    def _setup_auto_stop_timer(self):
        """设置自动停止录制计时器"""
        # 取消现有的计时器
        if self.auto_stop_timer and self.auto_stop_timer.is_alive():
            self.auto_stop_timer.cancel()
        
        # 创建新的计时器
        self.auto_stop_timer = threading.Timer(
            self.config.obs_record_duration,
            self._on_recording_timeout
        )
        self.auto_stop_timer.daemon = True
        self.auto_stop_timer.start()
        
    def _on_recording_timeout(self):
        """录制超时处理"""
        if self.is_recording():
            state.logger.info("录制时间到达，自动停止录制")
            self.stop_recording()
            
            # 延迟后重启服务
            state.logger.info(f"{self.config.restart_delay}秒后重启服务...")
            time.sleep(self.config.restart_delay)
            self._restart_service()
            
    def _restart_service(self):
        """重启Windows服务"""
        try:
            service_name = self.config.service_name
            state.logger.info(f"正在重启服务: {service_name}")
            
            # 停止服务
            subprocess.run(["net", "stop", service_name], check=True, timeout=30)
            state.logger.info("服务已停止")
            
            # 启动服务
            subprocess.run(["net", "start", service_name], check=True, timeout=30)
            state.logger.info("服务已启动")
            
        except subprocess.CalledProcessError as e:
            state.logger.error(f"服务重启失败: {e}")
        except subprocess.TimeoutExpired:
            state.logger.error("服务重启超时")
        except Exception as e:
            state.logger.error(f"服务重启过程中发生错误: {e}")
            
    def stop_recording(self) -> bool:
        """停止录制"""
        if not self.connected:
            return False
            
        try:
            from obswebsocket import requests
            response = self.ws.call(requests.StopRecord())
            if response.status:
                with self.lock:
                    self.recording = False
                state.logger.info("OBS录制已停止")
                return True
            else:
                state.logger.error("OBS录制停止失败")
                return False
                
        except Exception as e:
            state.logger.error(f"停止OBS录制失败: {e}")
            self.connected = False
            return False
            
    def is_recording(self) -> bool:
        """检查是否正在录制"""
        with self.lock:
            return self.recording
            
    def check_and_stop_recording(self):
        """检查录制时间并停止超时的录制"""
        with self.lock:
            if (self.recording and 
                time.time() - self.record_start_time > self.config.obs_record_duration):
                self.stop_recording()
                
    def recording_loop(self):
        """录制监控循环"""
        while state.program_state != ProgramState.STOPPING:
            try:
                self.check_and_stop_recording()
                time.sleep(5)  # 每5秒检查一次
            except Exception as e:
                state.logger.error(f"OBS监控循环出错: {e}")
                time.sleep(30)  # 出错后等待30秒再继续


# =========================
# 时间处理工具函数
# =========================
def parse_datetime_with_timezone(datetime_str: str, timezone_offset: int) -> Optional[datetime]:
    """
    解析日期时间字符串并应用时区偏移
    
    Args:
        datetime_str: 日期时间字符串
        timezone_offset: 时区偏移（小时），例如UTC+8为8，UTC+9为9
        
    Returns:
        datetime对象或None（如果解析失败）
    """
    try:
        # 尝试解析不同的日期时间格式
        formats = [
            "%Y/%m/%d %H:%M:%S",  # JMA格式
            "%Y-%m-%d %H:%M:%S",  # CEA格式
            "%Y年%m月%d日 %H:%M:%S",  # 中文格式
            "%Y-%m-%dT%H:%M:%S",  # ISO格式（带T）
            "%Y-%m-%dT%H:%M:%S.%fZ",  # ISO格式（带毫秒和Z）
            "%Y-%m-%dT%H:%M:%S.%f",  # ISO格式（带毫秒）
            "%Y-%m-%d %H:%M:%S.%f"  # 带毫秒的标准格式
        ]
        
        dt = None
        for fmt in formats:
            try:
                dt = datetime.strptime(datetime_str, fmt)
                break
            except ValueError:
                continue
                
        if dt is None:
            state.logger.warning(f"无法解析日期时间字符串: {datetime_str}")
            return None
            
        # 应用时区偏移
        dt = dt.replace(tzinfo=timezone(timedelta(hours=timezone_offset)))
        return dt
        
    except Exception as e:
        state.logger.error(f"解析日期时间失败 '{datetime_str}': {e}")
        return None


def is_within_time_window(event_time_str: str, timezone_offset: int, window_minutes: int = None) -> bool:
    """
    检查事件时间是否在当前时间的指定时间窗口内
    
    Args:
        event_time_str: 事件时间字符串
        timezone_offset: 事件时间的时区偏移（小时）
        window_minutes: 时间窗口（分钟），如果为None则使用配置中的值
        
    Returns:
        bool: 是否在时间窗口内
    """
    # 使用配置的时间窗口如果未指定
    if window_minutes is None:
        window_minutes = state.config.time_window_minutes
        
    # 解析事件时间
    event_dt = parse_datetime_with_timezone(event_time_str, timezone_offset)
    if event_dt is None:
        state.logger.warning(f"无法解析事件时间: {event_time_str}")
        return False
        
    # 获取当前时间（UTC+8）
    current_dt = datetime.now(timezone(timedelta(hours=8)))
    
    # 将事件时间转换为UTC+8时区
    event_dt_utc8 = event_dt.astimezone(timezone(timedelta(hours=8)))
    
    # 计算时间差
    time_diff = current_dt - event_dt_utc8
    time_diff_seconds = abs(time_diff.total_seconds())
    
    # 记录时间差信息
    state.logger.info(f"事件时间: {event_dt_utc8}, 当前时间: {current_dt}, 时间差: {time_diff_seconds:.1f}秒, 窗口: {window_minutes}分钟")
    
    # 检查是否在时间窗口内
    return time_diff_seconds <= window_minutes * 60


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
        self.logger: Optional[logging.Logger] = None
        self.lock = threading.Lock()  # 添加线程锁
        self.ws_connections = {}  # 跟踪WebSocket连接状态
        self.is_service = False  # 标记是否作为服务运行
        self.obs_controller: Optional[OBSController] = None  # OBS控制器
        self.last_cwa_request_time = 0.0  # 上次CWA请求时间
        
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
    
    def init_obs_controller(self):
        """初始化OBS控制器"""
        self.obs_controller = OBSController(self.config)
        # 尝试连接OBS
        if self.obs_controller.connect():
            self.logger.info("OBS控制器初始化成功")
        else:
            self.logger.warning("OBS控制器初始化失败，录制功能可能不可用")
    
    def cleanup(self):
        """清理资源"""
        if self.obs_controller:
            # 取消自动停止计时器
            if self.obs_controller.auto_stop_timer and self.obs_controller.auto_stop_timer.is_alive():
                self.obs_controller.auto_stop_timer.cancel()
            self.obs_controller.disconnect()


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
    
    # 只有在非服务模式下才添加控制台处理器
    if not state.is_service:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        ))
        logger.addHandler(console_handler)
    
    # 添加文件处理器
    logger.addHandler(file_handler)
    
    return logger


def validate_config(config: AppConfig) -> bool:
    """验证配置是否有效"""
    errors = []
    
    # 检查必要文件路径
    if not os.path.exists(config.alert_wav):
        errors.append(f"警报音文件不存在: {config.alert_wav}")
    
    # 检查配置值有效性
    if config.trigger_jma_intensity not in JMA_INTENSITY_MAP:
        errors.append(f"JMA阈值设置无效: {config.trigger_jma_intensity}")
    
    if config.trigger_cea_intensity <= 0:
        errors.append(f"CEA阈值必须大于0: {config.trigger_cea_intensity}")
    
    # 检查CWA阈值有效性
    if config.trigger_cwa_intensity not in JMA_INTENSITY_MAP:
        errors.append(f"CWA阈值设置无效: {config.trigger_cwa_intensity}")
    
    # 检查时间窗口有效性
    if config.time_window_minutes <= 0:
        errors.append(f"时间窗口必须大于0: {config.time_window_minutes}")
    
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
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        return False


def is_running_as_service():
    """检测是否作为Windows服务运行"""
    try:
        # 检查是否有控制台窗口
        import ctypes
        kernel32 = ctypes.windll.kernel32
        return kernel32.GetConsoleWindow() == 0
    except:
        return False


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
        AlertSource.CWA: "台湾气象局 (CWA)",
        AlertSource.TEST: "人工测试"
    }[source]
    
    content = "\n".join(lines)
    
    # 将耗时操作放在单独线程中执行，避免阻塞WebSocket消息处理
    def trigger_operations():
        state.logger.info(f"触发来源: {source_name}\n{content}")
        
        # 播放警报
        play_alarm()
        
        # 启动OBS录制（如果配置了OBS）
        if state.obs_controller:
            # 在单独的线程中启动录制，避免阻塞
            def start_obs_recording():
                # 如果正在录制，先停止当前录制
                if state.obs_controller.is_recording():
                    state.logger.info("检测到已有录制正在进行，先停止当前录制")
                    state.obs_controller.stop_recording()
                    time.sleep(1)  # 等待1秒确保录制停止
                
                # 开始新的录制
                if state.obs_controller.start_recording():
                    state.logger.info("地震触发OBS录制已启动")
                else:
                    state.logger.warning("无法启动OBS录制")
            
            obs_thread = threading.Thread(target=start_obs_recording)
            obs_thread.daemon = True
            obs_thread.start()
    
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
            
            # 检查时间是否在配置的时间窗口内 (JMA是UTC+9)
            if not is_within_time_window(ann, 9):
                state.logger.info(f"JMA 事件 {eid} 发布时间 {ann} 不在时间窗口内，忽略")
                return
            
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
        state.logger.error(f"JMA JSON解析错误: {e}")
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
            # 检查时间是否在配置的时间窗口内 (CEA是UTC+8)
            if not is_within_time_window(shock, 8):
                state.logger.info(f"CEA 事件 {eid} 发震时刻 {shock} 不在时间窗口内，忽略")
                return
            
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


def cwa_monitor_loop():
    """CWA API监控循环"""
    while state.program_state != ProgramState.STOPPING:
        try:
            # 检查是否启用监控
            if not state.monitoring_enabled:
                time.sleep(state.config.cwa_poll_interval)
                continue
                
            # 检查网络连接
            if not check_network_connection():
                time.sleep(state.config.cwa_poll_interval)
                continue
                
            # 检查是否达到请求间隔
            current_time = time.time()
            if current_time - state.last_cwa_request_time < state.config.cwa_poll_interval:
                time.sleep(0.1)
                continue
                
            # 发送HTTP GET请求
            response = requests.get(state.config.cwa_api_url, timeout=10)
            state.last_cwa_request_time = current_time
            
            if response.status_code == 200:
                data = response.json()
                
                # 忽略取消报文
                if data.get("isCancel", False):
                    continue
                    
                # 获取最大震度（字符串）
                max_intensity = str(data.get("MaxIntensity", "")).strip()
                
                # 使用映射表进行比较
                current_intensity_value = JMA_INTENSITY_MAP.get(max_intensity, -1)
                threshold_value = JMA_INTENSITY_MAP.get(state.config.trigger_cwa_intensity, -1)
                
                # 检查是否达到阈值
                if current_intensity_value >= threshold_value and current_intensity_value != -1:
                    # 获取其他信息
                    place = data.get("HypoCenter", "")
                    mag = data.get("Magunitude", "")
                    depth = data.get("Depth", "")
                    origin_time = data.get("OriginTime", "")
                    report_time = data.get("ReportTime", "")
                    eid = str(data.get("ID", ""))
                    report_num = data.get("ReportNum", "")
                    
                    # 检查报告时间是否在配置的时间窗口内 (CWA是UTC+8)
                    if not is_within_time_window(report_time, 8):
                        continue
                    
                    lines = [
                        f"地点: {place}",
                        f"最大震度: {max_intensity}",
                        f"震级: M{mag}   深度: {depth} km",
                        f"发震时间: {origin_time}",
                        f"报告时间: {report_time}",
                        f"报告编号: {report_num}",
                        f"事件ID: {eid}"
                    ]
                    
                    unified_trigger(AlertSource.CWA, lines, eid)
                    
        except requests.exceptions.RequestException:
            # 网络请求异常，静默处理
            pass
        except json.JSONDecodeError:
            # JSON解析异常，静默处理
            pass
        except Exception:
            # 其他异常，静默处理
            pass
            
        # 短暂休眠
        time.sleep(0.1)


def ws_loop(name: str, url: str, handler: Callable):
    """WebSocket连接循环（带自动重连）"""
    reconnect_delay = state.config.ws_reconnect_delay
    max_reconnect_delay = 300  # 最大重连延迟5分钟
    
    while state.program_state != ProgramState.STOPPING:
        try:
            state.logger.info(f"正在连接 {name}...")
            
            # 检查网络连接
            if not check_network_connection():
                state.logger.warning(f"网络连接不可用，等待 {reconnect_delay} 秒后重试...")
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                continue
            
            # 记录连接状态
            state.ws_connections[name] = {
                'status': 'connecting',
                'last_activity': time.time(),
                'url': url
            }
            
            ws = websocket.WebSocketApp(
                url,
                on_message=handler,
                on_error=lambda _ws, err: state.logger.error(f"{name} 错误: {err}"),
                on_close=lambda _ws, code, msg: state.logger.info(f"{name} 关闭: {code} {msg}")
            )
            ws.on_open = lambda _ws: state.logger.info(f"已连接 {name}")
            
            # 更新连接状态
            state.ws_connections[name]['status'] = 'connected'
            
            # 重置重连延迟
            reconnect_delay = state.config.ws_reconnect_delay
            
            # 运行WebSocket，设置合理的心跳参数
            ws.run_forever(
                ping_interval=state.config.ws_ping_interval,
                ping_timeout=state.config.ws_ping_timeout,
                ping_payload="",  # 使用空字符串作为ping载荷
                skip_utf8_validation=state.config.ws_skip_errors
            )
            
        except websocket.WebSocketConnectionClosedException:
            state.logger.warning(f"{name} 连接已关闭，尝试重连...")
        except websocket.WebSocketTimeoutException:
            state.logger.warning(f"{name} 连接超时，尝试重连...")
        except Exception as e:
            state.logger.error(f"{name} 连接异常: {e}")
            
        # 检查是否需要停止
        if state.program_state == ProgramState.STOPPING:
            break
            
        # 更新连接状态
        state.ws_connections[name]['status'] = 'disconnected'
            
        # 使用指数退避策略进行重连
        state.logger.info(f"{reconnect_delay}秒后尝试重连{name}...")
        time.sleep(reconnect_delay)
        
        # 增加下次重连的等待时间，但不超过最大值
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)


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
            
        elif cmd == "status":
            status = "running" if state.monitoring_enabled else "paused"
            self._ok(status)
            
        elif cmd == "cleanuplogs":
            cleanup_old_logs()
            self._ok("logs cleaned")
            
        elif cmd == "wsstatus":
            # 返回WebSocket连接状态
            self._ok(json.dumps(state.ws_connections))
            
        elif cmd == "obsstatus":
            # 返回OBS状态
            if state.obs_controller:
                status = {
                    "connected": state.obs_controller.connected,
                    "recording": state.obs_controller.is_recording(),
                    "record_duration": state.config.obs_record_duration
                }
                self._ok(json.dumps(status))
            else:
                self._ok("OBS控制器未初始化")
                
        elif cmd == "obsstart":
            # 手动启动OBS录制
            if state.obs_controller and state.obs_controller.start_recording():
                self._ok("OBS录制已启动")
            else:
                self._ok("OBS录制启动失败")
                
        elif cmd == "obsstop":
            # 手动停止OBS录制
            if state.obs_controller and state.obs_controller.stop_recording():
                self._ok("OBS录制已停止")
            else:
                self._ok("OBS录制停止失败")
                
        elif cmd == "timewindow":
            # 获取或设置时间窗口
            if "value" in qs:
                try:
                    new_value = int(qs["value"][0])
                    if new_value > 0:
                        state.config.time_window_minutes = new_value
                        state.logger.info(f"时间窗口已设置为 {new_value} 分钟")
                        self._ok(f"时间窗口已设置为 {new_value} 分钟")
                    else:
                        self._ok("时间窗口必须大于0")
                except ValueError:
                    self._ok("无效的时间窗口值")
            else:
                self._ok(str(state.config.time_window_minutes))

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
    # 检测是否作为服务运行
    state.is_service = is_running_as_service()
    
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
    start_control_server()
    
    # 初始化OBS控制器
    state.init_obs_controller()
    
    # 启动日志清理线程
    log_cleanup_thread = threading.Thread(target=log_cleanup_loop, daemon=True)
    log_cleanup_thread.start()
    
    # 启动OBS录制监控线程
    if state.obs_controller:
        obs_monitor_thread = threading.Thread(target=state.obs_controller.recording_loop, daemon=True)
        obs_monitor_thread.start()
    
    state.logger.info(
        f"程序已启动 "
        f"(JMA阈值: {state.config.trigger_jma_intensity}, "
        f"CEA阈值: {state.config.trigger_cea_intensity}, "
        f"CWA阈值: {state.config.trigger_cwa_intensity}, "
        f"时间窗口: {state.config.time_window_minutes}分钟, "
        f"日志保留天数: {state.config.log_retention_days})"
    )
    
    state.logger.info(f"运行模式: {'服务模式' if state.is_service else '控制台模式'}")

    # 启动三个监控线程
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
    
    cwa_thread = threading.Thread(
        target=cwa_monitor_loop, 
        daemon=True
    )
    
    jma_thread.start()
    cea_thread.start()
    cwa_thread.start()

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
