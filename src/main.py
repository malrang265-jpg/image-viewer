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

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QDialog, QHBoxLayout, QComboBox, QCheckBox, QPushButton,
                            QColorDialog, QGroupBox, QFormLayout, QSpinBox,
                            QListWidget, QListWidgetItem, QMessageBox,
                            QListView, QSlider)
from PyQt5.QtCore import Qt, QTimer, QObject, QByteArray, QSize, QThread, pyqtSignal, QPoint, QEvent, QBuffer, QIODevice
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QWheelEvent, QTransform, QImageReader,
                        QMovie, QKeyEvent, QCloseEvent, QMouseEvent, QIcon, QColor, QPainter)
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

# Which algorithm handles saturation. Both are kept side by side so the two
# can be A/B compared directly -- flip this to 'enhance' to go back to the
# original behavior.
#   'matrix'  (default) -- does the exact same blend math as
#             ImageEnhance.Color (out = gray*(1-s) + channel*s, using
#             Pillow's own ITU-R 601-2 luma weights) but as a single 3x4
#             color-matrix convert() instead of building a full-size
#             grayscale "degenerate" image and blending against it.
#             Benchmarked ~20-35% faster across 480p-4K on this machine,
#             with pixel output within +/-1 of 'enhance' (rounding only).
#   'enhance' -- the original ImageEnhance.Color(img).enhance(...) path.
SATURATION_METHOD = 'matrix'

def _saturate_enhance(img, saturation):
    return get_pil_enhance().Color(img).enhance(saturation / 100.0)

def _saturate_matrix(img, saturation):
    s = saturation / 100.0
    lr, lg, lb = 0.299, 0.587, 0.114  # same weights Pillow's convert('L') uses
    if img.mode != 'RGB':
        img = img.convert('RGB')
    matrix = (
        lr * (1 - s) + s, lg * (1 - s),     lb * (1 - s),     0,
        lr * (1 - s),     lg * (1 - s) + s, lb * (1 - s),     0,
        lr * (1 - s),     lg * (1 - s),     lb * (1 - s) + s, 0,
    )
    return img.convert('RGB', matrix)

def apply_color_adjustments(img, saturation=100, brightness=100, contrast=100):
    """Apply saturation/brightness/contrast to a PIL RGB(A) image.

    Visually matches chaining ImageEnhance.Color -> Brightness -> Contrast
    (within +/-1-3 out of 255 from rounding, verified against many slider
    combinations including the 0/200 extremes), but brightness and contrast
    are each a plain per-channel function of the pixel value, so they're
    applied as one fast 256-entry point() lookup table instead of
    ImageEnhance's blend-against-a-full-size-degenerate-image, which
    benchmarked ~2.7x faster for those two alone on a 24MP image. Saturation
    is handled by _saturate_matrix/_saturate_enhance above depending on
    SATURATION_METHOD -- see that constant for how the two compare (a
    hand-written numpy version was also tried for this and was slower than
    either of PIL's own C implementations, so it isn't offered as a third
    option).
    The contrast LUT's pivot is computed from the *current* image (after
    saturation/brightness were already applied, same as ImageEnhance does
    internally) via PIL's own fast ImageStat, so the sequential-clipping
    behavior matches too.
    """
    if saturation != 100:
        if SATURATION_METHOD == 'matrix':
            img = _saturate_matrix(img, saturation)
        else:
            img = _saturate_enhance(img, saturation)
    if brightness != 100:
        b = brightness / 100.0
        lut = [max(0, min(255, round(x * b))) for x in range(256)]
        img = img.point(lut * len(img.getbands()))
    if contrast != 100:
        from PIL import ImageStat
        mean = round(ImageStat.Stat(img.convert('L')).mean[0])
        c = contrast / 100.0
        lut = [max(0, min(255, round(mean + (x - mean) * c))) for x in range(256)]
        img = img.point(lut * len(img.getbands()))
    return img


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

def get_frame_count(filepath):
    """Return a file's real animated-frame count via Pillow, or 0 if it
    isn't a multi-frame animation. Used for both gif and webp:
    QMovie.frameCount() is unreliable for many animated webp files (often
    reports 0), and for gif this is what tells a genuinely animated gif
    apart from a single-frame one -- a single-frame gif has nothing to
    animate, so it belongs on the normal static-image path instead of
    QMovie. (A QMovie whose one frame never advances again also never gets
    asked to redecode at a new size, so a later window resize can leave it
    stuck showing that frame at the old size -- routing it away from
    QMovie entirely sidesteps that instead of trying to patch around it.)"""
    try:
        Image = get_pil_image()
        with Image.open(filepath) as img:
            if getattr(img, 'is_animated', False):
                n = getattr(img, 'n_frames', 1)
                if n > 1:
                    return n
    except:
        pass
    return 0

class SingleApplication:
    def __init__(self, app_name="PekoviewerApp"):
        self.app_name = app_name
        self.socket = QLocalSocket()
        self.server = None
        self.file_received_callback = None

    def is_running(self):
        self.socket.connectToServer(self.app_name)
        # The already-running instance can only accept this connection once
        # its GUI thread is free -- e.g. it may be mid-decode of a large
        # animated webp frame right now. 30ms was too tight for that and
        # made this check false-negative under exactly that load. A real
        # "nothing is listening" case still fails almost immediately (a
        # refused connection isn't a timeout), so this doesn't slow down a
        # normal cold start.
        return self.socket.waitForConnected(1000)

    def start_server(self):
        self.server = QLocalServer()
        self.server.listen(self.app_name)
        self.server.newConnection.connect(self.on_new_connection)

    def send_message(self, message):
        if self.socket.state() == QLocalSocket.ConnectedState:
            self.socket.write(message.encode('utf-8'))
            # flush() alone doesn't guarantee the bytes actually left the
            # pipe -- Qt's own docs note the amount written depends on the
            # OS and say to use waitForBytesWritten() when not about to
            # return to an event loop, which is exactly this case (this
            # process calls sys.exit(0) right after send_message returns,
            # so it never does). Without this wait, disconnectFromServer()
            # right below could tear the pipe down before the message
            # actually left, silently dropping the file to open.
            self.socket.waitForBytesWritten(2000)
            self.socket.disconnectFromServer()

    def on_new_connection(self):
        socket = self.server.nextPendingConnection()
        if socket is None:
            return
        # The old fixed 30ms wait here regularly timed out before the
        # sender's bytes had arrived whenever this GUI thread happened to
        # be busy at that instant -- a large animated webp mid-frame-decode
        # is the common case -- which silently dropped the file-switch
        # request with no error and no retry. 3 seconds gives large
        # headroom over any realistic decode stall while keeping this as
        # the same plain, direct read the rest of this class already used.
        if socket.waitForReadyRead(3000):
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
            'window_geometry': {
                'x': 777, 'y': 258, 'width': 595, 'height': 608
            },
            'zoom_quality': 'balanced',
            'show_filename': False,
            'background_color': '#2b2b2b',
            'fit_to_window': True,
            'snap_enabled': True,
            'snap_threshold': 20,
            'saturation': 100,
            'brightness': 100,
            'contrast': 100,
            'anim_saturation': 100,
            'anim_brightness': 100,
            'anim_contrast': 100,
            'broken_image_path': '',
            'slideshow_interval': 3,
            'slideshow_mode': 'time',
            'slideshow_gif_loops': 2,
            'cache_size': 200,
            'cache_mb': 768,
            'preload_next': True,
            'preload_count': 3,
            'shortcuts': {
                'next_image': ['', ''],
                'prev_image': ['', ''],
                'zoom_in': ['', ''],
                'zoom_out': ['', ''],
                'toggle_actual_size': ['Tilt Left', ''],
                'toggle_fullscreen': ['Left Double Click', ''],
                'close_program': ['XButton1', ''],
                'show_image_list': ['Return', ''],
                'delete_image': ['', ''],
                'open_file': ['', ''],
                'slideshow': ['', ''],
            }
        }
    
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    def set(self, key, value):
        self.data[key] = value
        self.save()

    def update_many(self, values):
        if values:
            self.data.update(values)
            self.save()

    def update_shortcuts_many(self, values):
        if values:
            self.data.setdefault('shortcuts', {}).update(values)
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

