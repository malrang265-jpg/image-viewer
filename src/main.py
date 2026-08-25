import sys
import os
import json
import zipfile
import threading
from io import BytesIO
from collections import OrderedDict

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QDialog, QHBoxLayout, QComboBox, QCheckBox, QPushButton,
                            QColorDialog, QGroupBox, QFormLayout, QSpinBox,
                            QListWidget, QListWidgetItem, QShortcut)
from PyQt5.QtCore import Qt, QTimer, QObject, QByteArray, QSize
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QWheelEvent, QTransform,
                        QMovie, QKeyEvent, QCloseEvent, QMouseEvent)
from PIL import Image

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
                'actual_size': '0',
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
            # PIL로 이미지 로드
            with Image.open(filepath) as img:
                # RGB/RGBA 변환
                if img.mode == 'P':
                    img = img.convert('RGBA')
                elif img.mode == 'L':
                    img = img.convert('RGB')
                elif img.mode == 'CMYK':
                    img = img.convert('RGB')
                elif img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                
                # QImage 변환
                if img.mode == 'RGBA':
                    data = img.tobytes('raw', 'RGBA')
                    qimage = QImage(data, img.width, img.height, QImage.Format_RGBA8888)
                else:
                    data = img.tobytes('raw', 'RGB')
                    qimage = QImage(data, img.width, img.height, QImage.Format_RGB888)
                
                # QPixmap 변환
                pixmap = QPixmap.fromImage(qimage.copy())
                return pixmap
        except Exception as e:
            print(f"이미지 로드 실패: {filepath} - {e}")
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
                img = Image.open(BytesIO(data))
                
                if img.mode == 'P':
                    img = img.convert('RGBA')
                elif img.mode == 'L':
                    img = img.convert('RGB')
                elif img.mode == 'CMYK':
                    img = img.convert('RGB')
                elif img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                
                if img.mode == 'RGBA':
                    data_bytes = img.tobytes('raw', 'RGBA')
                    qimage = QImage(data_bytes, img.width, img.height, QImage.Format_RGBA8888)
                else:
                    data_bytes = img.tobytes('raw', 'RGB')
                    qimage = QImage(data_bytes, img.width, img.height, QImage.Format_RGB888)
                
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
            ('actual_size', '실제 크기'),
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
        
        # 마우스 이벤트 추적 활성화
        self.setMouseTracking(True)
    
    def load_shortcuts(self):
        actions = ['next_image', 'prev_image', 'toggle_fullscreen', 'close_program',
                  'show_image_list', 'zoom_in', 'zoom_out', 'actual_size',
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
        button.setText('키/마우스 버튼 누르세요...')
        button.setStyleSheet("background-color: #4a90d9; color: white; border: 1px solid #555; padding: 5px 10px;")
        self.grabKeyboard()
        self.grabMouse()
    
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
                self.capturing = False
                self.current_action = None
                self.releaseKeyboard()
                self.releaseMouse()
        
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
                self.capturing = False
                self.current_action = None
                self.releaseKeyboard()
                self.releaseMouse()
                return
        
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        if self.capturing and self.current_action:
            button = event.button()
            
            if button == Qt.LeftButton:
                self.shortcut_buttons[self.current_action].setText('Left Double Click')
                self.shortcut_buttons[self.current_action].setStyleSheet("")
                self.capturing = False
                self.current_action = None
                self.releaseKeyboard()
                self.releaseMouse()
                return
        
        super().mouseDoubleClickEvent(event)
    
    def cancel_capture(self):
        if self.current_action and self.current_action in self.shortcut_buttons:
            original = self.settings.get_shortcut(self.current_action, '없음')
            self.shortcut_buttons[self.current_action].setText(original)
            self.shortcut_buttons[self.current_action].setStyleSheet("")
        self.capturing = False
        self.current_action = None
        self.releaseKeyboard()
        self.releaseMouse()
    
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
            'actual_size': self.actual_size,
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
                # 마우스 클릭 단축키는 QShortcut으로 처리 불가
                if 'Click' in key:
                    continue
                
                try:
                    shortcut = QShortcut(QKeySequence(key), self)
                    shortcut.activated.connect(callback)
                    self.shortcut_objects[action_name] = shortcut
                except:
                    pass
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.load_path(path)
    
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
    
    def load_directory(self, directory):
        self.image_list = []
        self.current_zip = None
        
        try:
            for filename in sorted(os.listdir(directory)):
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
        
        # 선택한 파일의 인덱스 찾기
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
        if not self.image_list or self.current_index < 0 or self.current_index >= len(self.image_list):
            return
        
        self.stop_current_movie()
        
        current_file = self.image_list[self.current_index]
        pixmap = None
        
        try:
            if self.current_zip:
                if ImageLoader.is_animated(current_file):
                    movie = ZipHandler.load_animation_from_zip(self.current_zip, current_file)
                    if movie:
                        self.current_movie = movie
                        self.image_label.setMovie(movie)
                        movie.start()
                        self.update_image_display()
                        return
                else:
                    pixmap = ZipHandler.load_image_from_zip(self.current_zip, current_file)
            else:
                if ImageLoader.is_animated(current_file):
                    movie = ImageLoader.load_movie(current_file)
                    if movie:
                        self.current_movie = movie
                        self.image_label.setMovie(movie)
                        movie.start()
                        self.update_image_display()
                        return
                else:
                    cache_key = current_file
                    pixmap = self.cache_manager.get(cache_key)
                    
                    if pixmap is None:
                        quality = self.settings.get('zoom_quality', 'balanced')
                        pixmap = ImageLoader.load_pixmap(current_file, quality)
                        if pixmap:
                            self.cache_manager.put(cache_key, pixmap)
            
            if pixmap:
                if self.rotation_angle != 0:
                    transform = QTransform().rotate(self.rotation_angle)
                    pixmap = pixmap.transformed(transform, Qt.SmoothTransformation)
                
                self.current_pixmap = pixmap
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
    
    def update_image_display(self):
        # 애니메이션 크기 조정
        if self.current_movie:
            if self.fit_to_window:
                original_size = self.current_movie.currentPixmap().size()
                if original_size.width() > 0 and original_size.height() > 0:
                    scaled_size = original_size.scaled(self.scroll_area.size(), Qt.KeepAspectRatio)
                    self.current_movie.setScaledSize(scaled_size)
            else:
                original_size = self.current_movie.currentPixmap().size()
                if original_size.width() > 0 and original_size.height() > 0:
                    scaled_size = original_size * self.zoom_factor
                    self.current_movie.setScaledSize(scaled_size)
        
        # 일반 이미지 크기 조정
        if self.current_pixmap:
            if self.fit_to_window:
                scaled_pixmap = self.current_pixmap.scaled(
                    self.scroll_area.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled_pixmap)
            else:
                new_size = self.current_pixmap.size() * self.zoom_factor
                scaled_pixmap = self.current_pixmap.scaled(
                    new_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled_pixmap)
        
        self.image_label.adjustSize()
    
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
        
        zoom_in_action = QAction('확대', self)
        zoom_in_action.triggered.connect(self.zoom_in)
        menu.addAction(zoom_in_action)
        
        zoom_out_action = QAction('축소', self)
        zoom_out_action.triggered.connect(self.zoom_out)
        menu.addAction(zoom_out_action)
        
        actual_size_action = QAction('실제 크기', self)
        actual_size_action.triggered.connect(self.actual_size)
        menu.addAction(actual_size_action)
        
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
        # 휠 위로 = 이전 이미지, 휠 아래로 = 다음 이미지
        if event.angleDelta().y() > 0:
            self.prev_image()
        else:
            self.next_image()
        event.accept()
    
    def mousePressEvent(self, event: QMouseEvent):
        # 설정된 마우스 단축키 확인
        next_shortcut = self.settings.get_shortcut('next_image', '')
        prev_shortcut = self.settings.get_shortcut('prev_image', '')
        
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
        
        if button_text == next_shortcut:
            self.next_image()
        elif button_text == prev_shortcut:
            self.prev_image()
        
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        # 더블클릭 단축키 확인
        next_shortcut = self.settings.get_shortcut('next_image', '')
        prev_shortcut = self.settings.get_shortcut('prev_image', '')
        
        if event.button() == Qt.LeftButton:
            if next_shortcut == 'Left Double Click':
                self.next_image()
            elif prev_shortcut == 'Left Double Click':
                self.prev_image()
        
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
