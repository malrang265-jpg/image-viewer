import sys
import os
import json
import zipfile
import threading
import re
import ctypes
import ctypes.wintypes
import concurrent.futures
from io import BytesIO
from collections import OrderedDict

os.environ['QT_OPENGL'] = 'software'

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QDialog, QHBoxLayout, QComboBox, QCheckBox, QPushButton,
                            QColorDialog, QGroupBox, QFormLayout, QSpinBox,
                            QListWidget, QListWidgetItem, QMessageBox,
                            QListView, QSlider)
from PyQt5.QtCore import Qt, QTimer, QObject, QByteArray, QSize, QThread, pyqtSignal, QPoint, QEvent
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QWheelEvent, QTransform,
                        QMovie, QKeyEvent, QCloseEvent, QMouseEvent, QIcon)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PIL_Image = None
PIL_ImageEnhance = None

def get_pil_image():
    global PIL_Image
    if PIL_Image is None:
        from PIL import Image
        PIL_Image = Image
    return PIL_Image

def get_pil_enhance():
    global PIL_ImageEnhance
    if PIL_ImageEnhance is None:
        from PIL import ImageEnhance
        PIL_ImageEnhance = ImageEnhance
    return PIL_ImageEnhance

def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

def get_icon_path():
    possible_paths = [
        os.path.join(get_app_dir(), 'icon.ico'),
        os.path.join(getattr(sys, '_MEIPASS', ''), 'icon.ico') if hasattr(sys, '_MEIPASS') else '',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico'),
        os.path.join(os.path.dirname(get_app_dir()), 'icon.ico'),
    ]
    for path in possible_paths:
        if path and os.path.exists(path):
            return path
    return None

def is_animated_webp(filepath):
    try:
        Image = get_pil_image()
        with Image.open(filepath) as img:
            return getattr(img, 'is_animated', False) and getattr(img, 'n_frames', 1) > 1
    except:
        return False

class SingleApplication:
    def __init__(self, app_name="PekoviewerApp"):
        self.app_name = app_name
        self.socket = QLocalSocket()
        self.server = None
        self.file_received_callback = None
    
    def is_running(self):
        self.socket.connectToServer(self.app_name)
        if self.socket.waitForConnected(30):
            return True
        return False
    
    def start_server(self):
        self.server = QLocalServer()
        self.server.listen(self.app_name)
        self.server.newConnection.connect(self.on_new_connection)
    
    def send_message(self, message):
        if self.socket.state() == QLocalSocket.ConnectedState:
            self.socket.write(message.encode())
            self.socket.flush()
            self.socket.disconnectFromServer()
    
    def on_new_connection(self):
        socket = self.server.nextPendingConnection()
        if socket.waitForReadyRead(30):
            data = socket.readAll().data().decode('utf-8', errors='ignore')
            if self.file_received_callback and data:
                self.file_received_callback(data)
        socket.disconnectFromServer()
    
    def set_file_received_callback(self, callback):
        self.file_received_callback = callback

class Settings:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        app_dir = get_app_dir()
        self.settings_file = os.path.join(app_dir, 'pekoviewer_settings.json')
        
        if not os.path.exists(self.settings_file):
            self.data = self.default_settings()
            self.save()
        else:
            self.load()
    
    def load(self):
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except:
            self.data = self.default_settings()
    
    def save(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except:
            pass
    
    def default_settings(self):
        return {
            'window_geometry': None,
            'zoom_quality': 'balanced',
            'show_filename': False,
            'background_color': '#2b2b2b',
            'fit_to_window': True,
            'snap_enabled': True,
            'snap_threshold': 20,
            'saturation': 100,
            'brightness': 100,
            'contrast': 100,
            'slideshow_interval': 3,
            'slideshow_mode': 'time',
            'slideshow_gif_loops': 2,
            'cache_size': 200,
            'cache_mb': 768,
            'preload_next': True,
            'preload_count': 3,
            'shortcuts': {
                'next_image': ['Right', ''],
                'prev_image': ['Left', ''],
                'zoom_in': ['Up', ''],
                'zoom_out': ['Down', ''],
                'toggle_actual_size': ['0', ''],
                'toggle_fullscreen': ['F11', ''],
                'close_program': ['Ctrl+Q', ''],
                'show_image_list': ['Tab', ''],
                'delete_image': ['Delete', ''],
                'open_file': ['Ctrl+O', ''],
                'slideshow': ['S', ''],
                'rotate_right': ['R', ''],
                'rotate_left': ['L', '']
            }
        }
    
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    def set(self, key, value):
        self.data[key] = value
        self.save()
    
    def get_shortcuts(self, action):
        shortcuts = self.data.get('shortcuts', {})
        value = shortcuts.get(action, ['', ''])
        if isinstance(value, str):
            return [value, '']
        if isinstance(value, list):
            while len(value) < 2:
                value.append('')
            return value[:2]
        return ['', '']
    
    def set_shortcuts(self, action, shortcuts_list):
        if 'shortcuts' not in self.data:
            self.data['shortcuts'] = {}
        self.data['shortcuts'][action] = shortcuts_list
        self.save()

class ImageLoader:
    _shutdown = False
    SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(2, min(4, (os.cpu_count() or 4))))

    @staticmethod
    def is_supported(filename):
        return os.path.splitext(filename)[1].lower() in ImageLoader.SUPPORTED_FORMATS

    @staticmethod
    def load_image_data(filepath, saturation=100, brightness=100, contrast=100, max_size=None):
        try:
            Image = get_pil_image()
            with Image.open(filepath) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                img = src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    resample = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
                    img.thumbnail(max_size, resample)
                if saturation != 100:
                    img = get_pil_enhance().Color(img).enhance(saturation / 100.0)
                if brightness != 100:
                    img = get_pil_enhance().Brightness(img).enhance(brightness / 100.0)
                if contrast != 100:
                    img = get_pil_enhance().Contrast(img).enhance(contrast / 100.0)
                data = img.tobytes('raw', 'RGB')
                return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        except Exception as e:
            print(f"이미지 백그라운드 로드 오류: {e}")
        return None

    @staticmethod
    def load_pixmap(filepath, quality='balanced', saturation=100, brightness=100, contrast=100):
        image = ImageLoader.load_image_data(filepath, saturation, brightness, contrast)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        try:
            pixmap = QPixmap(filepath)
            return pixmap if not pixmap.isNull() else None
        except Exception:
            return None

    @staticmethod
    def load_thumbnail(filepath, size=(150, 150)):
        image = ImageLoader.load_image_data(filepath, max_size=size)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        return None

    @staticmethod
    @classmethod
    def shutdown_executor(cls):
        cls._shutdown = True
        try:
            cls._executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            cls._executor.shutdown(wait=True)
        except Exception:
            pass

    @classmethod
    def restart_executor(cls):
        if cls._shutdown:
            cls._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(2, min(4, (os.cpu_count() or 4))))
            cls._shutdown = False

    @staticmethod
    def load_movie(filepath):
        try:
            movie = QMovie(filepath)
            if movie.isValid():
                return movie
        except Exception:
            pass
        return None


class CacheManager:
    def __init__(self, max_size=200, max_mb=768):
        self.max_size = max(20, int(max_size or 200))
        self.max_bytes = max(128, int(max_mb or 768)) * 1024 * 1024
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.lock = threading.RLock()

    @staticmethod
    def _cost(value):
        try:
            return max(1, value.width() * value.height() * 4)
        except Exception:
            return 1

    def get(self, key):
        with self.lock:
            value = self.cache.pop(key, None)
            if value is not None:
                self.cache[key] = value
            return value

    def put(self, key, value):
        if value is None:
            return
        cost = self._cost(value)
        with self.lock:
            old = self.cache.pop(key, None)
            if old is not None:
                self.cache_bytes -= self._cost(old)
            while self.cache and (len(self.cache) >= self.max_size or self.cache_bytes + cost > self.max_bytes):
                _, evicted = self.cache.popitem(last=False)
                self.cache_bytes -= self._cost(evicted)
            if cost <= self.max_bytes:
                self.cache[key] = value
                self.cache_bytes += cost

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.cache_bytes = 0