# PIL releases the GIL during its C-level decode/enhance work, so these
# worker threads genuinely run in parallel on multi-core machines. The old
# cap of 4 could bottleneck once the current image plus several preloaded
# neighbors are all in flight together; 8 gives more headroom on typical
# desktops while the floor of 2 and the cpu_count() scaling still protect
# low-core machines.
_DECODE_WORKER_COUNT = max(2, min(8, (os.cpu_count() or 4)))

# Animated-frame color processing gets its own small pool, separate from
# _executor above (which handles static-image decode/preload). Without this
# split, playing a color-adjusted gif/webp while neighboring images preload
# in the background makes both compete for the same workers -- the
# animation stalls waiting behind preload jobs and preload slows down too.
# Kept small (1-3) on purpose: only one frame is ever "live" per movie plus
# a couple of look-ahead frames, so extra workers here wouldn't speed up a
# single animation, they'd just take workers away from the shared pool.
_ANIM_WORKER_COUNT = max(1, min(3, (os.cpu_count() or 4) // 2))

class ImageLoader:
    _shutdown = False
    # bmp/tif(f)/ico added on top of the original set -- Pillow already
    # handles all of them (no new dependency), and none of them are ever
    # animated here (see _load_animated_movie's ext check), so they go
    # straight through the same static-image decode path as png/jpg: the
    # QImageReader fast path with a PIL fallback, background-thread
    # decoding, and the existing cache. No change to per-frame/playback
    # cost for gif or webp.
    SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.gif', '.webp',
                          '.bmp', '.tif', '.tiff', '.ico'}
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=_DECODE_WORKER_COUNT)
    _anim_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_WORKER_COUNT)

    @staticmethod
    def is_supported(filename):
        return os.path.splitext(filename)[1].lower() in ImageLoader.SUPPORTED_FORMATS

    @staticmethod
    def load_image_data(filepath, saturation=100, brightness=100, contrast=100, max_size=None):
        # Fast path: native Qt decoding avoids Pillow RGB conversion and byte copies.
        try:
            if saturation == 100 and brightness == 100 and contrast == 100:
                reader = QImageReader(filepath)
                reader.setAutoTransform(True)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    src_size = reader.size()
                    if src_size.isValid() and src_size.width() > 0 and src_size.height() > 0:
                        reader.setScaledSize(src_size.scaled(
                            QSize(int(max_size[0]), int(max_size[1])), Qt.KeepAspectRatio))
                image = reader.read()
                if not image.isNull():
                    return image

            # Keep Pillow for the color-adjustment path so output behavior stays the same.
            Image = get_pil_image()
            with Image.open(filepath) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                img = src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    # BILINEAR here trades a little resample quality for real
                    # speed: this thumbnail gets scaled again by Qt to the
                    # exact viewport size right after (update_image_display),
                    # so LANCZOS's extra sharpness on this intermediate step
                    # was mostly being thrown away anyway.
                    resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                    img.thumbnail(max_size, resample)
                img = apply_color_adjustments(img, saturation, brightness, contrast)
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
        try:
            cls._anim_executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            cls._anim_executor.shutdown(wait=True)
        except Exception:
            pass

    @classmethod
    def restart_executor(cls):
        if cls._shutdown:
            cls._executor = concurrent.futures.ThreadPoolExecutor(max_workers=_DECODE_WORKER_COUNT)
            cls._anim_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_WORKER_COUNT)
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
    _supported = ImageLoader.SUPPORTED_FORMATS

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

            if saturation == 100 and brightness == 100 and contrast == 100:
                buffer = QBuffer()
                buffer.setData(QByteArray(data))
                buffer.open(QIODevice.ReadOnly)
                reader = QImageReader(buffer)
                reader.setAutoTransform(True)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    src_size = reader.size()
                    if src_size.isValid() and src_size.width() > 0 and src_size.height() > 0:
                        reader.setScaledSize(src_size.scaled(
                            QSize(int(max_size[0]), int(max_size[1])), Qt.KeepAspectRatio))
                image = reader.read()
                buffer.close()
                if not image.isNull():
                    return image

            Image = get_pil_image()
            with Image.open(BytesIO(data)) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                img = src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                    img.thumbnail(max_size, resample)
                img = apply_color_adjustments(img, saturation, brightness, contrast)
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
            ('slideshow', '슬라이드쇼'),
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
                  'delete_image', 'open_file', 'slideshow']
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
    
    def wheelEvent(self, event):
        if self.capturing and self.current_action:
            dx = event.angleDelta().x()
            if dx != 0:
                # Match the physical tilt direction used by the viewer:
                # positive Qt horizontal delta corresponds to physical Tilt Left.
                button_text = 'Tilt Left' if dx > 0 else 'Tilt Right'
                if button_text not in self.captured_keys:
                    self.captured_keys.append(button_text)
                event.accept()
                return
        super().wheelEvent(event)

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
        values = {}
        for action, buttons in self.shortcut_buttons.items():
            shortcuts = [buttons[0].text(), buttons[1].text()]
            values[action] = [s if s != '없음' else '' for s in shortcuts]
        self.settings.update_shortcuts_many(values)
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
        
        static_adjust_group = QGroupBox('정지 이미지 조절')
        static_adjust_layout = QFormLayout()

        self.saturation_slider = QSlider(Qt.Horizontal)
        self.saturation_slider.setRange(0, 200)
        self.saturation_slider.setValue(100)
        self.saturation_label = QLabel('100%')
        saturation_row = QHBoxLayout()
        saturation_row.addWidget(self.saturation_slider)
        saturation_row.addWidget(self.saturation_label)
        static_adjust_layout.addRow('채도:', saturation_row)

        self.brightness_slider = QSlider(Qt.Horizontal)
        self.brightness_slider.setRange(0, 200)
        self.brightness_slider.setValue(100)
        self.brightness_label = QLabel('100%')
        brightness_row = QHBoxLayout()
        brightness_row.addWidget(self.brightness_slider)
        brightness_row.addWidget(self.brightness_label)
        static_adjust_layout.addRow('밝기:', brightness_row)

        self.contrast_slider = QSlider(Qt.Horizontal)
        self.contrast_slider.setRange(0, 200)
        self.contrast_slider.setValue(100)
        self.contrast_label = QLabel('100%')
        contrast_row = QHBoxLayout()
        contrast_row.addWidget(self.contrast_slider)
        contrast_row.addWidget(self.contrast_label)
        static_adjust_layout.addRow('명도/대비:', contrast_row)

        reset_adjust_button = QPushButton('정지 이미지 조절 초기화')
        reset_adjust_button.clicked.connect(self.reset_adjustments)
        static_adjust_layout.addRow('', reset_adjust_button)

        static_adjust_group.setLayout(static_adjust_layout)
        layout.addWidget(static_adjust_group)

        anim_adjust_group = QGroupBox('움직이는 이미지 조절 (GIF·애니메이션 WebP)')
        anim_adjust_layout = QFormLayout()

        self.anim_saturation_slider = QSlider(Qt.Horizontal)
        self.anim_saturation_slider.setRange(0, 200)
        self.anim_saturation_slider.setValue(100)
        self.anim_saturation_label = QLabel('100%')
        anim_saturation_row = QHBoxLayout()
        anim_saturation_row.addWidget(self.anim_saturation_slider)
        anim_saturation_row.addWidget(self.anim_saturation_label)
        anim_adjust_layout.addRow('채도:', anim_saturation_row)

        self.anim_brightness_slider = QSlider(Qt.Horizontal)
        self.anim_brightness_slider.setRange(0, 200)
        self.anim_brightness_slider.setValue(100)
        self.anim_brightness_label = QLabel('100%')
        anim_brightness_row = QHBoxLayout()
        anim_brightness_row.addWidget(self.anim_brightness_slider)
        anim_brightness_row.addWidget(self.anim_brightness_label)
        anim_adjust_layout.addRow('밝기:', anim_brightness_row)

        self.anim_contrast_slider = QSlider(Qt.Horizontal)
        self.anim_contrast_slider.setRange(0, 200)
        self.anim_contrast_slider.setValue(100)
        self.anim_contrast_label = QLabel('100%')
        anim_contrast_row = QHBoxLayout()
        anim_contrast_row.addWidget(self.anim_contrast_slider)
        anim_contrast_row.addWidget(self.anim_contrast_label)
        anim_adjust_layout.addRow('명도/대비:', anim_contrast_row)

        reset_anim_adjust_button = QPushButton('움직이는 이미지 조절 초기화')
        reset_anim_adjust_button.clicked.connect(self.reset_anim_adjustments)
        anim_adjust_layout.addRow('', reset_anim_adjust_button)

        anim_adjust_group.setLayout(anim_adjust_layout)
        layout.addWidget(anim_adjust_group)

        apply_button = QPushButton('현재 이미지에 즉시 적용')
        apply_button.clicked.connect(self.apply_immediately)
        layout.addWidget(apply_button)
        
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

        error_group = QGroupBox('오류 처리')
        error_layout = QFormLayout()
        self.broken_image_path = ''
        self.broken_image_path_label = QLabel('설정 안 함 (기본 이미지 사용)')
        self.broken_image_path_label.setWordWrap(True)
        error_layout.addRow('손상된 파일 대체 이미지:', self.broken_image_path_label)
        broken_image_buttons = QHBoxLayout()
        choose_broken_button = QPushButton('찾아보기...')
        choose_broken_button.clicked.connect(self.choose_broken_image)
        clear_broken_button = QPushButton('지우기')
        clear_broken_button.clicked.connect(self.clear_broken_image)
        broken_image_buttons.addWidget(choose_broken_button)
        broken_image_buttons.addWidget(clear_broken_button)
        error_layout.addRow('', broken_image_buttons)
        error_note = QLabel('직접 이동 중 파일을 읽을 수 없으면 이 이미지가 대신 표시됩니다.\n슬라이드쇼 중에는 대신 자동으로 다음 이미지로 건너뜁니다.')
        error_note.setWordWrap(True)
        error_layout.addRow('', error_note)
        error_group.setLayout(error_layout)
        layout.addWidget(error_group)
        
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
        self.anim_saturation_slider.valueChanged.connect(
            lambda v: self.anim_saturation_label.setText(f'{v}%'))
        self.anim_brightness_slider.valueChanged.connect(
            lambda v: self.anim_brightness_label.setText(f'{v}%'))
        self.anim_contrast_slider.valueChanged.connect(
            lambda v: self.anim_contrast_label.setText(f'{v}%'))
    
    def reset_adjustments(self):
        self.saturation_slider.setValue(100)
        self.brightness_slider.setValue(100)
        self.contrast_slider.setValue(100)
        self.apply_immediately()

    def reset_anim_adjustments(self):
        self.anim_saturation_slider.setValue(100)
        self.anim_brightness_slider.setValue(100)
        self.anim_contrast_slider.setValue(100)
        self.apply_immediately()

    def apply_immediately(self):
        parent = self.parent()
        if parent and hasattr(parent, 'apply_image_adjustments'):
            parent.apply_image_adjustments(
                self.saturation_slider.value(),
                self.brightness_slider.value(),
                self.contrast_slider.value(),
                self.anim_saturation_slider.value(),
                self.anim_brightness_slider.value(),
                self.anim_contrast_slider.value()
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
        self.anim_saturation_slider.setValue(self.settings.get('anim_saturation', 100))
        self.anim_brightness_slider.setValue(self.settings.get('anim_brightness', 100))
        self.anim_contrast_slider.setValue(self.settings.get('anim_contrast', 100))
        self.snap_enabled.setChecked(self.settings.get('snap_enabled', True))
        self.snap_threshold.setValue(self.settings.get('snap_threshold', 20))
        mode = self.settings.get('slideshow_mode', 'time')
        index = self.slideshow_mode.findData(mode)
        if index >= 0:
            self.slideshow_mode.setCurrentIndex(index)
        self.slideshow_interval.setValue(self.settings.get('slideshow_interval', 3))
        self.slideshow_gif_loops.setValue(self.settings.get('slideshow_gif_loops', 2))
        self.broken_image_path = self.settings.get('broken_image_path', '')
        self.update_broken_image_label()
        self.current_color = self.settings.get('background_color', '#2b2b2b')
        self.update_color_button()
    
    def choose_broken_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '손상된 파일 대체 이미지 선택', '',
            '이미지 파일 (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff *.ico)')
        if path:
            self.broken_image_path = path
            self.update_broken_image_label()

    def clear_broken_image(self):
        self.broken_image_path = ''
        self.update_broken_image_label()

    def update_broken_image_label(self):
        if self.broken_image_path:
            self.broken_image_path_label.setText(self.broken_image_path)
        else:
            self.broken_image_path_label.setText('설정 안 함 (기본 이미지 사용)')

    def choose_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.current_color = color.name()
            self.update_color_button()
    
    def update_color_button(self):
        self.color_button.setStyleSheet(f"background-color: {self.current_color}; color: white;")
        self.color_button.setText(self.current_color)
    
    def save_settings(self):
        self.settings.update_many({
            'zoom_quality': self.zoom_quality.currentData(),
            'show_filename': self.show_filename.isChecked(),
            'fit_to_window': self.fit_to_window.isChecked(),
            'preload_next': self.preload_enabled.isChecked(),
            'preload_count': self.preload_count.currentData(),
            'saturation': self.saturation_slider.value(),
            'brightness': self.brightness_slider.value(),
            'contrast': self.contrast_slider.value(),
            'anim_saturation': self.anim_saturation_slider.value(),
            'anim_brightness': self.anim_brightness_slider.value(),
            'anim_contrast': self.anim_contrast_slider.value(),
            'snap_enabled': self.snap_enabled.isChecked(),
            'snap_threshold': self.snap_threshold.value(),
            'slideshow_mode': self.slideshow_mode.currentData(),
            'slideshow_interval': self.slideshow_interval.value(),
            'slideshow_gif_loops': self.slideshow_gif_loops.value(),
            'broken_image_path': self.broken_image_path,
            'background_color': self.current_color,
        })
        self.accept()

