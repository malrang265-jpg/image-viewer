import sys
import os
import json
import zipfile
import threading
from io import BytesIO
from collections import OrderedDict

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QProgressBar, QDialog, QHBoxLayout, QComboBox,
                            QCheckBox, QPushButton, QColorDialog, QGroupBox,
                            QFormLayout, QSpinBox, QTableWidget, QTableWidgetItem,
                            QHeaderView)
from PyQt5.QtCore import Qt, QTimer, QObject
from PyQt5.QtGui import QImage, QPixmap, QKeySequence, QWheelEvent, QTransform
from PyQt5.QtWidgets import QShortcut
from PIL import Image

# Settings class
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
            'zoom_quality': 'speed',
            'wheel_action': 'navigate',
            'show_filename': True,
            'background_color': '#1a1a1a',
            'fit_to_window': True,
            'slideshow_interval': 3,
            'cache_size': 50,
            'preload_next': True,
            'shortcuts': {
                'next_image': ['Right'],
                'prev_image': ['Left'],
                'zoom_in': ['Up'],
                'zoom_out': ['Down'],
                'actual_size': ['0'],
                'toggle_fullscreen': ['F', 'F11'],
                'delete_image': ['Delete'],
                'open_file': ['O', 'Ctrl+O'],
                'slideshow': ['S'],
                'rotate_right': ['R'],
                'rotate_left': ['L'],
                'exit_fullscreen': ['Escape'],
                'mouse_next': 'XButton1',
                'mouse_prev': 'XButton2'
            }
        }
    
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    def set(self, key, value):
        self.data[key] = value
        self.save()

# ImageLoader class
class ImageLoader:
    SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    
    @staticmethod
    def is_supported(filename):
        ext = os.path.splitext(filename)[1].lower()
        return ext in ImageLoader.SUPPORTED_FORMATS
    
    @staticmethod
    def load_pixmap(filepath, quality='speed'):
        try:
            with Image.open(filepath) as img:
                if img.format == 'GIF':
                    img.seek(0)
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                data = img.tobytes('raw', img.mode)
                qimage = QImage(data, img.width, img.height, 
                               QImage.Format_RGBA8888 if img.mode == 'RGBA' else QImage.Format_RGB888)
                return QPixmap.fromImage(qimage.copy())
        except:
            return None
    
    @staticmethod
    def load_pixmap_fast(filepath):
        try:
            with Image.open(filepath) as img:
                if img.format == 'GIF':
                    img.seek(0)
                max_size = 2048
                if img.width > max_size or img.height > max_size:
                    ratio = min(max_size / img.width, max_size / img.height)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                data = img.tobytes('raw', img.mode)
                qimage = QImage(data, img.width, img.height, 
                               QImage.Format_RGBA8888 if img.mode == 'RGBA' else QImage.Format_RGB888)
                return QPixmap.fromImage(qimage.copy())
        except:
            return None

# CacheManager class
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

# ZipHandler class
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
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                data_bytes = img.tobytes('raw', img.mode)
                qimage = QImage(data_bytes, img.width, img.height,
                              QImage.Format_RGBA8888 if img.mode == 'RGBA' else QImage.Format_RGB888)
                return QPixmap.fromImage(qimage.copy())
        except:
            return None
    
    @staticmethod
    def load_image_from_zip_fast(zip_path, filename):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                data = zf.read(filename)
                img = Image.open(BytesIO(data))
                max_size = 2048
                if img.width > max_size or img.height > max_size:
                    ratio = min(max_size / img.width, max_size / img.height)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                data_bytes = img.tobytes('raw', img.mode)
                qimage = QImage(data_bytes, img.width, img.height,
                              QImage.Format_RGBA8888 if img.mode == 'RGBA' else QImage.Format_RGB888)
                return QPixmap.fromImage(qimage.copy())
        except:
            return None

# SlideshowManager class
class SlideshowManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer()
        self.timer.timeout.connect(self.next_slide)
        self.is_playing = False
        self.interval = 3000
        self.current_callback = None
    
    def set_interval(self, seconds):
        self.interval = seconds * 1000
        if self.is_playing:
            self.timer.setInterval(self.interval)
    
    def set_callback(self, callback):
        self.current_callback = callback
    
    def start(self):
        if self.current_callback:
            self.timer.start(self.interval)
            self.is_playing = True
    
    def stop(self):
        self.timer.stop()
        self.is_playing = False
    
    def toggle(self):
        if self.is_playing:
            self.stop()
        else:
            self.start()
    
    def next_slide(self):
        if self.current_callback:
            self.current_callback()
    
    def is_active(self):
        return self.is_playing