class ZipHandler:
    _thread_local = threading.local()
    _supported = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

    @staticmethod
    def is_zip(filename):
        return filename.lower().endswith('.zip')

    @staticmethod
    def list_images(zip_path):
        images = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for info in zf.infolist():
                    if not info.is_dir() and os.path.splitext(info.filename)[1].lower() in ZipHandler._supported:
                        images.append(info.filename)
        except Exception as e:
            print(f"ZIP 목록 로드 오류: {e}")
        def natural_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        images.sort(key=natural_key)
        return images

    @staticmethod
    def _get_zip(zip_path):
        handles = getattr(ZipHandler._thread_local, 'handles', None)
        if handles is None:
            handles = {}
            ZipHandler._thread_local.handles = handles
        key = os.path.abspath(zip_path)
        zf = handles.get(key)
        if zf is None or zf.fp is None:
            zf = zipfile.ZipFile(key, 'r')
            handles[key] = zf
        return zf

    @staticmethod
    def load_image_data(zip_path, filename, saturation=100, brightness=100, contrast=100, max_size=None):
        try:
            zf = ZipHandler._get_zip(zip_path)
            with zf.open(filename, 'r') as fp:
                data = fp.read()
            Image = get_pil_image()
            with Image.open(BytesIO(data)) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                img = src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    resample = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
                    img.thumbnail(max_size, resample)
                if saturation != 100:
                    img = get_pil_enhance().Color(img).enhance(saturation / 100.0)
                if brightness != 100:
                    img = get_pil_enhance().Brightness(img).enhance(brightness / 100.0)
                if contrast != 100:
                    img = get_pil_enhance().Contrast(img).enhance(contrast / 100.0)
                raw = img.tobytes('raw', 'RGB')
                return QImage(raw, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        except Exception as e:
            print(f"ZIP 이미지 백그라운드 로드 오류: {e}")
        return None

    @staticmethod
    def load_image_from_zip(zip_path, filename, saturation=100, brightness=100, contrast=100):
        image = ZipHandler.load_image_data(zip_path, filename, saturation, brightness, contrast)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        return None


class ImageLoadBridge(QObject):
    loaded = pyqtSignal(int, str, object, bool)
    animated_frame = pyqtSignal(int, int, object)

class ImageListDialog(QDialog):
    def __init__(self, image_list, current_index, parent=None, current_zip=None):
        super().__init__(parent)
        self.image_list = image_list
        self.current_index = current_index
        self.selected_index = current_index
        self.current_zip = current_zip
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle('이미지 목록')
        self.setModal(True)
        self.setMinimumSize(400, 500)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QListWidget { background-color: #3c3c3c; color: white; border: 1px solid #555; }
            QListWidget::item:selected { background-color: #4a90d9; }
            QLabel { color: white; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px; }
            QPushButton:hover { background-color: #4c4c4c; }
        """)
        layout = QVBoxLayout(self)
        self.preview_label = QLabel('이미지를 선택하세요')
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(200)
        self.preview_label.setStyleSheet("border: 1px solid #555; background-color: #3c3c3c;")
        layout.addWidget(self.preview_label)
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListView.ListMode)
        self.list_widget.setSpacing(2)
        for i, image_path in enumerate(self.image_list):
            display_name = os.path.basename(image_path)
            item = QListWidgetItem(display_name)
            item.setData(Qt.UserRole, i)
            self.list_widget.addItem(item)
        self.list_widget.setCurrentRow(self.current_index)
        self.list_widget.itemDoubleClicked.connect(self.on_double_click)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        layout.addWidget(self.list_widget)
        self.show_preview(self.current_index)
        button_layout = QHBoxLayout()
        select_button = QPushButton('선택')
        select_button.clicked.connect(self.accept)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(select_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def on_item_clicked(self, item):
        self.selected_index = item.data(Qt.UserRole)
        self.show_preview(self.selected_index)
    
    def on_double_click(self, item):
        self.selected_index = item.data(Qt.UserRole)
        self.accept()
    
    def show_preview(self, index):
        if index < 0 or index >= len(self.image_list):
            return
        self.preview_label.setText('로딩 중...')
        try:
            if self.current_zip:
                pixmap = ZipHandler.load_image_from_zip(self.current_zip, self.image_list[index])
            else:
                pixmap = ImageLoader.load_thumbnail(self.image_list[index])
            if pixmap and not pixmap.isNull():
                scaled = pixmap.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.preview_label.setPixmap(scaled)
        except:
            self.preview_label.setText('미리보기 불가')
    
    def get_selected_index(self):
        return self.selected_index

class ShortcutSettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.shortcut_buttons = {}
        self.capturing = False
        self.current_action = None
        self.current_slot = 0
        self.capture_timer = QTimer()
        self.capture_timer.setSingleShot(True)
        self.capture_timer.timeout.connect(self.finish_capture)
        self.captured_keys = []
        self.init_ui()
        self.load_shortcuts()
    
    def init_ui(self):
        self.setWindowTitle('단축키 설정')
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QLabel { color: white; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px 10px; }
            QPushButton:hover { background-color: #4c4c4c; }
            QGroupBox { color: white; border: 1px solid #555; margin-top: 10px; }
        """)
        layout = QVBoxLayout(self)
        info_label = QLabel('버튼 클릭 후 1초 동안 입력한 모든 키/마우스 버튼이 단축키로 설정됩니다.\n더블클릭: Left Double Click / Right Double Click\nESC: 삭제')
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        actions = [
            ('next_image', '다음 이미지'), ('prev_image', '이전 이미지'),
            ('toggle_fullscreen', '전체화면 토글'), ('close_program', '프로그램 닫기'),
            ('show_image_list', '이미지 목록 표시'), ('zoom_in', '확대'),
            ('zoom_out', '축소'), ('toggle_actual_size', '실제 크기/창 크기 토글'),
            ('delete_image', '삭제'), ('open_file', '열기'),
            ('slideshow', '슬라이드쇼'), ('rotate_right', '오른쪽 회전'),
            ('rotate_left', '왼쪽 회전'),
        ]
        for action_key, action_name in actions:
            group = QGroupBox(action_name)
            group_layout = QHBoxLayout()
            button1 = QPushButton('단축키 1')
            button1.setMinimumWidth(120)
            button1.clicked.connect(lambda checked, k=action_key, s=0, b=button1: self.start_capture(k, s, b))
            button2 = QPushButton('단축키 2')
            button2.setMinimumWidth(120)
            button2.clicked.connect(lambda checked, k=action_key, s=1, b=button2: self.start_capture(k, s, b))
            self.shortcut_buttons[action_key] = [button1, button2]
            group_layout.addWidget(button1)
            group_layout.addWidget(button2)
            group.setLayout(group_layout)
            layout.addWidget(group)
        button_layout = QHBoxLayout()
        reset_button = QPushButton('기본값 복원')
        reset_button.clicked.connect(self.reset_defaults)
        button_layout.addWidget(reset_button)
        button_layout.addStretch()
        save_button = QPushButton('저장')
        save_button.clicked.connect(self.save_shortcuts)
        button_layout.addWidget(save_button)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def load_shortcuts(self):
        actions = ['next_image', 'prev_image', 'toggle_fullscreen', 'close_program',
                  'show_image_list', 'zoom_in', 'zoom_out', 'toggle_actual_size',
                  'delete_image', 'open_file', 'slideshow', 'rotate_right', 'rotate_left']
        for action in actions:
            shortcuts = self.settings.get_shortcuts(action)
            if action in self.shortcut_buttons:
                for i, button in enumerate(self.shortcut_buttons[action]):
                    text = shortcuts[i] if i < len(shortcuts) and shortcuts[i] else '없음'
                    button.setText(text)
    
    def start_capture(self, action_key, slot, button):
        if self.capturing:
            return
        self.capturing = True
        self.current_action = action_key
        self.current_slot = slot
        self.captured_keys = []
        button.setText('입력 중... (1초)')
        button.setStyleSheet("background-color: #4a90d9; color: white; border: 1px solid #555; padding: 5px 10px;")
        self.grabKeyboard()
        self.setFocus()
        self.capture_timer.start(1000)
    
    def finish_capture(self):
        if self.capturing and self.current_action:
            if self.captured_keys:
                shortcut_text = self.captured_keys[0]
                self.shortcut_buttons[self.current_action][self.current_slot].setText(shortcut_text)
            else:
                self.shortcut_buttons[self.current_action][self.current_slot].setText('없음')
            self.shortcut_buttons[self.current_action][self.current_slot].setStyleSheet("")
            self.capturing = False
            self.current_action = None
            self.current_slot = 0
            self.captured_keys = []
            self.releaseKeyboard()
    
    def keyPressEvent(self, event):
        if self.capturing and self.current_action:
            key = event.key()
            modifiers = event.modifiers()
            if key == Qt.Key_Escape:
                self.shortcut_buttons[self.current_action][self.current_slot].setText('없음')
                self.shortcut_buttons[self.current_action][self.current_slot].setStyleSheet("")
                self.capture_timer.stop()
                self.capturing = False
                self.current_action = None
                self.current_slot = 0
                self.captured_keys = []
                self.releaseKeyboard()
                return
            key_sequence = QKeySequence(modifiers | key).toString()
            if key_sequence and key_sequence not in self.captured_keys:
                self.captured_keys.append(key_sequence)
        super().keyPressEvent(event)
    
    def mousePressEvent(self, event):
        if self.capturing and self.current_action:
            button = event.button()
            mouse_buttons = {
                Qt.LeftButton: 'Left Click',
                Qt.RightButton: 'Right Click',
                Qt.MiddleButton: 'Middle Click',
                Qt.XButton1: 'XButton1',
                Qt.XButton2: 'XButton2'
            }
            if button in mouse_buttons:
                button_text = mouse_buttons[button]
                if button_text not in self.captured_keys:
                    self.captured_keys.append(button_text)
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        if self.capturing and self.current_action:
            if event.button() == Qt.LeftButton:
                if 'Left Double Click' not in self.captured_keys:
                    self.captured_keys.insert(0, 'Left Double Click')
            elif event.button() == Qt.RightButton:
                if 'Right Double Click' not in self.captured_keys:
                    self.captured_keys.insert(0, 'Right Double Click')
        super().mouseDoubleClickEvent(event)
    
    def reset_defaults(self):
        defaults = self.settings.default_settings()['shortcuts']
        for action, shortcuts in defaults.items():
            if action in self.shortcut_buttons:
                for i, button in enumerate(self.shortcut_buttons[action]):
                    text = shortcuts[i] if i < len(shortcuts) and shortcuts[i] else '없음'
                    button.setText(text)
    
    def save_shortcuts(self):
        for action, buttons in self.shortcut_buttons.items():
            shortcuts = [buttons[0].text(), buttons[1].text()]
            shortcuts = [s if s != '없음' else '' for s in shortcuts]
            self.settings.set_shortcuts(action, shortcuts)
        self.accept()

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.init_ui()
        self.load_settings()
    
    def init_ui(self):
        self.setWindowTitle('설정')
        self.setModal(True)
        self.setMinimumWidth(450)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: #ffffff; }
            QGroupBox { color: #ffffff; border: 1px solid #555; margin-top: 10px; }
            QLabel { color: #ffffff; }
            QCheckBox { color: #ffffff; }
            QComboBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 3px; }
            QComboBox QAbstractItemView { background-color: #3c3c3c; color: #ffffff; selection-background-color: #4a90d9; }
            QSpinBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 3px; }
            QSlider::groove:horizontal { height: 8px; background: #555; border-radius: 4px; }
            QSlider::handle:horizontal { width: 24px; height: 24px; margin: -8px 0; background: #4a90d9; border-radius: 12px; }
            QSlider::handle:horizontal:hover { background: #6aa8e8; }
            QPushButton { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 5px 10px; }
            QPushButton:hover { background-color: #4c4c4c; }
        """)
        layout = QVBoxLayout(self)
        
        display_group = QGroupBox('이미지 표시')
        display_layout = QFormLayout()
        self.zoom_quality = QComboBox()
        self.zoom_quality.addItem('속도 우선', 'speed')
        self.zoom_quality.addItem('균형', 'balanced')
        self.zoom_quality.addItem('품질 우선', 'quality')
        display_layout.addRow('확대/축소 품질:', self.zoom_quality)
        self.show_filename = QCheckBox('파일명 표시')
        display_layout.addRow('', self.show_filename)
        self.fit_to_window = QCheckBox('창에 맞추기')
        display_layout.addRow('', self.fit_to_window)

        self.preload_enabled = QCheckBox('주변 이미지 미리 로딩')
        display_layout.addRow('', self.preload_enabled)
        self.preload_count = QComboBox()
        for count, label in [(0, '사용 안 함'), (1, '앞/뒤 1장'), (2, '앞/뒤 2장'),
                             (3, '앞/뒤 3장'), (5, '앞/뒤 5장'), (10, '앞/뒤 10장')]:
            self.preload_count.addItem(label, count)
        display_layout.addRow('미리 로딩 범위:', self.preload_count)
        display_group.setLayout(display_layout)
        layout.addWidget(display_group)
        
        adjust_group = QGroupBox('이미지 조절')
        adjust_layout = QFormLayout()
        
        self.saturation_slider = QSlider(Qt.Horizontal)
        self.saturation_slider.setRange(0, 200)
        self.saturation_slider.setValue(100)
        self.saturation_label = QLabel('100%')
        saturation_row = QHBoxLayout()
        saturation_row.addWidget(self.saturation_slider)
        saturation_row.addWidget(self.saturation_label)
        adjust_layout.addRow('채도:', saturation_row)
        
        self.brightness_slider = QSlider(Qt.Horizontal)
        self.brightness_slider.setRange(0, 200)
        self.brightness_slider.setValue(100)
        self.brightness_label = QLabel('100%')
        brightness_row = QHBoxLayout()
        brightness_row.addWidget(self.brightness_slider)
        brightness_row.addWidget(self.brightness_label)
        adjust_layout.addRow('밝기:', brightness_row)
        
        self.contrast_slider = QSlider(Qt.Horizontal)
        self.contrast_slider.setRange(0, 200)
        self.contrast_slider.setValue(100)
        self.contrast_label = QLabel('100%')
        contrast_row = QHBoxLayout()
        contrast_row.addWidget(self.contrast_slider)
        contrast_row.addWidget(self.contrast_label)
        adjust_layout.addRow('명도/대비:', contrast_row)

        reset_adjust_button = QPushButton('채도·밝기·명도 초기화')
        reset_adjust_button.clicked.connect(self.reset_adjustments)
        adjust_layout.addRow('', reset_adjust_button)
        
        apply_button = QPushButton('현재 이미지에 즉시 적용')
        apply_button.clicked.connect(self.apply_immediately)
        adjust_layout.addRow('', apply_button)
        
        adjust_group.setLayout(adjust_layout)
        layout.addWidget(adjust_group)
        
        snap_group = QGroupBox('창 자석 기능')
        snap_layout = QFormLayout()
        self.snap_enabled = QCheckBox('화면 가장자리에 달라붙기')
        snap_layout.addRow('', self.snap_enabled)
        self.snap_threshold = QSpinBox()
        self.snap_threshold.setRange(5, 50)
        self.snap_threshold.setSuffix(' 픽셀')
        snap_layout.addRow('자석 작동 거리:', self.snap_threshold)
        snap_group.setLayout(snap_layout)
        layout.addWidget(snap_group)
        
        slideshow_group = QGroupBox('슬라이드쇼')
        slideshow_layout = QFormLayout()
        self.slideshow_mode = QComboBox()
        self.slideshow_mode.addItem('시간 기반', 'time')
        self.slideshow_mode.addItem('GIF 재생 횟수', 'loop')
        slideshow_layout.addRow('모드:', self.slideshow_mode)
        self.slideshow_interval = QSpinBox()
        self.slideshow_interval.setRange(1, 60)
        self.slideshow_interval.setSuffix(' 초')
        slideshow_layout.addRow('시간 간격:', self.slideshow_interval)
        self.slideshow_gif_loops = QSpinBox()
        self.slideshow_gif_loops.setRange(1, 10)
        self.slideshow_gif_loops.setSuffix(' 회')
        slideshow_layout.addRow('GIF 재생 횟수:', self.slideshow_gif_loops)
        slideshow_group.setLayout(slideshow_layout)
        layout.addWidget(slideshow_group)
        
        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel('배경색:'))
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self.choose_color)
        color_layout.addWidget(self.color_button)
        layout.addLayout(color_layout)
        
        button_layout = QHBoxLayout()
        save_button = QPushButton('저장')
        save_button.clicked.connect(self.save_settings)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        
        self.saturation_slider.valueChanged.connect(
            lambda v: self.saturation_label.setText(f'{v}%'))
        self.brightness_slider.valueChanged.connect(
            lambda v: self.brightness_label.setText(f'{v}%'))
        self.contrast_slider.valueChanged.connect(
            lambda v: self.contrast_label.setText(f'{v}%'))
    
    def reset_adjustments(self):
        self.saturation_slider.setValue(100)
        self.brightness_slider.setValue(100)
        self.contrast_slider.setValue(100)
        self.apply_immediately()

    def apply_immediately(self):
        parent = self.parent()
        if parent and hasattr(parent, 'apply_image_adjustments'):
            parent.apply_image_adjustments(
                self.saturation_slider.value(),
                self.brightness_slider.value(),
                self.contrast_slider.value()
            )
    
    def load_settings(self):
        quality = self.settings.get('zoom_quality', 'balanced')
        index = self.zoom_quality.findData(quality)
        if index >= 0:
            self.zoom_quality.setCurrentIndex(index)
        self.show_filename.setChecked(self.settings.get('show_filename', False))
        self.fit_to_window.setChecked(self.settings.get('fit_to_window', True))
        self.preload_enabled.setChecked(self.settings.get('preload_next', True))
        preload_count = int(self.settings.get('preload_count', 3))
        preload_index = self.preload_count.findData(preload_count)
        if preload_index < 0:
            preload_index = self.preload_count.findData(3)
        self.preload_count.setCurrentIndex(preload_index)
        self.saturation_slider.setValue(self.settings.get('saturation', 100))
        self.brightness_slider.setValue(self.settings.get('brightness', 100))
        self.contrast_slider.setValue(self.settings.get('contrast', 100))
        self.snap_enabled.setChecked(self.settings.get('snap_enabled', True))
        self.snap_threshold.setValue(self.settings.get('snap_threshold', 20))
        mode = self.settings.get('slideshow_mode', 'time')
        index = self.slideshow_mode.findData(mode)
        if index >= 0:
            self.slideshow_mode.setCurrentIndex(index)
        self.slideshow_interval.setValue(self.settings.get('slideshow_interval', 3))
        self.slideshow_gif_loops.setValue(self.settings.get('slideshow_gif_loops', 2))
        self.current_color = self.settings.get('background_color', '#2b2b2b')
        self.update_color_button()
    
    def choose_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.current_color = color.name()
            self.update_color_button()
    
    def update_color_button(self):
        self.color_button.setStyleSheet(f"background-color: {self.current_color}; color: white;")
        self.color_button.setText(self.current_color)
    
    def save_settings(self):
        self.settings.set('zoom_quality', self.zoom_quality.currentData())
        self.settings.set('show_filename', self.show_filename.isChecked())
        self.settings.set('fit_to_window', self.fit_to_window.isChecked())
        self.settings.set('preload_next', self.preload_enabled.isChecked())
        self.settings.set('preload_count', self.preload_count.currentData())
        self.settings.set('saturation', self.saturation_slider.value())
        self.settings.set('brightness', self.brightness_slider.value())
        self.settings.set('contrast', self.contrast_slider.value())
        self.settings.set('snap_enabled', self.snap_enabled.isChecked())
        self.settings.set('snap_threshold', self.snap_threshold.value())
        self.settings.set('slideshow_mode', self.slideshow_mode.currentData())
        self.settings.set('slideshow_interval', self.slideshow_interval.value())
        self.settings.set('slideshow_gif_loops', self.slideshow_gif_loops.value())
        self.settings.set('background_color', self.current_color)
        self.accept()

from PyQt5.QtWidgets import QMainWindow, QLabel, QScrollArea, QApplication
from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtGui import QMouseEvent, QCursor

class ImageViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # 상태 플래그 초기화
        self.resizing = False
        self.panning = False
        self.dragging = False
        self.fit_to_window = False
        self.current_movie = None
        
        # 마우스 위치 저장용 변수
        self.resize_start_pos = None
        self.resize_start_size = None
        self.resize_region = None
        self.pan_start_pos = None
        self.drag_start_pos = None
        self.window_start_pos = None
        
        self.init_ui()

    def init_ui(self):
        # (기존 UI 설정 코드들 - 레이아웃, 스크롤 에어리어 등)
        # 예시:
        # self.scroll_area = QScrollArea()
        # self.image_label = QLabel()
        # self.scroll_area.setWidget(self.image_label)
        # self.setCentralWidget(self.scroll_area)
        
        # 마우스 트래킹 활성화
        self.image_label.setMouseTracking(True)
        self.scroll_area.viewport().setMouseTracking(True)
        
        # 주의: 앱 전역 이벤트 필터는 여기서 제거되었습니다!
        self.image_label.installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)

    # ---------------------------------------------------------
    # 1. 이벤트 필터 (단순화)
    # ---------------------------------------------------------
    def eventFilter(self, obj, event):
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------
    # 2. 팬(Pan) 기능 조건 및 헬퍼 메서드
    # ---------------------------------------------------------
    def _can_pan_image(self):
        if self.fit_to_window:
            return False
        viewport = self.scroll_area.viewport().size()
        label_size = self.image_label.size()
        return label_size.width() > viewport.width() or label_size.height() > viewport.height()

    def _start_image_pan(self, global_pos):
        self.panning = True
        self.pan_start_pos = global_pos
        self.setCursor(Qt.ClosedHandCursor)

    def _move_image_pan(self, global_pos):
        delta = global_pos - self.pan_start_pos
        h_bar = self.scroll_area.horizontalScrollBar()
        v_bar = self.scroll_area.verticalScrollBar()
        h_bar.setValue(h_bar.value() - delta.x())
        v_bar.setValue(v_bar.value() - delta.y())
        self.pan_start_pos = global_pos # 위치 갱신

    def _end_image_pan(self):
        self.panning = False
        self.pan_start_pos = None
        self.setCursor(Qt.ArrowCursor)

    # ---------------------------------------------------------
    # 3. 마우스 이벤트 3대장 (우선순위: 크기조절 > 팬 > 창이동)
    # ---------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent):
        # self.show_cursor()
        # self.reset_cursor_timer()
        
        # get_resize_region은 기존에 구현하신 창 테두리 감지 메서드를 사용합니다.
        region = self.get_resize_region(event.pos()) if hasattr(self, 'get_resize_region') else None
        
        if event.button() == Qt.LeftButton:
            # 1순위: 창 테두리 클릭 시 -> 창 크기 조절
            if region:
                self.resizing = True
                self.resize_start_pos = event.globalPos()
                self.resize_start_size = self.size()
                self.resize_region = region
                event.accept()
                return
            
            # 2순위: 이미지가 창보다 클 때 -> 이미지 팬(이동)
            if self._can_pan_image():
                self._start_image_pan(event.globalPos())
                event.accept()
                return
                
            # 3순위: 그 외 윈도우 내부 클릭 시 -> 창 이동(드래그)
            if not self.isFullScreen():
                self.dragging = True
                self.drag_start_pos = event.globalPos()
                self.window_start_pos = self.pos()
                event.accept()
                return

        # 마우스 부가 버튼 단축키 처리 (기존 코드)
        button_text = ''
        if event.button() == Qt.MiddleButton:
            button_text = 'Middle Click'
        elif event.button() == Qt.XButton1:
            button_text = 'XButton1'
        elif event.button() == Qt.XButton2:
            button_text = 'XButton2'
        
        if button_text and hasattr(self, 'check_mouse_shortcut'):
            self.check_mouse_shortcut(button_text)
            
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        # self.show_cursor()
        # self.reset_cursor_timer()
        
        # 1. 창 크기 조절 중
        if self.resizing and self.resize_start_pos:
            delta = event.globalPos() - self.resize_start_pos
            new_w = self.resize_start_size.width()
            new_h = self.resize_start_size.height()
            
            if self.resize_region in ['left', 'topleft', 'bottomleft']:
                new_w = max(200, self.resize_start_size.width() - delta.x())
            elif self.resize_region in ['right', 'topright', 'bottomright']:
                new_w = max(200, self.resize_start_size.width() + delta.x())
                
            if self.resize_region in ['top', 'topleft', 'topright']:
                new_h = max(150, self.resize_start_size.height() - delta.y())
            elif self.resize_region in ['bottom', 'bottomleft', 'bottomright']:
                new_h = max(150, self.resize_start_size.height() + delta.y())
                
            self.resize(new_w, new_h)
            event.accept()
            return
            
        # 2. 이미지 팬(이동) 중
        if self.panning and self.pan_start_pos:
            self._move_image_pan(event.globalPos())
            event.accept()
            return
            
        # 3. 창 이동(드래그) 중
        if self.dragging and self.drag_start_pos and not self.isFullScreen():
            delta = event.globalPos() - self.drag_start_pos
            new_pos = self.window_start_pos + delta
            
            # 스냅 기능이 있다면 적용, 없다면 그냥 이동
            if hasattr(self, 'snap_to_edge'):
                new_pos = self.snap_to_edge(new_pos)
                
            self.move(new_pos)
            event.accept()
            return
            
        # 마우스 커서 업데이트 (가장자리 커서 모양 변경 등)
        if hasattr(self, 'update_cursor'):
            self.update_cursor(event.pos())
            
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        # self.show_cursor()
        # self.reset_cursor_timer()
        
        if event.button() == Qt.LeftButton:
            # 떼는 순간 진행 중이던 모든 작업을 종료합니다.
            if self.resizing:
                self.resizing = False
                self.resize_start_pos = None
                self.resize_start_size = None
                self.resize_region = None
                self.unsetCursor()
                self.setCursor(Qt.ArrowCursor)
                event.accept()
                return
                
            if self.panning:
                self._end_image_pan()
                event.accept()
                return
                
            if self.dragging:
                self.dragging = False
                self.drag_start_pos = None
                self.window_start_pos = None
                event.accept()
                return
                
        super().mouseReleaseEvent(event)

    # (이 아래로는 원래 가지고 계시던 이미지 열기, 단축키 처리 등의 메서드를 그대로 두시면 됩니다.)
    
    def setup_icon(self):
        icon_path = get_icon_path()
        if icon_path:
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)
            app = QApplication.instance()
            if app:
                app.setWindowIcon(icon)
    
    def showEvent(self, event):
        super().showEvent(event)
        icon_path = get_icon_path()
        if icon_path:
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)
            if self.windowHandle():
                self.windowHandle().setIcon(icon)
    
    def hide_cursor(self):
        if self.isFullScreen() and not self.cursor_hidden:
            self.setCursor(Qt.BlankCursor)
            self.cursor_hidden = True
    
    def show_cursor(self):
        if self.cursor_hidden:
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            self.cursor_hidden = False
    
    def reset_cursor_timer(self):
        if self.isFullScreen():
            self.cursor_hide_timer.start(2000)
    
    def bring_to_front(self):
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(100, self.force_foreground)
    
    def force_foreground(self):
        try:
            hwnd = int(self.winId())
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
            cur_thread = kernel32.GetCurrentThreadId()
            if cur_thread != fg_thread:
                user32.AttachThreadInput(cur_thread, fg_thread, True)
            user32.ShowWindow(hwnd, 9)
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0002 | 0x0001)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            if cur_thread != fg_thread:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
        except:
            pass
    
    def snap_to_edge(self, pos):
        if not self.settings.get('snap_enabled', True):
            return pos
        threshold = self.settings.get('snap_threshold', 20)
        screen = QApplication.primaryScreen().availableGeometry()
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        if abs(x - screen.left()) < threshold:
            x = screen.left()
        if abs((x + w) - screen.right()) < threshold:
            x = screen.right() - w
        if abs(y - screen.top()) < threshold:
            y = screen.top()
        if abs((y + h) - screen.bottom()) < threshold:
            y = screen.bottom() - h
        return QPoint(x, y)
    
    def apply_image_adjustments(self, saturation, brightness, contrast):
        self.settings.set('saturation', saturation)
        self.settings.set('brightness', brightness)
        self.settings.set('contrast', contrast)
        self.cache_manager.clear()
        if self.image_list:
            self.show_current_image()
    
    def init_ui(self):
        self.setWindowTitle('Pekoviewer')
        self.setMinimumSize(200, 150)
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.FramelessWindowHint)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.scroll_area)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(100, 100)
        self.image_label.setScaledContents(False)
        self.scroll_area.setWidget(self.image_label)
        self.apply_background_color()
        self.filename_label = QLabel('')
        self.filename_label.setAlignment(Qt.AlignCenter)
        self.filename_label.setStyleSheet("color: white; background-color: rgba(0,0,0,0.7); padding: 5px;")
        self.filename_label.hide()
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.setMouseTracking(True)
        self.scroll_area.setMouseTracking(True)
        self.image_label.setMouseTracking(True)
        self.scroll_area.viewport().setMouseTracking(True)
        self.image_label.installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)
        QApplication.instance().installEventFilter(self)
    
    def apply_background_color(self):
        bg_color = self.settings.get('background_color', '#2b2b2b')
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {bg_color}; }}
            QScrollArea {{ background-color: {bg_color}; }}
            QLabel {{ background-color: transparent; }}
        """)
    
    def get_resize_region(self, pos):
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        margin = self.resize_margin
        left = x < margin
        right = x > w - margin
        top = y < margin
        bottom = y > h - margin
        if left and top:
            return 'topleft'
        elif right and top:
            return 'topright'
        elif left and bottom:
            return 'bottomleft'
        elif right and bottom:
            return 'bottomright'
        elif left:
            return 'left'
        elif right:
            return 'right'
        elif top:
            return 'top'
        elif bottom:
            return 'bottom'
        else:
            return None
    
    def update_cursor(self, pos):
        region = self.get_resize_region(pos)
        if region in ['left', 'right']:
            self.setCursor(Qt.SizeHorCursor)
        elif region in ['top', 'bottom']:
            self.setCursor(Qt.SizeVerCursor)
        elif region in ['topleft', 'bottomright']:
            self.setCursor(Qt.SizeFDiagCursor)
        elif region in ['topright', 'bottomleft']:
            self.setCursor(Qt.SizeBDiagCursor)
        else:
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
    
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            if self.isFullScreen():
                self.show_cursor()
                self.showNormal()
                self.cursor_hide_timer.stop()
                event.accept()
                return
        
        key_sequence = QKeySequence(event.modifiers() | event.key()).toString()
        close_shortcuts = self.settings.get_shortcuts('close_program')
        if key_sequence in close_shortcuts:
            QTimer.singleShot(150, self.close)
            event.accept()
            return
        shortcut_actions = {
            'next_image': self.next_image, 'prev_image': self.prev_image,
            'zoom_in': self.zoom_in, 'zoom_out': self.zoom_out,
            'toggle_actual_size': self.toggle_actual_size,
            'toggle_fullscreen': self.toggle_fullscreen,
            'show_image_list': self.show_image_list_dialog,
            'delete_image': self.delete_image, 'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
            'rotate_right': self.rotate_right, 'rotate_left': self.rotate_left,
        }
        for action_name, callback in shortcut_actions.items():
            shortcuts = self.settings.get_shortcuts(action_name)
            if key_sequence in shortcuts:
                callback()
                event.accept()
                return
        event.accept()
    
    def check_mouse_shortcut(self, button_text):
        actions = {
            'next_image': self.next_image, 'prev_image': self.prev_image,
            'toggle_fullscreen': self.toggle_fullscreen,
            'close_program': self.close_program,
            'show_image_list': self.show_image_list_dialog,
            'zoom_in': self.zoom_in, 'zoom_out': self.zoom_out,
            'toggle_actual_size': self.toggle_actual_size,
            'delete_image': self.delete_image, 'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
            'rotate_right': self.rotate_right, 'rotate_left': self.rotate_left,
        }
        for action_name, callback in actions.items():
            shortcuts = self.settings.get_shortcuts(action_name)
            if button_text in shortcuts:
                callback()
                return True
        return False
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.load_path(urls[0].toLocalFile())
            event.acceptProposedAction()
    
    def load_settings(self):
        geometry = self.settings.get('window_geometry')
        if geometry and not self.isFullScreen():
            if isinstance(geometry, dict):
                x = geometry.get('x', 100)
                y = geometry.get('y', 100)
                w = geometry.get('width', 800)
                h = geometry.get('height', 600)
                screen = QApplication.primaryScreen().availableGeometry()
                if x < screen.left():
                    x = screen.left()
                if y < screen.top():
                    y = screen.top()
                if x + w > screen.right():
                    x = max(screen.left(), screen.right() - w)
                if y + h > screen.bottom():
                    y = max(screen.top(), screen.bottom() - h)
                self.setGeometry(x, y, w, h)
    
    def save_settings(self):
        if not self.isFullScreen():
            pos = self.pos()
            size = self.size()
            geometry_data = {
                'x': pos.x(),
                'y': pos.y(),
                'width': size.width(),
                'height': size.height()
            }
            self.settings.set('window_geometry', geometry_data)
    
    def load_path(self, path):
        self.bring_to_front()
        if os.path.isdir(path):
            self.load_directory(path)
        elif os.path.isfile(path):
            if ZipHandler.is_zip(path):
                self.load_zip(path)
            else:
                self.load_single_file(path)
    
    def natural_sort_key(self, s):
        return [int(text) if text.isdigit() else text.lower() 
                for text in re.split(r'(\d+)', s)]
    
    def load_directory(self, directory):
        self.load_generation += 1
        self.cache_manager.clear()
        self.image_list = []
        self.current_zip = None
        try:
            files = sorted(os.listdir(directory), key=self.natural_sort_key)
            for filename in files:
                if ImageLoader.is_supported(filename):
                    self.image_list.append(os.path.join(directory, filename))
        except:
            pass
        if self.image_list:
            self.current_index = 0
            self.show_current_image()
        else:
            self.image_label.clear()
    
    def load_single_file(self, filepath):
        directory = os.path.dirname(filepath)
        self.load_directory(directory)
        try:
            abs_path = os.path.abspath(filepath)
            for i, img_path in enumerate(self.image_list):
                if os.path.abspath(img_path) == abs_path:
                    self.current_index = i
                    break
            self.show_current_image()
        except:
            pass
    
    def load_zip(self, zip_path):
        self.load_generation += 1
        self.cache_manager.clear()
        self.current_zip = zip_path
        self.image_list = ZipHandler.list_images(zip_path)
        if self.image_list:
            self.current_index = 0
            self.show_current_image()
        else:
            self.image_label.clear()
    
    def stop_current_movie(self):
        if self.current_movie:
            try:
                self.current_movie.frameChanged.disconnect(self.on_gif_frame_changed)
            except:
                pass
            try:
                self.current_movie.frameChanged.disconnect(self.on_animated_frame_changed)
            except:
                pass
            self.gif_frame_connected = False
            self.current_movie.stop()
            self.current_movie = None
        self.current_movie_original_size = None
        self.current_movie_generation += 1
        self.current_movie_frame = -1
        self.gif_last_frame = -1
        self.animated_frame_cache.clear()
    
    def _cache_key(self, filepath, saturation, brightness, contrast, max_size):
        size_key = 'full' if not max_size else f'{max_size[0]}x{max_size[1]}'
        source = f'{self.current_zip}|{filepath}' if self.current_zip else filepath
        return f'{source}|{saturation}|{brightness}|{contrast}|{size_key}'

    def _target_decode_size(self):
        if not self.fit_to_window:
            return None
        size = self.scroll_area.size()
        return (max(64, size.width() + 64), max(64, size.height() + 64))

    def _submit_image_load(self, index, generation=None, force=False):
        if ImageLoader._shutdown:
            ImageLoader.restart_executor()
        if generation is None:
            generation = self.load_generation
        if index < 0 or index >= len(self.image_list):
            return
        filename = self.image_list[index]
        saturation = self.settings.get('saturation', 100)
        brightness = self.settings.get('brightness', 100)
        contrast = self.settings.get('contrast', 100)
        max_size = self._target_decode_size()
        key = self._cache_key(filename, saturation, brightness, contrast, max_size)
        if not force and self.cache_manager.get(key) is not None:
            return
        if key in self.loading_keys:
            return
        self.loading_keys.add(key)
        source_zip = self.current_zip
        def worker():
            if source_zip:
                image = ZipHandler.load_image_data(source_zip, filename, saturation, brightness, contrast, max_size)
            else:
                image = ImageLoader.load_image_data(filename, saturation, brightness, contrast, max_size)
                if image is None and max_size is None:
                    try:
                        image = QImage(filename)
                    except Exception:
                        image = None
            self.load_bridge.loaded.emit(generation, key, image, index == self.current_index)
        try:
            ImageLoader._executor.submit(worker)
        except Exception as e:
            self.loading_keys.discard(key)
            print(f"백그라운드 로딩 시작 오류: {e}")

    def _on_background_loaded(self, generation, key, image, was_current):
        self.loading_keys.discard(key)

        current_key = None
        if self.image_list and 0 <= self.current_index < len(self.image_list):
            current_key = self._cache_key(
                self.image_list[self.current_index],
                self.settings.get('saturation', 100),
                self.settings.get('brightness', 100),
                self.settings.get('contrast', 100),
                self._target_decode_size()
            )

        if image is None or image.isNull():
            if key == current_key:
                count = self.load_retry_counts.get(key, 0)
                if count < 1:
                    self.load_retry_counts[key] = count + 1
                    QTimer.singleShot(
                        40,
                        lambda k=key, g=self.load_generation:
                            self._retry_current_load(k, g)
                    )
                else:
                    self.load_retry_counts.pop(key, None)
                    self.is_loading = False
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            if key == current_key:
                count = self.load_retry_counts.get(key, 0)
                if count < 1:
                    self.load_retry_counts[key] = count + 1
                    QTimer.singleShot(
                        40,
                        lambda k=key, g=self.load_generation:
                            self._retry_current_load(k, g)
                    )
                else:
                    self.load_retry_counts.pop(key, None)
                    self.is_loading = False
            return

        self.load_retry_counts.pop(key, None)
        self.cache_manager.put(key, pixmap)

        if key == current_key:
            self._display_pixmap(pixmap)
            self.is_loading = False

    def _retry_current_load(self, key, generation):
        if generation != self.load_generation:
            return
        if not self.image_list or not (0 <= self.current_index < len(self.image_list)):
            return
        current_key = self._cache_key(
            self.image_list[self.current_index],
            self.settings.get('saturation', 100),
            self.settings.get('brightness', 100),
            self.settings.get('contrast', 100),
            self._target_decode_size()
        )
        if key == current_key and self.cache_manager.get(key) is None:
            self._submit_image_load(self.current_index, generation, force=True)
        
    def _display_pixmap(self, pixmap):
        if not pixmap or pixmap.isNull():
            return
        if self.rotation_angle != 0:
            pixmap = pixmap.transformed(QTransform().rotate(self.rotation_angle), Qt.SmoothTransformation)
        self.current_pixmap = pixmap
        self.original_pixmap = pixmap
        self.update_image_display()
        if self.settings.get('show_filename', False):
            current_file = self.image_list[self.current_index]
            display_name = os.path.basename(current_file) if not self.current_zip else current_file
            self.filename_label.setText(display_name)
            self.filename_label.show()
            self.filename_label.adjustSize()
            self.filename_label.move(10, 10)
        else:
            self.filename_label.hide()

    def _preload_neighbors(self):
        if not self.preload_enabled or not self.image_list:
            return
        count = max(0, min(10, int(self.preload_count)))
        if count <= 0:
            return

        generation = self.load_generation
        for distance in range(1, count + 1):
            for direction in (1, -1):
                idx = self.current_index + (distance * direction)
                if 0 <= idx < len(self.image_list):
                    self._submit_image_load(idx, generation)

    def show_current_image(self):
        if not self.image_list or self.current_index < 0 or self.current_index >= len(self.image_list):
            return
        self.load_generation += 1
        generation = self.load_generation
        self.stop_current_movie()
        current_file = self.image_list[self.current_index]
        saturation = self.settings.get('saturation', 100)
        brightness = self.settings.get('brightness', 100)
        contrast = self.settings.get('contrast', 100)

        if not self.current_zip:
            ext = os.path.splitext(current_file)[1].lower()
            if ext == '.gif' or (ext == '.webp' and is_animated_webp(current_file)):
                movie = ImageLoader.load_movie(current_file)
                if movie:
                    self.current_movie = movie
                    self.current_movie_generation += 1
                    movie_generation = self.current_movie_generation
                    movie.jumpToFrame(0)
                    self.current_movie_original_size = movie.currentPixmap().size()
                    if self.fit_to_window and self.current_movie_original_size.width() > 0:
                        scaled_size = self.current_movie_original_size.scaled(self.scroll_area.size(), Qt.KeepAspectRatio)
                        if scaled_size.width() > 0 and scaled_size.height() > 0:
                            movie.setScaledSize(scaled_size)
                    self.current_movie_frame = -1
                    self.animated_frame_cache.clear()
                    movie.frameChanged.connect(self.on_animated_frame_changed)
                    if self.slideshow_playing and self.slideshow_mode == 'loop':
                        self.connect_gif_loop()
                    movie.start()
                    self._render_animated_frame(movie.currentFrameNumber(), movie_generation)
                    self.is_loading = False
                    return

        max_size = self._target_decode_size()
        key = self._cache_key(current_file, saturation, brightness, contrast, max_size)
        cached = self.cache_manager.get(key)
        if cached is not None:
            self.is_loading = False
            self._display_pixmap(cached)
            self._preload_neighbors()
            return

        self.is_loading = True
        self._submit_image_load(self.current_index, generation)
        self._preload_neighbors()

    def _animated_cache_key(self, frame_number):
        return (frame_number,
                self.settings.get('saturation', 100),
                self.settings.get('brightness', 100),
                self.settings.get('contrast', 100),
                self.fit_to_window,
                self.scroll_area.size().width(),
                self.scroll_area.size().height())

    def on_animated_frame_changed(self, frame_number):
        if not self.current_movie:
            return
        self.current_movie_frame = frame_number
        self._render_animated_frame(frame_number, self.current_movie_generation)

    def _render_animated_frame(self, frame_number, generation):
        if not self.current_movie or generation != self.current_movie_generation:
            return
        movie = self.current_movie
        qimage = movie.currentImage()
        if qimage.isNull():
            qimage = movie.currentPixmap().toImage()
        if qimage.isNull():
            return
        key = self._animated_cache_key(frame_number)
        cached = self.animated_frame_cache.get(key)
        if cached is not None:
            self.animated_frame_cache.move_to_end(key)
            self.current_pixmap = cached
            self.image_label.setPixmap(cached)
            self.image_label.adjustSize()
            return

        saturation = self.settings.get('saturation', 100)
        brightness = self.settings.get('brightness', 100)
        contrast = self.settings.get('contrast', 100)
        if saturation == 100 and brightness == 100 and contrast == 100:
            pixmap = QPixmap.fromImage(qimage)
            self._store_animated_frame(key, pixmap)
            self.current_pixmap = pixmap
            self.image_label.setPixmap(pixmap)
            self.image_label.adjustSize()
            return

        try:
            rgba = qimage.convertToFormat(QImage.Format_RGBA8888)
            w, h = rgba.width(), rgba.height()
            ptr = rgba.bits()
            ptr.setsize(rgba.byteCount())
            raw = bytes(ptr)
            settings = (saturation, brightness, contrast)
            def worker():
                try:
                    from PIL import Image, ImageEnhance
                    src = Image.frombuffer('RGBA', (w, h), raw, 'raw', 'RGBA', 0, 1)
                    rgb = src.convert('RGB')
                    if saturation != 100:
                        rgb = ImageEnhance.Color(rgb).enhance(saturation / 100.0)
                    if brightness != 100:
                        rgb = ImageEnhance.Brightness(rgb).enhance(brightness / 100.0)
                    if contrast != 100:
                        rgb = ImageEnhance.Contrast(rgb).enhance(contrast / 100.0)
                    out = rgb.convert('RGBA')
                    return out.tobytes('raw', 'RGBA'), w, h
                except Exception:
                    return None
            future = ImageLoader._executor.submit(worker)
            def done(fut, gen=generation, frame=frame_number, key=key):
                try:
                    result = fut.result()
                except Exception:
                    result = None
                self.load_bridge.animated_frame.emit(gen, frame, (key, result))
            future.add_done_callback(done)
        except Exception:
            pass

    def _on_animated_frame_ready(self, generation, frame_number, payload):
        if generation != self.current_movie_generation or not self.current_movie:
            return
        key, result = payload
        if not result:
            return
        raw, w, h = result
        qimg = QImage(raw, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pixmap = QPixmap.fromImage(qimg)
        if pixmap.isNull():
            return
        self._store_animated_frame(key, pixmap)
        if self.current_movie_frame == frame_number:
            self.current_pixmap = pixmap
            self.image_label.setPixmap(pixmap)
            self.image_label.adjustSize()

    def _store_animated_frame(self, key, pixmap):
        self.animated_frame_cache[key] = pixmap
        self.animated_frame_cache.move_to_end(key)
        while len(self.animated_frame_cache) > self.animated_frame_cache_limit:
            self.animated_frame_cache.popitem(last=False)

    def connect_gif_loop(self):
        if self.current_movie and not self.gif_frame_connected:
            self.current_movie.frameChanged.connect(self.on_gif_frame_changed)
            self.gif_frame_connected = True
    
    def on_gif_frame_changed(self, frame_number):
        if not self.slideshow_playing or self.slideshow_mode != 'loop':
            self.gif_last_frame = frame_number
            return
        if frame_number == 0 and self.gif_last_frame > 0:
            self.gif_loop_count += 1
            if self.gif_loop_count >= self.gif_max_loops:
                self.gif_loop_count = 0
                self.gif_last_frame = -1
                self.next_image()
                return
        self.gif_last_frame = frame_number
    
    def update_image_display(self):
        if self.panning:
            self._end_image_pan()
        if self.current_movie:
            try:
                if self.current_movie_original_size and self.current_movie_original_size.width() > 0:
                    original_size = self.current_movie_original_size
                else:
                    original_size = self.current_movie.currentPixmap().size()
                    if original_size.width() > 0:
                        self.current_movie_original_size = original_size
                if original_size.width() > 0 and original_size.height() > 0:
                    if self.fit_to_window:
                        scaled_size = original_size.scaled(self.scroll_area.size(), Qt.KeepAspectRatio)
                    else:
                        scaled_size = QSize(int(original_size.width() * self.zoom_factor),
                                           int(original_size.height() * self.zoom_factor))
                    if scaled_size.width() > 0 and scaled_size.height() > 0:
                        self.current_movie.setScaledSize(scaled_size)
                    self.current_movie_frame = self.current_movie.currentFrameNumber()
                    self._render_animated_frame(self.current_movie_frame, self.current_movie_generation)
            except:
                pass
            return
        
        if self.current_pixmap:
            if self.fit_to_window:
                scaled = self.current_pixmap.scaled(
                    self.scroll_area.size(),
                    Qt.KeepAspectRatio,
                    Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled)
            else:
                new_size = self.current_pixmap.size() * self.zoom_factor
                scaled = self.current_pixmap.scaled(
                    new_size,
                    Qt.KeepAspectRatio,
                    Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled)
            self.image_label.adjustSize()
    
    def toggle_actual_size(self):
        self.fit_to_window = not self.fit_to_window
        if self.fit_to_window:
            self.zoom_factor = 1.0
        self.show_current_image()
    
    def next_image(self):
        if self.image_list and self.current_index < len(self.image_list) - 1:
            self.current_index += 1
            self.rotation_angle = 0
            self.show_current_image()
    
    def prev_image(self):
        if self.image_list and self.current_index > 0:
            self.current_index -= 1
            self.rotation_angle = 0
            self.show_current_image()
    
    def _zoom_at(self, factor, global_pos=None):
        if not self.current_pixmap or self.current_pixmap.isNull():
            return

        if global_pos is None:
            from PyQt5.QtGui import QCursor
            global_pos = QCursor.pos()

        viewport = self.scroll_area.viewport()
        viewport_pos = viewport.mapFromGlobal(global_pos)

        old_h = self.scroll_area.horizontalScrollBar().value()
        old_v = self.scroll_area.verticalScrollBar().value()
        label_pos = self.image_label.mapFrom(viewport, viewport_pos)
        anchor_x = label_pos.x()
        anchor_y = label_pos.y()

        self.fit_to_window = False
        old_zoom = self.zoom_factor
        new_zoom = max(0.05, min(16.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-6:
            return
        self.zoom_factor = new_zoom
        self.update_image_display()

        ratio = new_zoom / old_zoom
        new_anchor_x = anchor_x * ratio
        new_anchor_y = anchor_y * ratio
        target_h = int(new_anchor_x - viewport_pos.x())
        target_v = int(new_anchor_y - viewport_pos.y())
        self.scroll_area.horizontalScrollBar().setValue(target_h)
        self.scroll_area.verticalScrollBar().setValue(target_v)

    def zoom_in(self):
        self._zoom_at(1.20)
    
    def zoom_out(self):
        self._zoom_at(1.0 / 1.20)
    
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.show_cursor()
            self.showNormal()
            self.cursor_hide_timer.stop()
        else:
            self.showFullScreen()
            self.reset_cursor_timer()
    
    def close_program(self):
        QTimer.singleShot(150, self.close)
    
    def show_image_list_dialog(self):
        if not self.image_list:
            return
        dialog = ImageListDialog(self.image_list, self.current_index, self, self.current_zip)
        if dialog.exec_() == QDialog.Accepted:
            selected = dialog.get_selected_index()
            if selected != self.current_index:
                self.current_index = selected
                self.rotation_angle = 0
                self.show_current_image()
    
    def delete_image(self):
        if not self.image_list or self.current_zip:
            return
        current_file = self.image_list[self.current_index]
        try:
            os.remove(current_file)
            self.cache_manager.clear()
            self.image_list.pop(self.current_index)
            if self.current_index >= len(self.image_list):
                self.current_index = len(self.image_list) - 1
            if self.image_list:
                self.show_current_image()
            else:
                self.image_label.clear()
                self.filename_label.hide()
        except:
            pass
    
    def open_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, '이미지 열기', '',
            '이미지 파일 (*.png *.jpg *.jpeg *.gif *.webp);;ZIP 파일 (*.zip);;모든 파일 (*)'
        )
        if file_path:
            self.load_path(file_path)
    
    def toggle_slideshow(self):
        if self.slideshow_playing:
            self.stop_slideshow()
        else:
            self.start_slideshow()
    
    def start_slideshow(self):
        self.slideshow_playing = True
        self.slideshow_mode = self.settings.get('slideshow_mode', 'time')
        self.gif_max_loops = self.settings.get('slideshow_gif_loops', 2)
        self.gif_loop_count = 0
        self.gif_last_frame = -1
        if self.slideshow_mode == 'loop' and self.current_movie:
            self.connect_gif_loop()
        else:
            interval = self.settings.get('slideshow_interval', 3)
            self.slideshow.start(interval * 1000)
    
    def stop_slideshow(self):
        self.slideshow_playing = False
        self.slideshow.stop()
        if self.gif_frame_connected and self.current_movie:
            try:
                self.current_movie.frameChanged.disconnect(self.on_gif_frame_changed)
            except:
                pass
            self.gif_frame_connected = False
    
    def rotate_right(self):
        self.rotation_angle = (self.rotation_angle + 90) % 360
        self.show_current_image()
    
    def rotate_left(self):
        self.rotation_angle = (self.rotation_angle - 90) % 360
        self.show_current_image()
    
    def show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2b2b2b; color: white; border: 1px solid #555; }
            QMenu::item:selected { background-color: #3c3c3c; }
        """)
        slideshow_action = QAction('슬라이드쇼', self)
        slideshow_action.triggered.connect(self.toggle_slideshow)
        menu.addAction(slideshow_action)
        menu.addSeparator()
        settings_action = QAction('설정', self)
        settings_action.triggered.connect(self.show_settings)
        menu.addAction(settings_action)
        shortcuts_action = QAction('단축키 설정', self)
        shortcuts_action.triggered.connect(self.show_shortcut_settings)
        menu.addAction(shortcuts_action)
        menu.addSeparator()
        close_action = QAction('프로그램 종료', self)
        close_action.triggered.connect(self.close_program)
        menu.addAction(close_action)
        menu.exec_(self.mapToGlobal(pos))
    
    def show_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec_():
            self.preload_enabled = self.settings.get('preload_next', True)
            self.preload_count = max(0, min(10, int(self.settings.get('preload_count', 3))))
            self.apply_background_color()
            self.cache_manager.clear()
            if self.image_list:
                self.show_current_image()
    
    def show_shortcut_settings(self):
        dialog = ShortcutSettingsDialog(self.settings, self)
        if dialog.exec_():
            pass
    
    def _can_pan_image(self):
        if self.current_movie or not self.current_pixmap or self.fit_to_window:
            return False
        viewport = self.scroll_area.viewport().size()
        label_size = self.image_label.size()
        return label_size.width() > viewport.width() or label_size.height() > viewport.height()

    def _start_image_pan(self, global_pos):
        if not self._can_pan_image():
            return False
        self.panning = True
        self.pan_start_pos = QPoint(global_pos)
        self.pan_start_h = self.scroll_area.horizontalScrollBar().value()
        self.pan_start_v = self.scroll_area.verticalScrollBar().value()
        self.setCursor(Qt.ClosedHandCursor)
        return True

    def _move_image_pan(self, global_pos):
        if not self.panning or self.pan_start_pos is None:
            return False
        delta = QPoint(global_pos) - self.pan_start_pos
        self.scroll_area.horizontalScrollBar().setValue(self.pan_start_h - delta.x())
        self.scroll_area.verticalScrollBar().setValue(self.pan_start_v - delta.y())
        return True

    def _end_image_pan(self):
        if not self.panning:
            return False
        self.panning = False
        self.pan_start_pos = None
        self.setCursor(Qt.ArrowCursor)
        return True

    def _is_pan_target(self, widget):
        if widget is None:
            return False
        viewport = self.scroll_area.viewport()
        if widget is self.image_label or widget is viewport:
            return True
        try:
            return viewport.isAncestorOf(widget) or self.image_label.isAncestorOf(widget)
        except Exception:
            return False

    def eventFilter(self, obj, event):
        event_type = event.type()

        # 마우스 눌림 이벤트 처리
        if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            # 창 크기 조절 영역(테두리)이 아닌 경우 마우스 팬 시도
            local_pos = self.mapFromGlobal(event.globalPos())
            if not self.get_resize_region(local_pos) and self._can_pan_image():
                target = QApplication.widgetAt(event.globalPos())
                if self._is_pan_target(target):
                    if self._start_image_pan(event.globalPos()):
                        return True

        # 마우스 이동 이벤트 처리 (팬 중일 때)
        elif event_type == QEvent.MouseMove and self.panning:
            if self._move_image_pan(event.globalPos()):
                return True

        # 마우스 뗌 이벤트 처리
        elif event_type == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            if self.panning:
                self._end_image_pan()
                return True

        return super().eventFilter(obj, event)

    def wheelEvent(self, event: QWheelEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        if event.angleDelta().y() > 0:
            self.prev_image()
        else:
            self.next_image()
        event.accept()
    
    def mousePressEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        region = self.get_resize_region(event.pos())
        if event.button() == Qt.LeftButton and region:
            self.resizing = True
            self.resize_start_pos = event.globalPos()
            self.resize_start_size = self.size()
            self.resize_region = region
            event.accept()
            return
        if event.button() == Qt.LeftButton and not region:
            if self.isFullScreen() or self.panning:
                event.accept()
                return
            self.dragging = True
            self.drag_start_pos = event.globalPos()
            self.window_start_pos = self.pos()
            event.accept()
            return
        button_text = ''
        if event.button() == Qt.MiddleButton:
            button_text = 'Middle Click'
        elif event.button() == Qt.XButton1:
            button_text = 'XButton1'
        elif event.button() == Qt.XButton2:
            button_text = 'XButton2'
        if button_text:
            self.check_mouse_shortcut(button_text)
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        
        if self.resizing and self.resize_start_pos:
            delta = event.globalPos() - self.resize_start_pos
            new_w = self.resize_start_size.width()
            new_h = self.resize_start_size.height()
            if self.resize_region in ['left', 'topleft', 'bottomleft']:
                new_w = max(200, self.resize_start_size.width() - delta.x())
            elif self.resize_region in ['right', 'topright', 'bottomright']:
                new_w = max(200, self.resize_start_size.width() + delta.x())
            if self.resize_region in ['top', 'topleft', 'topright']:
                new_h = max(150, self.resize_start_size.height() - delta.y())
            elif self.resize_region in ['bottom', 'bottomleft', 'bottomright']:
                new_h = max(150, self.resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)
            event.accept()
            return
        if self.dragging and self.drag_start_pos and not self.isFullScreen():
            delta = event.globalPos() - self.drag_start_pos
            new_pos = self.window_start_pos + delta
            new_pos = self.snap_to_edge(new_pos)
            self.move(new_pos)
            event.accept()
            return
        self.update_cursor(event.pos())
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        if event.button() == Qt.LeftButton and self.resizing:
            self.resizing = False
            self.resize_start_pos = None
            self.resize_start_size = None
            self.resize_region = None
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            self.drag_start_pos = None
            self.window_start_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
    
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.check_mouse_shortcut('Left Double Click')
        elif event.button() == Qt.RightButton:
            self.dragging = False
            self.check_mouse_shortcut('Right Double Click')
        super().mouseDoubleClickEvent(event)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_to_window:
            self.update_image_display()
    
    def closeEvent(self, event: QCloseEvent):
        self.show_cursor()
        if self.isFullScreen():
            self.showNormal()
        if not self.isFullScreen():
            self.save_settings()
        self.stop_current_movie()
        self.slideshow.stop()
        self.cursor_hide_timer.stop()
        try:
            self.load_generation += 1
            self.loading_keys.clear()
            self.cache_manager.clear()
            self.current_pixmap = None
            self.original_pixmap = None
        except Exception:
            pass
        try:
            ImageLoader.shutdown_executor()
        except Exception:
            pass
        try:
            ZipHandler._thread_local.__dict__.clear()
        except Exception:
            pass
        super().closeEvent(event)

def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    icon_path = get_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    
    single_app = SingleApplication()
    if single_app.is_running():
        if len(sys.argv) > 1:
            single_app.send_message(sys.argv[1])
        sys.exit(0)
    single_app.start_server()
    viewer = ImageViewer()
    single_app.set_file_received_callback(viewer.load_path)
    if len(sys.argv) > 1:
        viewer.load_path(sys.argv[1])
    viewer.show()
    QTimer.singleShot(100, viewer.force_foreground)
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
