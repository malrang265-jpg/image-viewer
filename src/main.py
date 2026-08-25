import sys
import os
import json
import zipfile
import threading
import re
from io import BytesIO
from collections import OrderedDict

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QDialog, QHBoxLayout, QComboBox, QCheckBox, QPushButton,
                            QColorDialog, QGroupBox, QFormLayout, QSpinBox,
                            QListWidget, QListWidgetItem, QShortcut, QMessageBox)
from PyQt5.QtCore import Qt, QTimer, QObject, QByteArray, QSize
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QWheelEvent, QTransform,
                        QMovie, QKeyEvent, QCloseEvent, QMouseEvent)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer
from PIL import Image

class SingleApplication:
    def __init__(self, app_name="ImageViewerApp"):
        self.app_name = app_name
        self.socket = QLocalSocket()
        self.server = None
    
    def is_running(self):
        self.socket.connectToServer(self.app_name)
        if self.socket.waitForConnected(100):
            return True
        return False
    
    def start_server(self):
        self.server = QLocalServer()
        self.server.listen(self.app_name)
    
    def send_message(self, message):
        if self.socket.state() == QLocalSocket.ConnectedState:
            self.socket.write(message.encode())
            self.socket.flush()

class Settings:
    def __init__(self):
        self.settings_file = os.path.join(os.path.expanduser('~'), '.image_viewer_settings.json')
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
            'slideshow_interval': 3,
            'cache_size': 50,
            'preload_next': True,
            'shortcuts': {
                'next_image': 'Right',
                'prev_image': 'Left',
                'zoom_in': 'Up',
                'zoom_out': 'Down',
                'toggle_actual_size': '0',
                'toggle_fullscreen': 'F11',
                'close_program': 'Ctrl+Q',
                'show_image_list': 'Tab',
                'delete_image': 'Delete',
                'open_file': 'Ctrl+O',
                'slideshow': 'S',
                'rotate_right': 'R',
                'rotate_left': 'L'
            }
        }
    
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    def set(self, key, value):
        self.data[key] = value
        self.save()
    
    def get_shortcut(self, action, default=''):
        shortcuts = self.data.get('shortcuts', {})
        value = shortcuts.get(action, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value
    
    def set_shortcut(self, action, key_sequence):
        if 'shortcuts' not in self.data:
            self.data['shortcuts'] = {}
        self.data['shortcuts'][action] = key_sequence
        self.save()

class ImageLoader:
    SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    ANIMATED_FORMATS = {'.gif', '.webp'}
    
    @staticmethod
    def is_supported(filename):
        ext = os.path.splitext(filename)[1].lower()
        return ext in ImageLoader.SUPPORTED_FORMATS
    
    @staticmethod
    def is_animated(filename):
        ext = os.path.splitext(filename)[1].lower()
        return ext in ImageLoader.ANIMATED_FORMATS
    
    @staticmethod
    def load_pixmap(filepath, quality='balanced'):
        try:
            pixmap = QPixmap(filepath)
            if not pixmap.isNull():
                return pixmap
            
            with Image.open(filepath) as img:
                img = img.convert('RGBA')
                data = img.tobytes('raw', 'RGBA')
                qimage = QImage(data, img.width, img.height, QImage.Format_RGBA8888)
                return QPixmap.fromImage(qimage.copy())
        except:
            return None
    
    @staticmethod
    def load_movie(filepath):
        try:
            movie = QMovie(filepath)
            if movie.isValid():
                return movie
        except:
            pass
        return None
    
    @staticmethod
    def load_gif_first_frame(filepath):
        """GIF 첫 프레임 로드"""
        try:
            movie = QMovie(filepath)
            if movie.isValid():
                movie.jumpToFrame(0)
                return movie.currentPixmap()
        except:
            pass
        return None

class CacheManager:
    def __init__(self, max_size=50):
        self.max_size = max_size
        self.cache = OrderedDict()
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            if key in self.cache:
                value = self.cache.pop(key)
                self.cache[key] = value
                return value
            return None
    
    def put(self, key, value):
        with self.lock:
            if key in self.cache:
                del self.cache[key]
            elif len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)
            self.cache[key] = value
    
    def clear(self):
        with self.lock:
            self.cache.clear()