# SettingsDialog class
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
        
        mouse_group = QGroupBox('마우스')
        mouse_layout = QFormLayout()
        
        self.wheel_action = QComboBox()
        self.wheel_action.addItem('이미지 전환', 'navigate')
        self.wheel_action.addItem('확대/축소', 'zoom')
        mouse_layout.addRow('휠 동작:', self.wheel_action)
        
        mouse_group.setLayout(mouse_layout)
        layout.addWidget(mouse_group)
        
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
        quality = self.settings.get('zoom_quality', 'speed')
        index = self.zoom_quality.findData(quality)
        if index >= 0:
            self.zoom_quality.setCurrentIndex(index)
        self.show_filename.setChecked(self.settings.get('show_filename', True))
        self.fit_to_window.setChecked(self.settings.get('fit_to_window', True))
        wheel_action = self.settings.get('wheel_action', 'navigate')
        index = self.wheel_action.findData(wheel_action)
        if index >= 0:
            self.wheel_action.setCurrentIndex(index)
        self.cache_size.setValue(self.settings.get('cache_size', 50))
        self.preload_next.setChecked(self.settings.get('preload_next', True))
        self.slideshow_interval.setValue(self.settings.get('slideshow_interval', 3))
        self.current_color = self.settings.get('background_color', '#1a1a1a')
        self.update_color_button()
    
    def choose_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.current_color = color.name()
            self.update_color_button()
    
    def update_color_button(self):
        self.color_button.setStyleSheet(f"background-color: {self.current_color};")
        self.color_button.setText(self.current_color)
    
    def save_settings(self):
        self.settings.set('zoom_quality', self.zoom_quality.currentData())
        self.settings.set('show_filename', self.show_filename.isChecked())
        self.settings.set('fit_to_window', self.fit_to_window.isChecked())
        self.settings.set('wheel_action', self.wheel_action.currentData())
        self.settings.set('cache_size', self.cache_size.value())
        self.settings.set('preload_next', self.preload_next.isChecked())
        self.settings.set('slideshow_interval', self.slideshow_interval.value())
        self.settings.set('background_color', self.current_color)
        self.accept()