class PanLabel(QLabel):
    """Image label with reliable left-drag panning.

    The pan starts only after a small movement threshold so a normal
    left double-click can still trigger the fullscreen shortcut.
    """
    def __init__(self, viewer):
        super().__init__()
        self.viewer = viewer
        self._pressed = False
        self._panning = False
        self._press_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.viewer._can_pan_image():
            self._pressed = True
            self._panning = False
            self._press_pos = QPoint(event.globalPos())
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed and self._press_pos is not None:
            delta = QPoint(event.globalPos()) - self._press_pos
            if not self._panning and (abs(delta.x()) >= 4 or abs(delta.y()) >= 4):
                if self.viewer._start_image_pan(self._press_pos):
                    self._panning = True
            if self._panning:
                self.viewer._move_image_pan(event.globalPos())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pressed:
            was_panning = self._panning
            self._pressed = False
            self._panning = False
            self._press_pos = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            if was_panning:
                self.viewer._end_image_pan()
                event.accept()
                return
            # No movement: let the viewer handle the click/double-click.
            # Releasing here prevents the frameless-window drag from starting.
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = False
            self._panning = False
            self._press_pos = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            self.viewer.check_mouse_shortcut('Left Double Click')
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ImageViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.cache_manager = CacheManager(self.settings.get('cache_size', 200), self.settings.get('cache_mb', 768))
        self.load_bridge = ImageLoadBridge()
        self.load_bridge.loaded.connect(self._on_background_loaded)
        self.load_bridge.animated_frame.connect(self._on_animated_frame_ready)
        self.load_generation = 0
        self.loading_keys = set()
        # Cache the expensive color-adjusted source separately from the
        # display-size cache. This prevents repeated Pillow work when navigating.
        self._adjusted_image_cache = {}
        self._adjusted_image_cache_order = []
        self._adjusted_image_cache_limit = 24
        self._source_image_cache = {}
        self._source_image_cache_order = []
        self._source_image_cache_limit = 12
        self.load_retry_counts = {}
        self.preload_enabled = self.settings.get('preload_next', True)
        self.preload_count = max(0, min(10, int(self.settings.get('preload_count', 3))))
        self.slideshow = QTimer()
        self.slideshow.timeout.connect(self.next_image)
        self.slideshow_playing = False
        self.slideshow_mode = 'time'
        self.slideshow_fail_streak = 0
        self.gif_loop_count = 0
        self.gif_max_loops = 2
        self.gif_frame_connected = False
        self.gif_last_frame = -1
        self.current_index = 0
        self.image_list = []
        self.current_zip = None
        self.zoom_factor = 1.0
        self.fit_to_window = True
        self.current_movie = None
        # Backing QBuffer for a movie built from in-memory bytes (a zip
        # entry, since QMovie can't read a zip path directly). Must be kept
        # alive for as long as the movie is; stop_current_movie() closes it.
        self.current_movie_buffer = None
        self.current_movie_original_size = None
        self.current_movie_generation = 0
        self.animated_frame_cache = OrderedDict()
        self.animated_frame_cache_limit = 24
        self.current_movie_frame = -1
        # A second, unstarted QMovie built from the exact same source as
        # current_movie, used only to jumpToFrame() a few frames ahead and
        # hand those frames to the anim worker pool for color processing
        # *before* playback reaches them. It decodes through the same Qt
        # plugin with the same setScaledSize(), so prefetched frames are
        # pixel-identical to what the live movie would have produced --
        # unlike re-decoding with Pillow, which could scale slightly
        # differently. Keys already in animated_frame_cache/in-flight are
        # skipped, so this only ever does extra work that would otherwise
        # have been done later anyway, just earlier.
        self.prefetch_movie = None
        self.prefetch_buffer = None
        self.prefetch_frame_count = None
        self.anim_lookahead = 2
        self.animated_inflight_keys = set()
        # The size the current animated frame should actually appear at on
        # screen (post zoom/fit). May be larger than
        # current_movie_original_size when zoomed in -- see
        # _anim_decode_size/_apply_anim_scaled_size for why the movies
        # themselves are capped at the original resolution instead of
        # being asked to decode at this size directly.
        self.current_movie_target_size = None
        self.current_pixmap = None
        self.original_pixmap = None
        self.is_loading = False
        self._default_broken_pixmap_cache = None
        self.dragging = False
        self.drag_start_pos = None
        # Image panning: when zoomed beyond the viewport, drag the image itself.
        # Only scrollbar offsets change during a pan; the pixmap is never re-scaled.
        self.panning = False
        self.pan_start_pos = None
        self.pan_start_h = 0
        self.pan_start_v = 0
        self.window_start_pos = None
        self.resizing = False
        self.resize_start_pos = None
        self.resize_start_size = None
        self.resize_region = None
        self.resize_margin = 12
        self.cursor_hidden = False
        self.cursor_hide_timer = QTimer()
        self.cursor_hide_timer.setSingleShot(True)
        self.cursor_hide_timer.timeout.connect(self.hide_cursor)
        self._display_update_timer = QTimer(self)
        self._display_update_timer.setSingleShot(True)
        self._display_update_timer.timeout.connect(self.update_image_display)
        self.init_ui()
        self.load_settings()
        self.setup_icon()
        self.slideshow.setInterval(self.settings.get('slideshow_interval', 3) * 1000)
    
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
        self.reset_cursor_timer()
    
    def hide_cursor(self):
        # Never hide mid-interaction: losing the cursor while actively
        # resizing/dragging/panning would be disorienting.
        if self.cursor_hidden or self.dragging or self.resizing or self.panning:
            return
        self.setCursor(Qt.BlankCursor)
        self.cursor_hidden = True
    
    def show_cursor(self):
        if self.cursor_hidden:
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            self.cursor_hidden = False
    
    def reset_cursor_timer(self):
        # Auto-hide-after-idle now applies in windowed mode too, not just fullscreen.
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
    
    def apply_image_adjustments(self, saturation, brightness, contrast,
                                 anim_saturation, anim_brightness, anim_contrast):
        self.settings.update_many({
            'saturation': saturation,
            'brightness': brightness,
            'contrast': contrast,
            'anim_saturation': anim_saturation,
            'anim_brightness': anim_brightness,
            'anim_contrast': anim_contrast,
        })
        # No cache_manager.clear() here: every cache key already includes
        # saturation/brightness/contrast (see _cache_key / _animated_cache_key),
        # so entries made under the old values simply stop being matched
        # instead of needing eviction -- and leaving them in place means
        # flipping back to a value used earlier (or an image already
        # processed at the new one) can still hit the cache instead of
        # paying full decode+adjust again.
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
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.scroll_area)
        self.image_label = PanLabel(self)
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
        if self.isFullScreen():
            # Resizing isn't possible in fullscreen, so never show a resize cursor there.
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            return
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
                self.reset_cursor_timer()
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
        if self.current_movie_buffer is not None:
            try:
                self.current_movie_buffer.close()
            except:
                pass
            self.current_movie_buffer = None
        if self.prefetch_movie is not None:
            try:
                self.prefetch_movie.stop()
            except:
                pass
            self.prefetch_movie = None
        if self.prefetch_buffer is not None:
            try:
                self.prefetch_buffer.close()
            except:
                pass
            self.prefetch_buffer = None
        self.prefetch_frame_count = None
        self.animated_inflight_keys.clear()
        self.current_movie_original_size = None
        self.current_movie_target_size = None
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
        # Small safety margin prevents repeated reloads caused by tiny widget changes.
        return (max(64, size.width() + 64), max(64, size.height() + 64))

    def _source_cache_get(self, key):
        value = self._source_image_cache.get(key)
        if value is not None:
            try:
                self._source_image_cache_order.remove(key)
            except ValueError:
                pass
            self._source_image_cache_order.append(key)
        return value

    def _source_cache_put(self, key, image):
        if image is None:
            return
        self._source_image_cache[key] = image
        try:
            self._source_image_cache_order.remove(key)
        except ValueError:
            pass
        self._source_image_cache_order.append(key)
        while len(self._source_image_cache_order) > self._source_image_cache_limit:
            old = self._source_image_cache_order.pop(0)
            self._source_image_cache.pop(old, None)

    def _adjusted_cache_key(self, filepath, saturation, brightness, contrast):
        source = f'{self.current_zip}|{filepath}' if self.current_zip else filepath
        return (source, int(saturation), int(brightness), int(contrast))

    def _adjusted_cache_get(self, key):
        value = self._adjusted_image_cache.get(key)
        if value is not None:
            try:
                self._adjusted_image_cache_order.remove(key)
            except ValueError:
                pass
            self._adjusted_image_cache_order.append(key)
        return value

    def _adjusted_cache_put(self, key, image):
        if image is None:
            return
        self._adjusted_image_cache[key] = image
        try:
            self._adjusted_image_cache_order.remove(key)
        except ValueError:
            pass
        self._adjusted_image_cache_order.append(key)
        while len(self._adjusted_image_cache_order) > self._adjusted_image_cache_limit:
            old = self._adjusted_image_cache_order.pop(0)
            self._adjusted_image_cache.pop(old, None)

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
            source_fast_key = ((source_zip, filename), max_size)
            if (saturation, brightness, contrast) == (100, 100, 100):
                source_cached = self._source_cache_get(source_fast_key)
                if source_cached is not None:
                    self.load_bridge.loaded.emit(
                        generation, key, source_cached, index == self.current_index
                    )
                    return

            adjustment_key = self._adjusted_cache_key(filename, saturation, brightness, contrast)
            # For non-default adjustments, reuse the expensive color-adjusted
            # source when available. The existing display cache still handles
            # the fit-to-window/full-size distinction.
            if (saturation, brightness, contrast) != (100, 100, 100):
                cached_adjusted = self._adjusted_cache_get(adjustment_key)
                if cached_adjusted is not None:
                    image = cached_adjusted
                    self.load_bridge.loaded.emit(generation, key, image, index == self.current_index)
                    return

            if source_zip:
                image = ZipHandler.load_image_data(source_zip, filename, saturation, brightness, contrast, max_size)
            else:
                image = ImageLoader.load_image_data(filename, saturation, brightness, contrast, max_size)
                if image is None and max_size is None:
                    try:
                        image = QImage(filename)
                    except Exception:
                        image = None

            if image is not None and (saturation, brightness, contrast) != (100, 100, 100):
                self._adjusted_cache_put(adjustment_key, image)
            self.load_bridge.loaded.emit(generation, key, image, index == self.current_index)
        try:
            ImageLoader._executor.submit(worker)
        except Exception as e:
            self.loading_keys.discard(key)
            print(f"백그라운드 로딩 시작 오류: {e}")

    def _on_background_loaded(self, generation, key, image, was_current):
        # A preload may have been started for an older navigation generation.
        # Its result is still valuable: keep it in cache. Only the paint decision
        # must be based on the image that is current *now*.
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
                self._retry_or_fail_current_load(key)
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            if key == current_key:
                self._retry_or_fail_current_load(key)
            return

        self.load_retry_counts.pop(key, None)
        self.cache_manager.put(key, pixmap)

        # Display only if this result matches what is visible right now.
        # This fixes rapid navigation races where an older worker finishes late.
        if key == current_key:
            self._display_pixmap(pixmap)
            self.is_loading = False
            self.slideshow_fail_streak = 0

    def _retry_or_fail_current_load(self, key):
        # Never blank the viewer on the first failure -- retry the currently
        # requested image once, after a short delay, in case it was just a
        # transient read hiccup (e.g. a locked/still-being-written file).
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
            self._handle_unreadable_current_image()

    def _handle_unreadable_current_image(self):
        # The retry above was also exhausted: the current file has a
        # supported extension but its data genuinely can't be decoded
        # (corrupted/truncated, etc). During a slideshow this must not just
        # sit there waiting for a signal that will never come (this is what
        # used to freeze a GIF-loop-mode slideshow, since a movie that never
        # started never emits the frameChanged it needs to count loops) --
        # skip past it automatically instead. While browsing manually,
        # replace the stale previous frame with an explicit "broken image"
        # placeholder rather than leaving old content on screen that looks
        # like it belongs to this file.
        self.is_loading = False
        if self.slideshow_playing:
            self.slideshow_fail_streak += 1
            if self.slideshow_fail_streak > min(len(self.image_list), 200):
                # Every remaining image is failing to load; stop instead of
                # spinning through the whole list indefinitely.
                self.stop_slideshow()
                self._display_pixmap(self._get_broken_image_pixmap())
                return
            self.next_image()
        else:
            self._display_pixmap(self._get_broken_image_pixmap())

    def _get_broken_image_pixmap(self):
        path = self.settings.get('broken_image_path', '')
        if path:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                return pixmap
        return self._default_broken_pixmap()

    def _default_broken_pixmap(self):
        if self._default_broken_pixmap_cache is None:
            pixmap = QPixmap(400, 300)
            pixmap.fill(QColor('#3c3c3c'))
            painter = QPainter(pixmap)
            try:
                painter.setPen(QColor('#cccccc'))
                font = painter.font()
                font.setPointSize(13)
                painter.setFont(font)
                painter.drawText(pixmap.rect(), Qt.AlignCenter, '이미지를 불러올 수 없습니다')
            finally:
                painter.end()
            self._default_broken_pixmap_cache = pixmap
        return self._default_broken_pixmap_cache

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

        # Preload symmetrically around the current image. Nearer images are
        # submitted first so the immediately-next image gets priority.
        generation = self.load_generation
        for distance in range(1, count + 1):
            for direction in (1, -1):
                idx = self.current_index + (distance * direction)
                if 0 <= idx < len(self.image_list):
                    self._submit_image_load(idx, generation)

    def _set_display_pixmap(self, pixmap):
        if pixmap is None or pixmap.isNull():
            return False
        self.image_label.setPixmap(pixmap)
        return True

    def _load_animated_movie(self, current_file, ext):
        """Try to load current_file as a playable QMovie (an animated gif,
        or a webp with more than one frame). Works whether the file sits on
        disk or inside the currently open zip -- QMovie can't read a zip
        path directly, so a zip entry is read into memory first and handed
        to QMovie through a QBuffer.

        Returns (movie, buffer, frame_count):
          - movie is None both when the file isn't an animated gif/webp and
            when it is one but couldn't actually be decoded (corrupted/
            truncated data). Either way the caller should fall back to the
            regular static-image path, which is also what surfaces a
            genuine decode failure through the normal load-failure handling
            (retry, then slideshow auto-skip / broken-image placeholder).
          - buffer is the QBuffer backing a zip-sourced movie. It must be
            kept alive (self.current_movie_buffer) for as long as the movie
            is in use -- QMovie keeps reading frames from it as playback
            advances, it doesn't copy the data up front. It's None for a
            movie loaded straight from a real file path.
          - frame_count is the animation's real frame count, for both gif
            and webp (Pillow already had to report it via get_frame_count
            below to confirm the file is actually animated, so it's free
            here -- movie.frameCount() is unreliable for many animated
            webp files).
          - source is the raw bytes read from the zip entry (or None when
            the file was read straight from current_file on disk). Callers
            that want a second, independent QMovie on the same data (e.g.
            for frame look-ahead) can pass this straight back into
            _build_qmovie() -- re-reading the zip entry a second time is
            avoided since the bytes are already in memory here.
        """
        if ext not in ('.gif', '.webp'):
            return None, None, None, None

        data = None
        if self.current_zip:
            try:
                zf = ZipHandler._get_zip(self.current_zip)
                with zf.open(current_file, 'r') as fp:
                    data = fp.read()
            except Exception:
                return None, None, None, None

        # get_frame_count tells a static (single-frame) gif/webp apart from
        # a genuinely animated one; a static one belongs on the normal
        # image path instead of QMovie. This used to only be checked for
        # webp -- every .gif went straight to QMovie regardless of frame
        # count -- which is what let a single-frame gif get stuck not
        # resizing with the window (see get_frame_count's docstring).
        frame_count = get_frame_count(BytesIO(data) if data is not None else current_file)
        if not frame_count:
            return None, None, None, None

        movie, buffer = self._build_qmovie(current_file, data)
        if movie is None:
            return None, None, None, None

        return movie, buffer, frame_count, data

    def _build_qmovie(self, current_file, data):
        """Build one playable QMovie from either raw bytes (data, for a zip
        entry) or a path on disk (current_file, when data is None). Used
        both for the live/display movie and for the independent look-ahead
        movie in show_current_image, so the two always decode identically."""
        buffer = None
        movie = None
        try:
            if data is not None:
                buffer = QBuffer()
                buffer.setData(QByteArray(data))
                if not buffer.open(QIODevice.ReadOnly):
                    return None, None
                movie = QMovie()
                movie.setDevice(buffer)
            else:
                movie = QMovie(current_file)
            if not movie.isValid():
                raise ValueError('invalid movie')
            movie.jumpToFrame(0)
            first_frame = movie.currentPixmap()
            if first_frame.isNull() or first_frame.width() <= 0:
                raise ValueError('first frame failed to decode')
        except Exception:
            if buffer is not None:
                try:
                    buffer.close()
                except Exception:
                    pass
            return None, None

        return movie, buffer

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

        # Animated GIF/WebP: keep QMovie for timing/decoding, and render each
        # frame through an in-memory filter on demand as it plays (only the
        # frame currently on screen is ever processed, cached by frame
        # number + anim_* settings so repeat loops are free). This works the
        # same way for a file inside a zip as for one on disk --
        # _load_animated_movie reads the zip entry into memory and hands
        # QMovie a QBuffer instead of a file path. Moving images use their
        # own anim_* saturation/brightness/contrast settings, independent
        # from the ones used for static images (see _render_animated_frame).
        ext = os.path.splitext(current_file)[1].lower()
        movie, movie_buffer, known_frame_count, movie_source = self._load_animated_movie(current_file, ext)
        if movie:
            self.current_movie = movie
            self.current_movie_buffer = movie_buffer
            self.current_movie_generation += 1
            movie_generation = self.current_movie_generation
            self.current_movie_original_size = movie.currentPixmap().size()
            self.scroll_area.setWidgetResizable(self.fit_to_window)

            # Built before the scaled-size is applied below so
            # _apply_anim_scaled_size can sync both movies in one place.
            self.prefetch_movie, self.prefetch_buffer = self._build_qmovie(current_file, movie_source)

            scaled_size = None
            if self.current_movie_original_size.width() > 0:
                if self.fit_to_window:
                    scaled_size = self.current_movie_original_size.scaled(self.scroll_area.size(), Qt.KeepAspectRatio)
                else:
                    scaled_size = QSize(int(self.current_movie_original_size.width() * self.zoom_factor),
                                         int(self.current_movie_original_size.height() * self.zoom_factor))
                if scaled_size.width() > 0 and scaled_size.height() > 0:
                    self._apply_anim_scaled_size(scaled_size)
                else:
                    scaled_size = None
            self.current_movie_frame = -1
            self.animated_frame_cache.clear()
            self.slideshow_fail_streak = 0

            # Size the frame cache to fit the animation's whole loop
            # (bounded by a memory budget, since frames can be large),
            # instead of a flat 24-frame limit. The frame count is already
            # known for free here -- get_frame_count (via Pillow) already
            # had to determine it for both gif and webp just to confirm
            # the file is genuinely animated before reaching this point,
            # so using it here costs no extra decoding.
            # A too-small fixed cache is what let a long/heavy animation's
            # frames get evicted before their next loop could reuse them,
            # so color processing (and the staleness that comes with it)
            # never actually settled down no matter how long you waited.
            frame_count = known_frame_count or movie.frameCount()
            if frame_count and frame_count > 0:
                w = scaled_size.width() if scaled_size else self.current_movie_original_size.width()
                h = scaled_size.height() if scaled_size else self.current_movie_original_size.height()
                if w > 0 and h > 0:
                    bytes_per_frame = w * h * 4
                    budget = 800 * 1024 * 1024  # ~800MB ceiling for this cache
                    by_memory = max(1, budget // max(1, bytes_per_frame))
                    self.animated_frame_cache_limit = max(24, min(frame_count, by_memory, 600))
                else:
                    self.animated_frame_cache_limit = max(24, min(frame_count, 300))
            else:
                self.animated_frame_cache_limit = 24

            self.prefetch_frame_count = frame_count if frame_count and frame_count > 0 else None

            # A new movie must reconnect slideshow loop counting.
            if self.slideshow_playing and self.slideshow_mode == 'loop':
                self.connect_gif_loop()
            movie.frameChanged.connect(self.on_animated_frame_changed)
            movie.start()
            start_frame = movie.currentFrameNumber()
            self._render_animated_frame(start_frame, movie_generation)
            self._prefetch_ahead(start_frame)
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
        # Keep the previous frame visible while the new image is decoding.
        # Clearing the label here caused the frequent black-screen effect during
        # rapid navigation. A successful decode will replace it atomically.
        self._submit_image_load(self.current_index, generation)
        self._preload_neighbors()

    def _animated_cache_key(self, frame_number):
        return (frame_number,
                self.settings.get('anim_saturation', 100),
                self.settings.get('anim_brightness', 100),
                self.settings.get('anim_contrast', 100),
                self.fit_to_window,
                self.zoom_factor,
                self.scroll_area.size().width(),
                self.scroll_area.size().height())

    def _anim_decode_size(self, target_size):
        """The size QMovie should actually decode/scale a frame to. Capped
        at the animation's native resolution: when the user is zoomed in
        past 1:1, letting QMovie upscale every frame before our own color
        filter runs on it means the filter -- and every raw-bytes copy
        around it in _submit_animated_frame_processing -- pays for pixels
        that carry no extra information over the native frame. Instead the
        movie decodes at native resolution and the upscale to the actual
        on-screen size happens once, cheaply (a single resize), after the
        per-pixel color math instead of before it. When target_size is at
        or below native resolution (fit-to-window, or zoomed out) this is a
        no-op -- that case was already cheap and correct."""
        orig = self.current_movie_original_size
        if not target_size or not orig or orig.width() <= 0 or orig.height() <= 0:
            return target_size
        if target_size.width() <= orig.width() and target_size.height() <= orig.height():
            return target_size
        return orig

    def _apply_anim_scaled_size(self, target_size):
        """Set the on-screen target size for the current animation and sync
        both the live and look-ahead movies to the (possibly smaller,
        native-capped) decode size. Call this -- not setScaledSize()
        directly -- on load and on every zoom/window-size change, so the
        two movies never drift apart; see the note in _prefetch_ahead about
        what happens when they do."""
        self.current_movie_target_size = target_size
        decode_size = self._anim_decode_size(target_size)
        if self.current_movie:
            self.current_movie.setScaledSize(decode_size)
        if self.prefetch_movie:
            self.prefetch_movie.setScaledSize(decode_size)

    def on_animated_frame_changed(self, frame_number):
        if not self.current_movie:
            return
        self.current_movie_frame = frame_number
        self._render_animated_frame(frame_number, self.current_movie_generation)
        # Keep the next couple of frames a step ahead of playback so their
        # color processing is already sitting in cache by the time the
        # movie actually reaches them, instead of starting cold each time.
        self._prefetch_ahead(frame_number)

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

        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)
        if saturation == 100 and brightness == 100 and contrast == 100:
            pixmap = QPixmap.fromImage(qimage)
            target = self.current_movie_target_size
            if target and (pixmap.width() != target.width() or pixmap.height() != target.height()):
                mode = Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
                pixmap = pixmap.scaled(target, Qt.KeepAspectRatio, mode)
            self._store_animated_frame(key, pixmap)
            self.current_pixmap = pixmap
            self.image_label.setPixmap(pixmap)
            self.image_label.adjustSize()
            return

        # A look-ahead prefetch may already be processing this exact frame;
        # if so just wait for that result instead of computing it twice.
        if key in self.animated_inflight_keys:
            return
        self._submit_animated_frame_processing(qimage, frame_number, generation, key)

    def _submit_animated_frame_processing(self, qimage, frame_number, generation, key):
        # Process the frame in the anim worker pool (kept separate from the
        # static-image pool -- see _ANIM_WORKER_COUNT). QMovie itself stays
        # on the GUI thread because it's a Qt object; the expensive Pillow
        # color work happens off the UI thread and never creates a temp file.
        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)
        target = self.current_movie_target_size
        target_w = target.width() if target else None
        target_h = target.height() if target else None
        try:
            rgba = qimage.convertToFormat(QImage.Format_RGBA8888)
            w, h = rgba.width(), rgba.height()
            ptr = rgba.bits()
            ptr.setsize(rgba.byteCount())
            raw = bytes(ptr)
            self.animated_inflight_keys.add(key)
            def worker():
                try:
                    from PIL import Image
                    src = Image.frombuffer('RGBA', (w, h), raw, 'raw', 'RGBA', 0, 1)
                    rgb = src.convert('RGB')
                    # Color math runs at native (w, h) resolution -- see
                    # _anim_decode_size -- so the upscale to the on-screen
                    # target size, if any, happens once here at the end
                    # instead of the filter paying for the extra pixels.
                    rgb = apply_color_adjustments(rgb, saturation, brightness, contrast)
                    if target_w and target_h and (rgb.width != target_w or rgb.height != target_h):
                        resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                        rgb = rgb.resize((target_w, target_h), resample)
                    out = rgb.convert('RGBA')
                    return out.tobytes('raw', 'RGBA'), rgb.width, rgb.height
                except Exception:
                    return None
            future = ImageLoader._anim_executor.submit(worker)
            def done(fut, gen=generation, frame=frame_number, key=key):
                try:
                    result = fut.result()
                except Exception:
                    result = None
                self.load_bridge.animated_frame.emit(gen, frame, (key, result))
            future.add_done_callback(done)
        except Exception:
            self.animated_inflight_keys.discard(key)

    def _prefetch_ahead(self, frame_number):
        """Decode the next anim_lookahead frames on the paused prefetch
        movie and hand them to the worker pool now, so they're ready in
        animated_frame_cache before playback actually reaches them."""
        if not self.prefetch_movie or not self.current_movie:
            return
        # No adjustment active: the live path takes a free instant fast path
        # (QPixmap.fromImage with no Pillow round-trip), so there's nothing
        # worth precomputing here.
        if (self.settings.get('anim_saturation', 100) == 100
                and self.settings.get('anim_brightness', 100) == 100
                and self.settings.get('anim_contrast', 100) == 100):
            return
        total = self.prefetch_frame_count or self.current_movie.frameCount()
        if not total or total <= 1:
            return
        # Backpressure: if the worker pool is already as busy as it can
        # usefully be (e.g. a large/zoomed frame is taking a while), don't
        # pile speculative work on top of it -- that only pushes the frame
        # that's actually about to be displayed further back in the queue,
        # which is what made zoomed-in playback feel *slower* than before
        # prefetching existed. Just skip this round; the next real frame
        # change will try again once things free up.
        if len(self.animated_inflight_keys) >= _ANIM_WORKER_COUNT:
            return
        generation = self.current_movie_generation
        for step in range(1, self.anim_lookahead + 1):
            target = (frame_number + step) % total
            key = self._animated_cache_key(target)
            if key in self.animated_frame_cache or key in self.animated_inflight_keys:
                continue
            if len(self.animated_inflight_keys) >= _ANIM_WORKER_COUNT:
                break
            try:
                if not self.prefetch_movie.jumpToFrame(target):
                    continue
                qimage = self.prefetch_movie.currentImage()
                if qimage.isNull():
                    qimage = self.prefetch_movie.currentPixmap().toImage()
                if qimage.isNull():
                    continue
            except Exception:
                continue
            self._submit_animated_frame_processing(qimage, target, generation, key)

    def _on_animated_frame_ready(self, generation, frame_number, payload):
        key, result = payload
        self.animated_inflight_keys.discard(key)
        if generation != self.current_movie_generation or not self.current_movie:
            return
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
        # Count a completed cycle only when the movie actually wraps from a
        # later frame back to frame 0. This avoids counting the initial frame
        # as a completed playback.
        if frame_number == 0 and self.gif_last_frame > 0:
            self.gif_loop_count += 1
            if self.gif_loop_count >= self.gif_max_loops:
                self.gif_loop_count = 0
                self.gif_last_frame = -1
                self.next_image()
                return
        self.gif_last_frame = frame_number
    
    def update_image_display(self):
        # A new scaled pixmap invalidates the old pan position. Qt will clamp
        # scrollbars to the new image bounds after the label is resized.
        if self.panning:
            self._end_image_pan()
        # QScrollArea only lets image_label take on its own (pixmap) size when
        # widgetResizable is False. With it True, Qt force-fits the label to
        # the viewport on every layout pass regardless of adjustSize() below,
        # so a zoomed image can never register as "larger than the viewport"
        # and panning/scrollbars never actually engage. Keep it True only for
        # the fit-to-window case, where auto-fitting is what we want anyway.
        self.scroll_area.setWidgetResizable(self.fit_to_window)
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
                        # _apply_anim_scaled_size caps the movies' own
                        # decode size at the native resolution and keeps
                        # the live and look-ahead movie in sync -- see its
                        # docstring for why that matters here.
                        self._apply_anim_scaled_size(scaled_size)
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
        # The fit-to-window cache may be a reduced decode; actual-size needs the full source.
        self.show_current_image()
    
    def next_image(self):
        if self.image_list and self.current_index < len(self.image_list) - 1:
            self.current_index += 1
            self.show_current_image()
    
    def prev_image(self):
        if self.image_list and self.current_index > 0:
            self.current_index -= 1
            self.show_current_image()
    
    def _zoom_at(self, factor, global_pos=None):
        if not self.current_pixmap or self.current_pixmap.isNull():
            return

        if global_pos is None:
            from PyQt5.QtGui import QCursor
            global_pos = QCursor.pos()

        viewport = self.scroll_area.viewport()
        viewport_pos = viewport.mapFromGlobal(global_pos)

        # Capture the image-space point under the cursor before scaling.
        # For a large image this is simply viewport position + scroll offset.
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

        # Keep the same image pixel underneath the cursor.
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
        self.show_cursor()
        if self.isFullScreen():
            self.showNormal()
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
            '이미지 파일 (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff *.ico);;ZIP 파일 (*.zip);;모든 파일 (*)'
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
            # See apply_image_adjustments: cache keys already scope by
            # saturation/brightness/contrast/size, so no explicit clear is
            # needed here either -- it would only discard reusable entries.
            if self.image_list:
                self.show_current_image()
    
    def show_shortcut_settings(self):
        dialog = ShortcutSettingsDialog(self.settings, self)
        if dialog.exec_():
            pass
    
    def _can_pan_image(self):
        if not self.current_pixmap or self.fit_to_window:
            return False
        viewport = self.scroll_area.viewport().size()
        label_size = self.image_label.size()
        return (label_size.width() > viewport.width() + 1 or
                label_size.height() > viewport.height() + 1)

    def _start_image_pan(self, global_pos):
        if not self._can_pan_image():
            return False
        self.panning = True
        self.pan_start_pos = QPoint(global_pos)
        self.pan_start_h = self.scroll_area.horizontalScrollBar().value()
        self.pan_start_v = self.scroll_area.verticalScrollBar().value()
        self.show_cursor()
        self.setCursor(Qt.ClosedHandCursor)
        return True

    def _move_image_pan(self, global_pos):
        if not self.panning or self.pan_start_pos is None:
            return False
        delta = QPoint(global_pos) - self.pan_start_pos
        # Move in the same direction as the hand drag. Scrollbar values are
        # therefore decreased by the mouse delta. Qt clamps them to valid bounds.
        self.scroll_area.horizontalScrollBar().setValue(self.pan_start_h - delta.x())
        self.scroll_area.verticalScrollBar().setValue(self.pan_start_v - delta.y())
        return True

    def _end_image_pan(self):
        if not self.panning:
            return False
        self.panning = False
        self.pan_start_pos = None
        self.show_cursor()
        self.setCursor(Qt.ArrowCursor)
        self.reset_cursor_timer()
        return True

    def _is_pan_target(self, widget):
        if widget is None:
            return False
        viewport = self.scroll_area.viewport()
        if widget is self.image_label or widget is viewport:
            return True
        try:
            # widgetAt() can return a child widget inside the viewport.
            # What matters is whether the pointer is inside our image area.
            return viewport.isAncestorOf(widget) or self.image_label.isAncestorOf(widget)
        except Exception:
            return False

    def _handle_tilt_wheel(self, event):
        dx = event.angleDelta().x()
        if dx == 0:
            return False
        # The mouse reports horizontal tilt with the opposite sign on this
        # device/event path. Map the physical direction to the UI name.
        button_text = 'Tilt Left' if dx > 0 else 'Tilt Right'
        self.check_mouse_shortcut(button_text)
        event.accept()
        return True

    def eventFilter(self, obj, event):
        # The image label / scroll-area viewport sit directly under the
        # cursor and cover the whole window, so they receive wheel and
        # mouse-move events before QMainWindow ever would. Handle wheel
        # tilt, the resize cursor, and the auto-hide timer here directly
        # instead of relying on those reaching wheelEvent/mouseMoveEvent.
        if event.type() == QEvent.Wheel:
            if self._handle_tilt_wheel(event):
                return True
        elif event.type() == QEvent.MouseMove:
            if not (self.dragging or self.resizing or self.panning):
                self.update_cursor(obj.mapTo(self, event.pos()))
            self.show_cursor()
            self.reset_cursor_timer()
        return super().eventFilter(obj, event)

    def wheelEvent(self, event: QWheelEvent):
        if self._handle_tilt_wheel(event):
            # Tilt wheel acts as a button shortcut, so it still wakes the cursor.
            self.show_cursor()
            self.reset_cursor_timer()
            return
        # Plain up/down wheel scroll (prev/next image) must NOT un-hide the
        # cursor while it's hidden — applies in both windowed and fullscreen
        # mode since this handler is shared by both.
        if event.angleDelta().y() > 0:
            self.prev_image()
        else:
            self.next_image()
        event.accept()
    
    def mousePressEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        region = self.get_resize_region(event.pos())
        if event.button() == Qt.LeftButton and region and not self.isFullScreen():
            self.resizing = True
            self.resize_start_pos = event.globalPos()
            self.resize_start_size = self.size()
            self.resize_region = region
            event.accept()
            return
        if event.button() == Qt.LeftButton and not region:
            # Fullscreen has no window-position dragging.
            if self.isFullScreen():
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
        if self.fit_to_window and not self._display_update_timer.isActive():
            self._display_update_timer.start(8)
    
    def closeEvent(self, event: QCloseEvent):
        self.show_cursor()
        if self.isFullScreen():
            self.showNormal()
        if not self.isFullScreen():
            self.save_settings()
        self.stop_current_movie()
        self.slideshow.stop()
        self.cursor_hide_timer.stop()
        # Stop creating new background work and release all worker threads.
        # This is important on Windows: ThreadPoolExecutor worker threads can
        # keep the process alive and retain large decoded images/ZIP handles.
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