class ZipHandler:
    @staticmethod
    def is_zip(filename):
        return filename.lower().endswith('.zip')
    
    @staticmethod
    def list_images(zip_path):
        images = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for filename in zf.namelist():
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
                        images.append(filename)
        except:
            pass
        return sorted(images)
    
    @staticmethod
    def load_image_from_zip(zip_path, filename):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                data = zf.read(filename)
                
                # GIF인 경우 첫 프레임만
                ext = os.path.splitext(filename)[1].lower()
                if ext == '.gif':
                    temp_file = os.path.join(os.path.expanduser('~'), '.temp_gif')
                    with open(temp_file, 'wb') as f:
                        f.write(data)
                    movie = QMovie(temp_file)
                    if movie.isValid():
                        movie.jumpToFrame(0)
                        pixmap = movie.currentPixmap()
                        movie.stop()
                        return pixmap
                
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    return pixmap
                
                img = Image.open(BytesIO(data))
                img = img.convert('RGBA')
                data_bytes = img.tobytes('raw', 'RGBA')
                qimage = QImage(data_bytes, img.width, img.height, QImage.Format_RGBA8888)
                return QPixmap.fromImage(qimage.copy())
        except:
            return None
    
    @staticmethod
    def load_animation_from_zip(zip_path, filename):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                data = zf.read(filename)
                temp_file = os.path.join(os.path.expanduser('~'), '.temp_animation')
                with open(temp_file, 'wb') as f:
                    f.write(data)
                movie = QMovie(temp_file)
                if movie.isValid():
                    return movie
        except:
            pass
        return None