# ImageViewer class
class ImageViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.cache_manager = CacheManager(self.settings.get('cache_size', 50))
        self.slideshow = SlideshowManager(self)
        self.current_index = 0
        self.image_list = []
        self.current_zip = None
        self.zoom_factor = 1.0
        self.fit_to_window = True
        self.rotation_angle = 0
        self.shortcut_objects = {}
        
        self.init_ui()
        self.load_settings()
        self.setup_shortcuts()
        
        self.slideshow.set_callback(self.next_image)
        self.slideshow.set_interval(self.settings.get('slideshow_interval', 3))
    
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
        self.scroll_area.setWidget(self.image_label)
        
        bg_color = self.settings.get('background_color', '#1a1a1a')
        self.setStyleSheet(f"background-color: {bg_color};")
        
        self.filename_label = QLabel('')
        self.filename_label.setAlignment(Qt.AlignCenter)
        self.filename_label.setStyleSheet("color: white; background-color: rgba(0,0,0,0.7); padding: 5px;")
        self.filename_label.hide()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(300)
        self.progress_bar.hide()
        
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
    
    def setup_shortcuts(self):
        shortcuts_config = self.settings.get('shortcuts', {})
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
            'delete_image': self.delete_image,
            'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
            'rotate_right': self.rotate_right,
            'rotate_left': self.rotate_left,
            'exit_fullscreen': self.exit_fullscreen,
        }
        
        for action_name, callback in shortcut_actions.items():
            keys = shortcuts_config.get(action_name, [])
            if isinstance(keys, str):
                keys = [keys]
            for key in keys:
                if key:
                    shortcut = QShortcut(QKeySequence(key), self)
                    shortcut.activated.connect(callback)
                    self.shortcut_objects[f"{action_name}_{key}"] = shortcut
    
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
            from PyQt5.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
    
    def save_settings(self):
        from PyQt5.QtCore import QByteArray
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
        self.show_progress_bar(True)
        self.image_list = []
        try:
            for filename in sorted(os.listdir(directory)):
                if ImageLoader.is_supported(filename):
                    self.image_list.append(os.path.join(directory, filename))
            if self.image_list:
                self.current_index = 0
                self.show_current_image()
                self.preload_adjacent_images()
        except Exception as e:
            print(f"폴더 로드 실패: {e}")
        finally:
            self.show_progress_bar(False)
    
    def load_single_file(self, filepath):
        directory = os.path.dirname(filepath)
        self.load_directory(directory)
        try:
            self.current_index = self.image_list.index(os.path.abspath(filepath))
            self.show_current_image()
        except ValueError:
            pass
    
    def load_zip(self, zip_path):
        self.show_progress_bar(True)
        self.current_zip = zip_path
        self.image_list = ZipHandler.list_images(zip_path)
        if self.image_list:
            self.current_index = 0
            self.show_current_image()
        self.show_progress_bar(False)
    
    def show_current_image(self):
        if not self.image_list or self.current_index < 0 or self.current_index >= len(self.image_list):
            return
        
        current_file = self.image_list[self.current_index]
        cache_key = f"{self.current_zip}_{current_file}" if self.current_zip else current_file
        pixmap = self.cache_manager.get(cache_key)
        
        if pixmap is None:
            quality = self.settings.get('zoom_quality', 'speed')
            if self.current_zip:
                if quality == 'speed':
                    pixmap = ZipHandler.load_image_from_zip_fast(self.current_zip, current_file)
                else:
                    pixmap = ZipHandler.load_image_from_zip(self.current_zip, current_file)
                display_name = f"{os.path.basename(self.current_zip)} - {current_file}"
            else:
                if quality == 'speed':
                    pixmap = ImageLoader.load_pixmap_fast(current_file)
                else:
                    pixmap = ImageLoader.load_pixmap(current_file, quality)
                display_name = os.path.basename(current_file)
            if pixmap:
                self.cache_manager.put(cache_key, pixmap)
        else:
            display_name = os.path.basename(current_file) if not self.current_zip else f"{os.path.basename(self.current_zip)} - {current_file}"
        
        if pixmap:
            if self.rotation_angle != 0:
                transform = QTransform().rotate(self.rotation_angle)
                pixmap = pixmap.transformed(transform, Qt.SmoothTransformation)
            self.current_pixmap = pixmap
            self.update_image_display()
            if self.settings.get('show_filename', True):
                self.filename_label.setText(display_name)
                self.filename_label.show()
                self.filename_label.adjustSize()
                self.filename_label.move(10, 10)
            self.preload_adjacent_images()
    
    def preload_adjacent_images(self):
        if not self.settings.get('preload_next', True):
            return
        preload_indices = []
        if self.current_index < len(self.image_list) - 1:
            preload_indices.append(self.current_index + 1)
        if self.current_index > 0:
            preload_indices.append(self.current_index - 1)
        
        for idx in preload_indices:
            file_path = self.image_list[idx]
            cache_key = f"{self.current_zip}_{file_path}" if self.current_zip else file_path
            if self.cache_manager.get(cache_key) is None:
                def preload_worker(key=file_path):
                    if self.current_zip:
                        pixmap = ZipHandler.load_image_from_zip_fast(self.current_zip, key)
                    else:
                        pixmap = ImageLoader.load_pixmap_fast(key)
                    if pixmap:
                        self.cache_manager.put(cache_key, pixmap)
                thread = threading.Thread(target=preload_worker, daemon=True)
                thread.start()
    
    def update_image_display(self):
        if not hasattr(self, 'current_pixmap'):
            return
        if self.fit_to_window:
            scaled_pixmap = self.current_pixmap.scaled(self.scroll_area.size(), Qt.KeepAspectRatio, Qt.FastTransformation)
        else:
            new_size = self.current_pixmap.size() * self.zoom_factor
            scaled_pixmap = self.current_pixmap.scaled(new_size, Qt.KeepAspectRatio, Qt.FastTransformation)
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
    
    def exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
    
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
        file_path, _ = QFileDialog.getOpenFileName(self, '이미지 열기', '', '이미지 파일 (*.png *.jpg *.jpeg *.gif *.webp);;ZIP 파일 (*.zip);;모든 파일 (*)')
        if file_path:
            self.load_path(file_path)
    
    def toggle_slideshow(self):
        self.slideshow.toggle()
        if self.slideshow.is_active():
            self.filename_label.setText("슬라이드쇼 재생 중...")
            self.filename_label.show()
        else:
            self.show_current_image()
    
    def rotate_right(self):
        self.rotation_angle = (self.rotation_angle + 90) % 360
        self.show_current_image()
    
    def rotate_left(self):
        self.rotation_angle = (self.rotation_angle - 90) % 360
        self.show_current_image()
    
    def show_context_menu(self, pos):
        menu = QMenu(self)
        
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
        
        menu.exec_(self.mapToGlobal(pos))
    
    def show_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec_():
            self.apply_settings()
    
    def apply_settings(self):
        bg_color = self.settings.get('background_color', '#1a1a1a')
        self.setStyleSheet(f"background-color: {bg_color};")
        cache_size = self.settings.get('cache_size', 50)
        self.cache_manager = CacheManager(cache_size)
        interval = self.settings.get('slideshow_interval', 3)
        self.slideshow.set_interval(interval)
        if self.image_list:
            self.show_current_image()
    
    def show_progress_bar(self, show):
        if show:
            self.progress_bar.show()
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.hide()
    
    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            return
        
        wheel_action = self.settings.get('wheel_action', 'navigate')
        if wheel_action == 'navigate':
            if event.angleDelta().y() > 0:
                self.prev_image()
            else:
                self.next_image()
        else:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
    
    def mousePressEvent(self, event):
        if event.button() == Qt.XButton1:
            self.prev_image()
        elif event.button() == Qt.XButton2:
            self.next_image()
        super().mousePressEvent(event)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_to_window and hasattr(self, 'current_pixmap'):
            self.update_image_display()
    
    def closeEvent(self, event):
        self.save_settings()
        self.slideshow.stop()
        super().closeEvent(event)

# Main
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