class ImageListDialog(QDialog):
    def __init__(self, image_list, current_index, parent=None):
        super().__init__(parent)
        self.image_list = image_list
        self.current_index = current_index
        self.selected_index = current_index
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle('이미지 목록')
        self.setModal(True)
        self.setMinimumSize(300, 400)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QListWidget { background-color: #3c3c3c; color: white; border: 1px solid #555; }
            QListWidget::item:selected { background-color: #4a90d9; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px; }
            QPushButton:hover { background-color: #4c4c4c; }
        """)
        
        layout = QVBoxLayout(self)
        
        self.list_widget = QListWidget()
        for i, image_path in enumerate(self.image_list):
            display_name = os.path.basename(image_path)
            item = QListWidgetItem(display_name)
            item.setData(Qt.UserRole, i)
            self.list_widget.addItem(item)
        
        self.list_widget.setCurrentRow(self.current_index)
        self.list_widget.itemDoubleClicked.connect(self.on_double_click)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        layout.addWidget(self.list_widget)
        
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
    
    def on_double_click(self, item):
        self.selected_index = item.data(Qt.UserRole)
        self.accept()
    
    def get_selected_index(self):
        return self.selected_index

class ShortcutSettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.shortcut_buttons = {}
        self.capturing = False
        self.current_action = None
        self.init_ui()
        self.load_shortcuts()
    
    def init_ui(self):
        self.setWindowTitle('단축키 설정')
        self.setModal(True)
        self.setMinimumWidth(400)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QLabel { color: white; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px 10px; }
            QPushButton:hover { background-color: #4c4c4c; }
            QGroupBox { color: white; border: 1px solid #555; margin-top: 10px; }
        """)
        
        layout = QVBoxLayout(self)
        
        actions = [
            ('next_image', '다음 이미지'),
            ('prev_image', '이전 이미지'),
            ('toggle_fullscreen', '전체화면 토글'),
            ('close_program', '프로그램 닫기'),
            ('show_image_list', '이미지 목록 표시'),
            ('zoom_in', '확대'),
            ('zoom_out', '축소'),
            ('toggle_actual_size', '실제 크기/창 크기 토글'),
            ('delete_image', '삭제'),
            ('open_file', '열기'),
            ('slideshow', '슬라이드쇼'),
            ('rotate_right', '오른쪽 회전'),
            ('rotate_left', '왼쪽 회전'),
        ]
        
        for action_key, action_name in actions:
            group = QGroupBox(action_name)
            group_layout = QHBoxLayout()
            
            button = QPushButton('클릭하여 설정')
            button.setMinimumWidth(150)
            button.clicked.connect(lambda checked, k=action_key, b=button: self.start_capture(k, b))
            self.shortcut_buttons[action_key] = button
            group_layout.addWidget(button)
            
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
            shortcut = self.settings.get_shortcut(action, '없음')
            if action in self.shortcut_buttons:
                self.shortcut_buttons[action].setText(shortcut)
    
    def start_capture(self, action_key, button):
        if self.capturing:
            return
        
        self.capturing = True
        self.current_action = action_key
        button.setText('입력 대기 중...')
        button.setStyleSheet("background-color: #4a90d9; color: white; border: 1px solid #555; padding: 5px 10px;")
        self.grabKeyboard()
        self.grabMouse()
        self.setFocus()
    
    def keyPressEvent(self, event):
        if self.capturing and self.current_action:
            key = event.key()
            modifiers = event.modifiers()
            
            if key == Qt.Key_Escape:
                self.cancel_capture()
                return
            
            key_sequence = QKeySequence(modifiers | key).toString()
            
            if key_sequence and self.current_action in self.shortcut_buttons:
                self.shortcut_buttons[self.current_action].setText(key_sequence)
                self.shortcut_buttons[self.current_action].setStyleSheet("")
                self.stop_capture()
        
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
                self.shortcut_buttons[self.current_action].setText(button_text)
                self.shortcut_buttons[self.current_action].setStyleSheet("")
                self.stop_capture()
                return
        
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        if self.capturing and self.current_action:
            if event.button() == Qt.LeftButton:
                self.shortcut_buttons[self.current_action].setText('Left Double Click')
                self.shortcut_buttons[self.current_action].setStyleSheet("")
                self.stop_capture()
                return
        
        super().mouseDoubleClickEvent(event)
    
    def stop_capture(self):
        self.capturing = False
        self.current_action = None
        self.releaseKeyboard()
        self.releaseMouse()
    
    def cancel_capture(self):
        if self.current_action and self.current_action in self.shortcut_buttons:
            original = self.settings.get_shortcut(self.current_action, '없음')
            self.shortcut_buttons[self.current_action].setText(original)
            self.shortcut_buttons[self.current_action].setStyleSheet("")
        self.stop_capture()
    
    def reset_defaults(self):
        defaults = self.settings.default_settings()['shortcuts']
        for action, shortcut in defaults.items():
            if action in self.shortcut_buttons:
                self.shortcut_buttons[action].setText(shortcut)
    
    def save_shortcuts(self):
        for action, button in self.shortcut_buttons.items():
            self.settings.set_shortcut(action, button.text())
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
        self.setMinimumWidth(400)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: #ffffff; }
            QGroupBox { color: #ffffff; border: 1px solid #555; margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLabel { color: #ffffff; }
            QCheckBox { color: #ffffff; }
            QComboBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 3px; }
            QSpinBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 3px; }
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
        
        display_group.setLayout(display_layout)
        layout.addWidget(display_group)
        
        performance_group = QGroupBox('성능')
        performance_layout = QFormLayout()
        
        self.cache_size = QSpinBox()
        self.cache_size.setRange(10, 200)
        self.cache_size.setSuffix(' 개')
        performance_layout.addRow('캐시 크기:', self.cache_size)
        
        self.preload_next = QCheckBox('다음/이전 이미지 미리 로드')
        performance_layout.addRow('', self.preload_next)
        
        performance_group.setLayout(performance_layout)
        layout.addWidget(performance_group)
        
        slideshow_group = QGroupBox('슬라이드쇼')
        slideshow_layout = QFormLayout()
        
        self.slideshow_interval = QSpinBox()
        self.slideshow_interval.setRange(1, 60)
        self.slideshow_interval.setSuffix(' 초')
        slideshow_layout.addRow('간격:', self.slideshow_interval)
        
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
    
    def load_settings(self):
        quality = self.settings.get('zoom_quality', 'balanced')
        index = self.zoom_quality.findData(quality)
        if index >= 0:
            self.zoom_quality.setCurrentIndex(index)
        self.show_filename.setChecked(self.settings.get('show_filename', False))
        self.fit_to_window.setChecked(self.settings.get('fit_to_window', True))
        self.cache_size.setValue(self.settings.get('cache_size', 50))
        self.preload_next.setChecked(self.settings.get('preload_next', True))
        self.slideshow_interval.setValue(self.settings.get('slideshow_interval', 3))
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
        self.settings.set('cache_size', self.cache_size.value())
        self.settings.set('preload_next', self.preload_next.isChecked())
        self.settings.set('slideshow_interval', self.slideshow_interval.value())
        self.settings.set('background_color', self.current_color)
        self.accept()

class ImageViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.cache_manager = CacheManager(self.settings.get('cache_size', 50))
        self.slideshow = QTimer()
        self.slideshow.timeout.connect(self.next_image)
        self.slideshow_playing = False
        self.current_index = 0
        self.image_list = []
        self.current_zip = None
        self.zoom_factor = 1.0
        self.fit_to_window = True
        self.rotation_angle = 0
        self.shortcut_objects = {}
        self.current_movie = None
        self.current_pixmap = None
        self.original_pixmap = None  # 원본 이미지 저장
        self.is_loading = False
        
        self.init_ui()
        self.load_settings()
        self.setup_shortcuts()
        
        self.slideshow.setInterval(self.settings.get('slideshow_interval', 3) * 1000)
    
    def init_ui(self):
        self.setWindowTitle('이미지 뷰어')
        self.setMinimumSize(400, 300)
        self.setAcceptDrops(True)
        
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
        
        bg_color = self.settings.get('background_color', '#2b2b2b')
        self.setStyleSheet(f"QMainWindow {{ background-color: {bg_color}; }}")
        
        self.filename_label = QLabel('')
        self.filename_label.setAlignment(Qt.AlignCenter)
        self.filename_label.setStyleSheet("color: white; background-color: rgba(0,0,0,0.7); padding: 5px; border-radius: 3px;")
        self.filename_label.hide()
        
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
    
    def setup_shortcuts(self):
        for shortcut in self.shortcut_objects.values():
            shortcut.setEnabled(False)
        self.shortcut_objects.clear()
        
        shortcut_actions = {
            'next_image': self.next_image,
            'prev_image': self.prev_image,
            'zoom_in': self.zoom_in,
            'zoom_out': self.zoom_out,
            'toggle_actual_size': self.toggle_actual_size,
            'toggle_fullscreen': self.toggle_fullscreen,
            'close_program': self.close,
            'show_image_list': self.show_image_list_dialog,
            'delete_image': self.delete_image,
            'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
            'rotate_right': self.rotate_right,
            'rotate_left': self.rotate_left,
        }
        
        for action_name, callback in shortcut_actions.items():
            key = self.settings.get_shortcut(action_name, '')
            if key and key != '없음' and key != '':
                if 'Click' in key:
                    continue
                
                try:
                    shortcut = QShortcut(QKeySequence(key), self)
                    shortcut.activated.connect(callback)
                    self.shortcut_objects[action_name] = shortcut
                except:
                    pass
    
    def check_mouse_shortcut(self, button_text):
        actions = {
            'next_image': self.next_image,
            'prev_image': self.prev_image,
            'toggle_fullscreen': self.toggle_fullscreen,
            'close_program': self.close_program,
            'show_image_list': self.show_image_list_dialog,
            'zoom_in': self.zoom_in,
            'zoom_out': self.zoom_out,
            'toggle_actual_size': self.toggle_actual_size,
            'delete_image': self.delete_image,
            'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
            'rotate_right': self.rotate_right,
            'rotate_left': self.rotate_left,
        }
        
        for action_name, callback in actions.items():
            shortcut = self.settings.get_shortcut(action_name, '')
            if shortcut == button_text:
                callback()
                return True
        
        return False
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.load_path(path)
            event.acceptProposedAction()
    
    def load_settings(self):
        geometry = self.settings.get('window_geometry')
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
    
    def save_settings(self):
        geometry = self.saveGeometry().toBase64().data().decode()
        self.settings.set('window_geometry', geometry)
    
    def load_path(self, path):
        if os.path.isdir(path):
            self.load_directory(path)
        elif os.path.isfile(path):
            if ZipHandler.is_zip(path):
                self.load_zip(path)
            else:
                self.load_single_file(path)
    
    def natural_sort_key(self, s):
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]
    
    def load_directory(self, directory):
        self.image_list = []
        self.current_zip = None
        
        try:
            files = os.listdir(directory)
            files.sort(key=self.natural_sort_key)
            
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
        self.current_zip = zip_path
        self.image_list = ZipHandler.list_images(zip_path)
        
        if self.image_list:
            self.current_index = 0
            self.show_current_image()
        else:
            self.image_label.clear()
    
    def stop_current_movie(self):
        if self.current_movie:
            self.current_movie.stop()
            self.current_movie = None
    
    def show_current_image(self):
        if self.is_loading:
            return
        
        if not self.image_list or self.current_index < 0 or self.current_index >= len(self.image_list):
            return
        
        self.is_loading = True
        self.stop_current_movie()
        
        current_file = self.image_list[self.current_index]
        pixmap = None
        
        try:
            if self.current_zip:
                # ZIP 파일의 GIF는 첫 프레임만 표시
                pixmap = ZipHandler.load_image_from_zip(self.current_zip, current_file)
            else:
                if ImageLoader.is_animated(current_file):
                    # GIF/WebP 애니메이션
                    movie = ImageLoader.load_movie(current_file)
                    if movie:
                        self.current_movie = movie
                        self.image_label.setMovie(movie)
                        movie.start()
                        self.original_pixmap = movie.currentPixmap()
                        self.update_image_display()
                        self.is_loading = False
                        return
                else:
                    cache_key = current_file
                    pixmap = self.cache_manager.get(cache_key)
                    
                    if pixmap is None:
                        pixmap = ImageLoader.load_pixmap(current_file)
                        if pixmap:
                            self.cache_manager.put(cache_key, pixmap)
            
            if pixmap:
                if self.rotation_angle != 0:
                    transform = QTransform().rotate(self.rotation_angle)
                    pixmap = pixmap.transformed(transform, Qt.SmoothTransformation)
                
                self.current_pixmap = pixmap
                self.original_pixmap = pixmap  # 원본 저장
                self.image_label.setPixmap(pixmap)
                self.update_image_display()
                
                if self.settings.get('show_filename', False):
                    display_name = os.path.basename(current_file) if not self.current_zip else current_file
                    self.filename_label.setText(display_name)
                    self.filename_label.show()
                    self.filename_label.adjustSize()
                    self.filename_label.move(10, 10)
                else:
                    self.filename_label.hide()
        except Exception as e:
            print(f"이미지 로드 오류: {e}")
        finally:
            self.is_loading = False
    
    def update_image_display(self):
        # 원본 이미지 기준으로 크기 조정
        base_pixmap = None
        
        if self.current_movie:
            base_pixmap = self.current_movie.currentPixmap()
        elif self.original_pixmap:
            base_pixmap = self.original_pixmap
        elif self.current_pixmap:
            base_pixmap = self.current_pixmap
        
        if not base_pixmap or base_pixmap.isNull():
            return
        
        if self.fit_to_window:
            # 창 크기에 맞추기
            if self.current_movie:
                scaled_size = base_pixmap.size().scaled(self.scroll_area.size(), Qt.KeepAspectRatio)
                self.current_movie.setScaledSize(scaled_size)
            else:
                scaled_pixmap = base_pixmap.scaled(
                    self.scroll_area.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled_pixmap)
        else:
            # 실제 크기
            if self.current_movie:
                self.current_movie.setScaledSize(base_pixmap.size())
            else:
                self.image_label.setPixmap(base_pixmap)
        
        self.image_label.adjustSize()
    
    def toggle_actual_size(self):
        """실제 크기/창 크기 토글"""
        self.fit_to_window = not self.fit_to_window
        if self.fit_to_window:
            self.zoom_factor = 1.0
        self.update_image_display()
    
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
    
    def zoom_in(self):
        self.fit_to_window = False
        self.zoom_factor *= 1.2
        self.update_image_display()
    
    def zoom_out(self):
        self.fit_to_window = False
        self.zoom_factor /= 1.2
        self.update_image_display()
    
    def actual_size(self):
        self.fit_to_window = False
        self.zoom_factor = 1.0
        self.update_image_display()
    
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
    
    def close_program(self):
        self.close()
    
    def show_image_list_dialog(self):
        if not self.image_list:
            return
        
        dialog = ImageListDialog(self.image_list, self.current_index, self)
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
            self.slideshow.stop()
            self.slideshow_playing = False
        else:
            self.slideshow.start()
            self.slideshow_playing = True
    
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
        
        prev_action = QAction('이전 이미지', self)
        prev_action.triggered.connect(self.prev_image)
        menu.addAction(prev_action)
        
        next_action = QAction('다음 이미지', self)
        next_action.triggered.connect(self.next_image)
        menu.addAction(next_action)
        
        menu.addSeparator()
        
        toggle_size_action = QAction('실제 크기/창 크기 토글', self)
        toggle_size_action.triggered.connect(self.toggle_actual_size)
        menu.addAction(toggle_size_action)
        
        zoom_in_action = QAction('확대', self)
        zoom_in_action.triggered.connect(self.zoom_in)
        menu.addAction(zoom_in_action)
        
        zoom_out_action = QAction('축소', self)
        zoom_out_action.triggered.connect(self.zoom_out)
        menu.addAction(zoom_out_action)
        
        menu.addSeparator()
        
        rotate_right_action = QAction('오른쪽으로 회전', self)
        rotate_right_action.triggered.connect(self.rotate_right)
        menu.addAction(rotate_right_action)
        
        rotate_left_action = QAction('왼쪽으로 회전', self)
        rotate_left_action.triggered.connect(self.rotate_left)
        menu.addAction(rotate_left_action)
        
        menu.addSeparator()
        
        slideshow_action = QAction('슬라이드쇼', self)
        slideshow_action.triggered.connect(self.toggle_slideshow)
        menu.addAction(slideshow_action)
        
        image_list_action = QAction('이미지 목록', self)
        image_list_action.triggered.connect(self.show_image_list_dialog)
        menu.addAction(image_list_action)
        
        menu.addSeparator()
        
        open_action = QAction('열기...', self)
        open_action.triggered.connect(self.open_file)
        menu.addAction(open_action)
        
        delete_action = QAction('삭제', self)
        delete_action.triggered.connect(self.delete_image)
        menu.addAction(delete_action)
        
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
            self.apply_settings()
    
    def show_shortcut_settings(self):
        dialog = ShortcutSettingsDialog(self.settings, self)
        if dialog.exec_():
            self.setup_shortcuts()
    
    def apply_settings(self):
        bg_color = self.settings.get('background_color', '#2b2b2b')
        self.setStyleSheet(f"QMainWindow {{ background-color: {bg_color}; }}")
        cache_size = self.settings.get('cache_size', 50)
        self.cache_manager = CacheManager(cache_size)
        interval = self.settings.get('slideshow_interval', 3)
        self.slideshow.setInterval(interval * 1000)
        if self.image_list:
            self.show_current_image()
    
    def wheelEvent(self, event: QWheelEvent):
        if event.angleDelta().y() > 0:
            self.prev_image()
        else:
            self.next_image()
        event.accept()
    
    def mousePressEvent(self, event: QMouseEvent):
        button_text = ''
        if event.button() == Qt.LeftButton:
            button_text = 'Left Click'
        elif event.button() == Qt.RightButton:
            button_text = 'Right Click'
        elif event.button() == Qt.MiddleButton:
            button_text = 'Middle Click'
        elif event.button() == Qt.XButton1:
            button_text = 'XButton1'
        elif event.button() == Qt.XButton2:
            button_text = 'XButton2'
        
        if button_text:
            self.check_mouse_shortcut(button_text)
        
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.check_mouse_shortcut('Left Double Click')
        
        super().mouseDoubleClickEvent(event)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_to_window:
            self.update_image_display()
    
    def closeEvent(self, event: QCloseEvent):
        self.save_settings()
        self.stop_current_movie()
        self.slideshow.stop()
        super().closeEvent(event)

def main():
    single_app = SingleApplication()
    if single_app.is_running():
        if len(sys.argv) > 1:
            single_app.send_message(sys.argv[1])
        sys.exit(0)
    
    single_app.start_server()
    
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    viewer = ImageViewer()
    if len(sys.argv) > 1:
        viewer.load_path(sys.argv[1])
    viewer.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
